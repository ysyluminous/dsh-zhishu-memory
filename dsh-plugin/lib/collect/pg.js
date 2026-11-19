/**
 * 本地直写（collect → 本机 pg16）。
 *
 * 逐条 `POST /ingest` 是汇集一直慢在的地方：服务端单条约 127ms，dsh 首轮要几分钟，
 * 客户端还会超时打断。这里改成**一次 COPY 批量写**，口径直接对齐已经跑通的
 * `tools/omnihist-migrate.mjs`：
 *
 *   - 文本走 COPY 的 base64 列（`convert_from(decode(...,'base64'),'UTF8')`），
 *     绕开 \t / \n / \\ 转义和 NUL（PostgreSQL 的 text 存不了 \u0000）；
 *   - `omni_id = ('x' || substr(md5(conversation_id || ':' || seq), 1, 14))::bit(56)::bigint`，
 *     和迁移工具同一条公式，所以两边写进的是**同一个槽位**；
 *   - 幂等键 = `(conversation_id, seq, content_hash)`：
 *       · 同 seq 同 hash → 不出现在待写集合里（重复行 0）；
 *       · 同 seq 不同 hash，且那一行是我们自己写的（omni_id 相同）→ 就地更新文本；
 *       · 同 seq 但那一行是别的写入方（omnihist 用 sqlite rowid 当 omni_id）→ 不动、不新增。
 *     结果：一个 (conversation_id, seq) 永远只有一行，重跑 0 变化。
 *
 * 拿不到本地库（没配 `pgContainer` / 本机没有 docker / 容器没起 / mem 表不存在）时，
 * 所有函数都返回 `ok:false`，调用方回落到 `/ingest`。
 *
 * 为什么用 spawnSync：scan 本身就是同步读文件（几十 MB 的 jsonl 要 3~4 秒），
 * 这里再异步也救不回宿主的事件循环；同步反而让「写成功才推进状态」的语义最简单。
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

import { sha256 } from './util.js'

/** docker exec psql 的单次超时；COPY 几十 MB 也在里面。 */
const PSQL_TIMEOUT_MS = 180_000

/** 单批 COPY 的字节上限：太大内存翻倍，太小每批都要付一次 psql 启动开销。 */
const MAX_BATCH_BYTES = 8 * 1024 * 1024

/** 一次计数查询里塞多少个 conversation_id；再多就把 SQL 拆开。 */
const IDS_PER_QUERY = 500

/** 解析到的 docker 可执行文件：undefined = 还没探测，null = 不可用。 */
let dockerPath

/**
 * Docker 可执行文件候选，最具体的在前。
 *
 * 不能只靠 PATH：DSH 宿主进程的 PATH 常常和用户终端不一致（Docker Desktop 装完
 * 只改了用户 PATH，宿主是更早启动的），实测宿主里 `docker` 直接 ENOENT。
 * @returns {string[]} Candidates in order.
 */
function dockerCandidates() {
  const list = []
  if (process.env.DOCKER_BIN) list.push(process.env.DOCKER_BIN)
  if (process.platform === 'win32') {
    const programFiles = process.env.ProgramFiles || 'C:\\Program Files'
    const programData = process.env.ProgramData || 'C:\\ProgramData'
    list.push(
      join(programFiles, 'Docker', 'Docker', 'resources', 'bin', 'docker.exe'),
      join(programData, 'DockerDesktop', 'version-bin', 'docker.exe'),
    )
  }
  list.push('docker')
  return list
}

/**
 * Resolve a working Docker CLI once (cheap `--version` probe); null when none answers.
 * @returns {string|null} Executable to spawn, or null.
 */
function dockerBin() {
  if (dockerPath !== undefined) return dockerPath
  for (const candidate of dockerCandidates()) {
    if (candidate.includes('/') || candidate.includes('\\')) {
      if (!existsSync(candidate)) continue
    }
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8', timeout: 5000, windowsHide: true })
    if (probe.status === 0) {
      dockerPath = candidate
      return candidate
    }
  }
  dockerPath = null
  return null
}

/**
 * Run one psql script through `docker exec -i` and collect stdout/stderr.
 * @param {{docker: string, container: string, user: string, database: string}} local - Resolved local target.
 * @param {string} sql - SQL script (may contain `copy ... from stdin` blocks).
 * @returns {{ok: boolean, stdout: string, stderr: string}} Outcome.
 */
