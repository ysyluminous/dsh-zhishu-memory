#!/usr/bin/env node
/**
 * omnihist.db → mem（pg16）迁移。幂等，可重跑。
 *
 * 口径变化（2026-11-19）：以前是「omnihist 存会话原文、mem 只存索引」；现在 mem 是唯一会话库，
 * 所以要把 omnihist 里的会话/消息全量搬进来，之后 omnihist 才可以删。
 *
 * 去重：按 (conversation_id, seq, content_hash) 判重——两边的 omni_id 是各自序列，不能拿来比。
 *
 * 用法：
 *   node tools/omnihist-migrate.mjs                 # dry-run：只报数字
 *   node tools/omnihist-migrate.mjs --source dsh    # dry-run 单个来源
 *   node tools/omnihist-migrate.mjs --apply         # 真写（先 dry-run 看过再执行）
 */
import { DatabaseSync } from 'node:sqlite'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const argv = process.argv.slice(2)
const flag = (name) => { const i = argv.indexOf(`--${name}`); return i === -1 ? null : argv[i + 1] }
const apply = argv.includes('--apply')
// `--source` 没给值（或给了空串）一律当「全量」，别让 '' 既当过滤条件又当参数。
const onlySource = flag('source') || ''
const DB = join(homedir(), '.omnihist', 'omnihist.db')
const CONTAINER = 'zhishu-mem-pg'

const psql = (sql) => spawnSync('docker', ['exec', '-i', CONTAINER, 'psql', '-U', 'postgres', '-d', 'mem', '-q', '-At', '-F', '\t', '-v', 'ON_ERROR_STOP=1'], {
  encoding: 'utf8', input: sql, windowsHide: true, maxBuffer: 1024 * 1024 * 1024,
})

const db = new DatabaseSync(DB, { readOnly: true })
const where = onlySource ? 'where source = ?' : ''
const params = onlySource ? [onlySource] : []

// 目标：每个来源要搬多少、已存在多少（按 conversation_id+seq+content_hash 判重）
const sources = db.prepare(`select source, count(*) convs from (select distinct source, id from conversation ${where}) group by 1 order by 1`).all(...params)
const memCount = new Map()
const memRows = psql("select split_part(conversation_id,':',1) s, count(*) from mem.conv_message group by 1").stdout || ''
for (const line of memRows.split('\n')) {
  const [s, n] = line.split('\t')
  if (s) memCount.set(s, Number(n))
}

console.log(`[${apply ? 'APPLY' : 'DRY-RUN'}] omnihist → mem（${DB}）`)
let totalNew = 0
for (const row of sources) {
  const source = row.source
  const convs = db.prepare('select count(*) c from conversation where source = ?').get(source).c
  const msgs = db.prepare(`select count(*) c from message m join conversation c2 on c2.id = m.conversation_id where c2.source = ?`).get(source).c
  const chars = db.prepare(`select coalesce(sum(length(m.text)),0) c from message m join conversation c2 on c2.id = m.conversation_id where c2.source = ?`).get(source).c
  const have = memCount.get(source) ?? 0
  const gap = Math.max(0, msgs - have)
  totalNew += gap
  console.log(`  ${source.padEnd(14)} 会话 ${String(convs).padStart(5)}｜消息 ${String(msgs).padStart(7)}｜mem 已有 ${String(have).padStart(7)}｜待补 ≈ ${String(gap).padStart(7)}｜${(chars / 1e6).toFixed(1)} MB`)
}
console.log(`合计待补 ≈ ${totalNew} 条`)

if (!apply) {
  console.log('\n（dry-run 结束；确认后用 --apply 真写）')
  process.exit(0)
}

/* ── 真写：COPY 到临时表 → 幂等插入 ─────────────────────── */
const esc = (value) => {
  if (value === null || value === undefined) return '\\N'
  return String(value).replace(/\\/g, '\\\\').replace(/\t/g, '\\t').replace(/\r/g, '\\r').replace(/\n/g, '\\n')
}