function psql(local, sql) {
  const result = spawnSync(local.docker, [
    'exec', '-i', local.container,
    'psql', '-U', local.user, '-d', local.database,
    '-q', '-At', '-F', '\t', '-v', 'ON_ERROR_STOP=1',
  ], {
    encoding: 'utf8',
    input: sql,
    windowsHide: true,
    timeout: PSQL_TIMEOUT_MS,
    maxBuffer: 1024 * 1024 * 1024,
  })
  const stdout = String(result.stdout || '')
  const stderr = String(result.stderr || '')
  if (result.error) return { ok: false, stdout, stderr: `${result.error.message}${stderr ? ` | ${stderr}` : ''}` }
  if (result.status !== 0) return { ok: false, stdout, stderr }
  return { ok: true, stdout, stderr }
}

/**
 * First meaningful line of a psql error, for reports and audit rows.
 * @param {string} stderr - Raw stderr.
 * @param {string} [fallback] - Value when stderr is empty.
 * @returns {string} Short error text.
 */
function shortError(stderr, fallback = 'psql failed') {
  const line = String(stderr || '')
    .split(/\r?\n/)
    .map((item) => item.trim())
    .find((item) => item && !/^psql: warning/i.test(item))
  return (line || fallback).slice(0, 300)
}

/**
 * Normalise one string for SQL literal use (single quotes doubled).
 * @param {unknown} value - Raw value.
 * @returns {string} Quoted literal.
 */
function literal(value) {
  return `'${String(value ?? '').replace(/\u0000/g, '').replace(/'/g, "''")}'`
}

/**
 * One COPY text-format field: NULL is `\N`, the rest is backslash-escaped.
 * @param {unknown} value - Raw value.
 * @returns {string} Field text.
 */
function field(value) {
  if (value === null || value === undefined) return '\\N'
  return String(value)
    .replace(/\\/g, '\\\\')
    .replace(/\t/g, '\\t')
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n')
}

/**
 * Strip NUL (PostgreSQL text rejects it) and coerce to text.
 * @param {unknown} value - Raw value.
 * @returns {string} Safe text.
 */
function cleanText(value) {
  return String(value ?? '').replace(/\u0000/g, '')
}

/**
 * Parse an ISO timestamp (or number) into epoch milliseconds.
 * @param {unknown} value - ISO string, ms number, or anything else.
 * @returns {number|null} Epoch ms, or null.
 */
function msOf(value) {
  if (value === undefined || value === null || value === '') return null
  const stamp = typeof value === 'number' ? value : Date.parse(String(value))
  return Number.isFinite(stamp) && stamp > 0 ? Math.round(stamp) : null
}

/**
 * Resolve the local pg target from the run environment (no probing).
 * @param {{pg?: {container?: string, user?: string, database?: string}}} env - Collect environment.
 * @returns {{docker: string, container: string, user: string, database: string}|null} Target, or null when unconfigured.
 */
function targetOf(env) {
  const container = String(env?.pg?.container || '').trim()
  if (!container) return null
  return {
    docker: '',
    container,
    user: String(env?.pg?.user || 'postgres').trim() || 'postgres',
    database: String(env?.pg?.database || 'mem').trim() || 'mem',
  }
}

/**
 * Probe whether the local database is usable for this run.
 *
 * Nothing here is fatal: an unconfigured / unreachable local database is a normal
 * state (other machines, service-down setups) and simply means "use `/ingest`".
 * @param {{pg?: Record<string, unknown>}} env - Collect environment.
 * @returns {{ok: boolean, local?: Record<string, any>, error?: string}} Availability.
 */
export function localStore(env) {
  const target = targetOf(env)
  if (!target) return { ok: false, error: 'pgContainer 未配置' }
  const docker = dockerBin()
  if (!docker) return { ok: false, error: 'docker 不可用（PATH 与常见安装路径都没找到）' }
  const local = { ...target, docker }
  const ping = psql(local, 'select 1')
  if (!ping.ok) return { ok: false, error: `本地库不可达：${shortError(ping.stderr)}` }
  return { ok: true, local }
}