// 去重键自动探测：mem 里若已有该会话，比 (seq, content_hash) 是否一致。
// 一致 → 按 hash 去重；不一致（历史镜像口径不同）→ 退化成按 seq 去重并告警。
//
// 探针只是「能更准就更好」的优化：任何一步取不到值（没有可探测的会话、psql 读不到、
// omnihist 侧没有对应消息）都必须落回 seq 去重。以前这里会在 sample 为空时仍然
// 走探测分支，然后拿空值去查 message / 参与比较（`Math.min(3, 0)` 还会把模式判成
// hash），单来源/空库时直接崩或写重。现在先把空值挡在分支外。
let dedupeMode = 'seq'
const sample = db.prepare('select id from conversation where source = ? order by created_at desc limit 1').get(onlySource || 'dsh')
if (!sample || typeof sample.id !== 'string' || !sample.id) {
  console.log('  [去重探测] 没有可探测的会话（sample 为空）→ 按 seq 去重')
} else {
  try {
    const memPairs = new Set()
    const out = String(psql(`select seq||':'||left(content_hash,12) from mem.conv_message where conversation_id='${sample.id.replace(/'/g, "''")}' order by seq limit 20`).stdout || '')
    for (const line of out.split('\n')) if (line.trim()) memPairs.add(line.trim())
    // mem 里没有这个会话（或 psql 读不到行）就没有可比对的基准：交给下方的纯新增分支。
    // 注意用 substr 而不是 Postgres 的 left()：node:sqlite 里没有 left，一用就
    // "no such function: left" —— 这正是以前 --apply 在「mem 已有该会话」时必崩的原因。
    const omniPairs = memPairs.size > 0
      ? db.prepare('select seq, substr(content_hash, 1, 12) h from message where conversation_id = ? order by seq limit 20').all(sample.id)
      : []
    if (memPairs.size === 0) {
      console.log(`  [去重探测] ${sample.id}：mem 里没有这个会话 → 纯新增`)
    } else if (omniPairs.length === 0) {
      // mem 有这个会话、omnihist 侧一条消息都没有 → 没有可比对的行，按 seq 去重。
      console.log(`  [去重探测] ${sample.id}：mem 有 ${memPairs.size} 行、omnihist 侧 0 行 → 按 seq 去重`)
    } else {
      const same = omniPairs.filter((row) => memPairs.has(`${row.seq}:${row.h}`)).length
      dedupeMode = same >= Math.min(3, omniPairs.length) ? 'hash' : 'seq'
      console.log(`  [去重探测] ${sample.id}：两边对上 ${same}/${omniPairs.length} 对 → 按 ${dedupeMode} 去重`)
    }
  } catch (error) {
    dedupeMode = 'seq'
    console.log(`  [去重探测] 跳过（${String((error && error.message) || error).slice(0, 120)}）→ 按 seq 去重`)
  }
}
const notExists = dedupeMode === 'hash'
  ? `and t.seq = stage_msg.seq and t.content_hash = stage_msg.content_hash`
  : `and t.seq = stage_msg.seq`

for (const row of sources) {
  const source = row.source
  const convs = db.prepare('select * from conversation where source = ?').all(source)
  if (convs.length === 0) continue
  const msgsOf = db.prepare('select * from message where conversation_id = ? order by seq, id')
  const started = Date.now()
  let inserted = 0

  const chunks = []
  const push = (sql) => chunks.push(sql)
  push(`begin;`)
  push(`create temp table stage_conv (id text, source text, external_id text, title text, project text, cwd text, model text, created_at bigint, updated_at bigint, message_count int) on commit drop;`)
  push(`create temp table stage_msg (conversation_id text, seq int, role text, kind text, created_at bigint, text_b64 text, content_hash text) on commit drop;`)
  push(`copy stage_conv from stdin;`)
  for (const c of convs) {
    const count = db.prepare('select count(*) c from message where conversation_id = ?').get(c.id).c
    chunks.push([c.id, c.source, c.external_id, c.title, c.project, c.cwd, c.model, c.created_at, c.updated_at, count].map(esc).join('\t'))
  }
  push(`\\.`)
  push(`copy stage_msg (conversation_id, seq, role, kind, created_at, text_b64, content_hash) from stdin;`)
  for (const c of convs) {
    for (const m of msgsOf.all(c.id)) {
      chunks.push([c.id, m.seq, m.role, m.kind, m.created_at, Buffer.from(String(m.text ?? '').replace(/\u0000/g, ''), 'utf8').toString('base64'), m.content_hash].map(esc).join('\t'))
    }
  }
  push(`\\.`)
  push(`insert into mem.conv (id, source, external_id, title, project, cwd, model, created_at, updated_at, message_count, snippet, hash, user_id)
        select id, source, external_id, coalesce(title,''), coalesce(project,''), coalesce(cwd,''), coalesce(model,''),
               to_timestamp(created_at/1000.0), to_timestamp(updated_at/1000.0), message_count, '', '', 'owner'
        from stage_conv
        on conflict (id) do update set message_count = excluded.message_count, updated_at = excluded.updated_at, title = excluded.title;`)
  push(`insert into mem.conv_message (omni_id, conversation_id, seq, role, kind, created_at, text, content_hash, meta, synced_at)
        select ('x' || substr(md5(conversation_id || ':' || seq::text), 1, 14))::bit(56)::bigint,
               conversation_id, seq, role, coalesce(kind,''), to_timestamp(created_at/1000.0), convert_from(decode(text_b64,'base64'),'UTF8'), coalesce(content_hash,''), '{}'::jsonb, now()
        from stage_msg
        where not exists (
          select 1 from mem.conv_message t
          where t.conversation_id = stage_msg.conversation_id
            ${notExists}
        );`)
  push(`commit;`)
  push(`select count(*) from mem.conv_message where conversation_id in (select id from mem.conv where source = '${source}');`)

  const result = psql(chunks.join('\n'))
  if (result.status !== 0) {
    console.error(`✗ ${source} 失败：${String(result.stderr || '').slice(0, 500)}`)
    process.exit(1)
  }
  const tail = String(result.stdout || '').trim().split('\n').pop()
  console.log(`✓ ${source}：会话 ${convs.length}，写入后该来源共 ${tail} 条（${((Date.now() - started) / 1000).toFixed(1)}s）`)
}
console.log('完成。')