/**
 * How many messages mem already holds for each conversation, and whether any of them
 * were written by somebody else.
 *
 * This is both the skip signal ("mem 里该会话的消息数 ≥ 本地条数 → 已同步") and the
 * reason to count `live_event` too: `/ingest` writes there, and `conv_get` merges the
 * two tables, so an already-HTTP-ingested session must not be written again as
 * conv_message rows.
 *
 * The `foreign` flag exists because conversation *content* can come from a different
 * importer with a different seq numbering (omnihist 导入的 workbuddy 会话就是如此：
 * 它按自己的口径去重/过滤，同一个 seq 对应的不是同一条消息）。往这种会话里按
 * seq 追加，会把历史写成乱序、内容重复。所以只对「mem 里这一行是我们自己写
 * （omni_id 由 md5(conv:seq) 得来，数值远大于 sqlite rowid）」的会话做增量追加。
 * @param {Record<string, any>} local - Resolved local target.
 * @param {string[]} conversationIds - Ids to count.
 * @returns {{ok: boolean, counts: Map<string, number>, foreign: Map<string, number>, error?: string}}
 *   Message counts and the number of non-collect rows per conversation id.
 */
export function readCounts(local, conversationIds) {
  const counts = new Map()
  const foreign = new Map()
  const ids = [...new Set(conversationIds.map((id) => cleanText(id)).filter(Boolean))]
  // omnihist 的 omni_id 是 sqlite rowid（几十万量级）；汇集写的是 md5 前 56 位
  // （> 1e15）。这个上限足够把它们分开，又不用再记一张「谁写过」的表。
  const OURS_FLOOR = 4294967295
  for (let start = 0; start < ids.length; start += IDS_PER_QUERY) {
    const chunk = ids.slice(start, start + IDS_PER_QUERY)
    const list = chunk.map(literal).join(',')
    // live_event 是 /ingest 的落点，conv_message 是本地直写的落点；两边都算「已有」。
    // 老库里没有 live_event 时整条 SQL 会报错，退回只数 conv_message。
    const combined = `select conversation_id, sum(n)::int, sum(f)::int from (
  select conversation_id, 1 as n, case when omni_id <= ${OURS_FLOOR} then 1 else 0 end as f
    from mem.conv_message where conversation_id in (${list})
  union all
  select conversation_id, 1, 0
    from mem.live_event where reconciled_at is null and conversation_id in (${list})
) u group by 1`
    const onlyConv = `select conversation_id, count(*)::int, count(*) filter (where omni_id <= ${OURS_FLOOR})::int
  from mem.conv_message where conversation_id in (${list}) group by 1`
    let result = psql(local, combined)
    if (!result.ok) result = psql(local, onlyConv)
    if (!result.ok) return { ok: false, counts, foreign, error: shortError(result.stderr) }
    for (const line of result.stdout.split(/\r?\n/)) {
      if (!line.trim()) continue
      const [id, total, imported] = line.split('\t')
      if (!id) continue
      counts.set(id, (counts.get(id) || 0) + (Number(total) || 0))
      foreign.set(id, (foreign.get(id) || 0) + (Number(imported) || 0))
    }
  }
  return { ok: true, counts, foreign }
}

/**
 * One conversation row for `stage_conv`.
 *
 * `snippet` / `hash` 与 `/ingest` 的口径一致：取第一条 user 消息（没有就第一条消息）
 * 压成单行、最多 300 字；hash = sha256(`title|role|snippet`)。会话列表与「这条会话
 * 变没变」都读这两列，留空会让面板预览变成空白。
 * @param {string} adapterId - Adapter id (`source`).
 * @param {Record<string, any>} session - Session from scan.
 * @returns {{id: string, line: string}} Conversation id and its COPY line.
 */
function convRow(adapterId, session) {
  const externalId = cleanText(session.externalId)
  const messages = Array.isArray(session.messages) ? session.messages : []
  let last = null
  for (const message of messages) {
    const stamp = msOf(message && message.time)
    if (stamp !== null && (last === null || stamp > last)) last = stamp
  }
  const created = msOf(session.createdAt) ?? last ?? Date.now()
  const updated = last ?? created
  const id = `${adapterId}:${externalId}`
  const snippetFrom = messages.find((message) => message && message.role === 'user') || messages[0] || null
  const snippet = snippetFrom ? cleanText(snippetFrom.text).replace(/\s+/g, ' ').trim().slice(0, 300) : ''
  const hash = snippet ? sha256(`${session.title || ''}|${snippetFrom.role || 'user'}|${snippet}`) : ''
  return {
    id,
    line: [
      field(id),
      field(adapterId),
      field(externalId),
      field(session.title || ''),
      field(''),
      field(session.cwd || ''),
      field(''),
      field(created),
      field(updated),
      field(conversationTotal(session)),
      field(snippet),
      field(hash),
    ].join('\t'),
  }
}

/** One message row for `stage_msg`; null when the message is unusable. */
function messageRow(conversationId, message) {
  const seq = Number(message && message.seq)
  if (!Number.isInteger(seq) || seq < 0) return null
  const text = cleanText(message && message.text)
  if (!text) return null
  const role = ['user', 'assistant', 'tool', 'system'].includes(message.role) ? message.role : 'user'
  return [
    field(conversationId),
    field(seq),
    field(role),
    field(typeof message.kind === 'string' ? message.kind : (role === 'tool' ? 'tool-result' : 'message')),
    field(msOf(message.time)),
    field(Buffer.from(text, 'utf8').toString('base64')),
    field(sha256(text)),
  ].join('\t')
}

/** The whole write for one stage batch: temp tables → COPY → 幂等插入。 */
function batchSql(convLines, msgLines) {
  return [
    'begin;',
    'create temp table stage_conv (id text, source text, external_id text, title text, project text, cwd text, model text, created_at bigint, updated_at bigint, message_count int, snippet text, hash text) on commit drop;',
    'create temp table stage_msg (conversation_id text, seq int, role text, kind text, created_at bigint, text_b64 text, content_hash text) on commit drop;',
    'copy stage_conv from stdin;',
    ...convLines,
    '\\.',
    'copy stage_msg (conversation_id, seq, role, kind, created_at, text_b64, content_hash) from stdin;',
    ...msgLines,
    '\\.',
    // 会话行：只补元信息；message_count 先取两边大的，最后统一按物理行数校正。
    `insert into mem.conv (id, source, external_id, title, project, cwd, model, created_at, updated_at, message_count, snippet, hash, user_id)
     select id, source, external_id, coalesce(title,''), coalesce(project,''), coalesce(cwd,''), coalesce(model,''),
            to_timestamp(created_at/1000.0), to_timestamp(updated_at/1000.0), message_count,
            coalesce(snippet,''), coalesce(hash,''), 'owner'
     from stage_conv
     on conflict (id) do update
       set source = excluded.source,
           external_id = excluded.external_id,
           title = case when excluded.title <> '' then excluded.title else mem.conv.title end,
           cwd = case when excluded.cwd <> '' then excluded.cwd else mem.conv.cwd end,
           snippet = case when excluded.snippet <> '' then excluded.snippet else mem.conv.snippet end,
           hash = case when excluded.hash <> '' then excluded.hash else mem.conv.hash end,
           message_count = greatest(mem.conv.message_count, excluded.message_count),
           updated_at = greatest(coalesce(mem.conv.updated_at, excluded.updated_at), excluded.updated_at);`,
    // 消息行：omni_id 同迁移工具；同 seq 只允许一行（别人的行不覆盖、不新增）。
    `with src as (
       select ('x' || substr(md5(conversation_id || ':' || seq::text), 1, 14))::bit(56)::bigint as omni_id,
              conversation_id, seq, role, kind, created_at, text_b64, content_hash
       from stage_msg
     )
     insert into mem.conv_message (omni_id, conversation_id, seq, role, kind, created_at, text, content_hash, meta, synced_at)
     select s.omni_id, s.conversation_id, s.seq, coalesce(s.role,''), coalesce(s.kind,''),
            to_timestamp(s.created_at/1000.0),
            convert_from(decode(s.text_b64, 'base64'), 'UTF8'),
            coalesce(s.content_hash, ''), '{}'::jsonb, now()
     from src s
     where not exists (
       select 1 from mem.conv_message t
       where t.conversation_id = s.conversation_id
         and t.seq = s.seq
         and t.omni_id <> s.omni_id
     )
     on conflict (omni_id) do update
       set text = excluded.text,
           content_hash = excluded.content_hash,
           role = excluded.role,
           kind = excluded.kind,
           synced_at = now()
       where mem.conv_message.content_hash <> excluded.content_hash;`,
    // message_count 以物理行为准：面板读它、conv_get 读行，两边必须一致。
    // 注意不能取 greatest——本轮只写了尾巴（状态里 sent 已经推进过）时，
    // 会话总条数会比库里真实的条数大，留下一个永远对不上的数字。
    `update mem.conv c
        set message_count = (select count(*) from mem.conv_message m where m.conversation_id = c.id)
      where c.id in (select id from stage_conv);`,
    'select count(*) from mem.conv_message where conversation_id in (select id from stage_conv);',
    'commit;',
  ].join('\n')
}

/**
 * Write new messages of the given sessions into `mem.conv_message` through COPY.
 *
 * Sessions are staged in batches (bounded by {@link MAX_BATCH_BYTES}); each batch is
 * one transaction, so a failure leaves the previous batches committed and the run
 * marked failed — the next run re-stages everything and the idempotency key drops
 * what is already there.
 * @param {Record<string, any>} local - Resolved local target.
 * @param {string} adapterId - Adapter id (`source`).
 * @param {Array<Record<string, any>>} sessions - Sessions to write.
 * @returns {{ok: boolean, messages: number, conversations: number, error?: string}} Outcome.
 */
export function writeSessions(local, adapterId, sessions) {
  const list = Array.isArray(sessions) ? sessions : []
  if (list.length === 0) return { ok: true, messages: 0, conversations: 0 }

  const seen = new Set()
  let convRows = []
  let msgLines = []
  let bytes = 0
  let messages = 0
  const conversations = new Set()

  const flush = () => {
    if (msgLines.length === 0 && convRows.length === 0) return null
    // 同一批里会话行去重（一个会话的消息可能被拆到两批，重复行无害但没必要）
    const unique = [...new Map(convRows.map((row) => [row.id, row.line])).values()]
    const batchMessages = msgLines.length
    const result = psql(local, batchSql(unique, msgLines))
    convRows = []
    msgLines = []
    bytes = 0
    return { result, batchMessages }
  }

  for (const session of list) {
    const externalId = cleanText(session && session.externalId).trim()
    if (!externalId) continue
    const conversationId = `${adapterId}:${externalId}`
    const row = convRow(adapterId, session)
    convRows.push(row)
    conversations.add(row.id)
    for (const message of Array.isArray(session.messages) ? session.messages : []) {
      const key = `${conversationId}\u0000${Number(message && message.seq)}`
      if (seen.has(key)) continue
      const line = messageRow(conversationId, message)
      if (line === null) continue
      seen.add(key)
      if (msgLines.length > 0 && bytes + line.length > MAX_BATCH_BYTES) {
        const flushed = flush()
        // 这一批失败就停：这一批的条数没落库，从报告里扣掉；下一轮从状态重放，幂等键保证不重复。
        if (flushed && !flushed.result.ok) {
          messages -= flushed.batchMessages
          return { ok: false, messages, conversations: conversations.size, error: shortError(flushed.result.stderr) }
        }
      }
      msgLines.push(line)
      bytes += line.length + 1
      messages += 1
    }
  }

  const flushed = flush()
  if (flushed && !flushed.result.ok) {
    messages -= flushed.batchMessages
    return { ok: false, messages, conversations: conversations.size, error: shortError(flushed.result.stderr) }
  }
  return { ok: true, messages, conversations: conversations.size }
}

/**
 * Absolute message count of one scan batch.
 *
 * Every bundled adapter numbers `seq` from 0 for the whole session (jsonl ones keep a
 * `sent` counter, the zstd one slices a fully parsed session), so `max(seq) + 1` is the
 * session's total even when this batch is only the tail. Adaptors may also declare
 * `total` explicitly; that wins.
 * @param {Record<string, any>} session - Session from scan.
 * @returns {number} Total messages the local store holds for this session.
 */
export function conversationTotal(session) {
  const declared = Number(session && session.total)
  const messages = Array.isArray(session && session.messages) ? session.messages : []
  let max = -1
  for (const message of messages) {
    const seq = Number(message && message.seq)
    if (Number.isInteger(seq) && seq > max) max = seq
  }
  return Math.max(Number.isFinite(declared) && declared > 0 ? declared : 0, max + 1)
}
