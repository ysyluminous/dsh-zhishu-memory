#!/usr/bin/env node
/**
 * 智枢记忆套件 · 有效性体检
 *
 * 这个脚本只读不写：吃 ~/.dsh 里插件自己留下的三份证据，输出一张记分卡。
 *
 *   1. ~/.dsh/zhishu-memory/audit.jsonl   记忆桥：gate 命中/失败/耗时、写回成败
 *   2. ~/.dsh/bot/workspace/logs/ctx-guard.jsonl  预算自检：注入了几次、有没有报错
 *   3. ~/.dsh/sessions/**.jsonl.zstd      真实会话日志：turn 收尾是 completed 还是 error
 *
 * 为什么要第 3 层：前两层只能证明「插件自己觉得没问题」，只有会话日志里的
 * `turn/end.reason.kind` 才能证明「用户这一轮真的跑完了」——上次那个
 * "Cannot read properties of undefined (reading 'kind')" 就是只在前两层看不出来的。
 *
 * 用法（用 DSH 自带 Node，普通 node 也行，zstd 需要 Node ≥ 22.15）：
 *   node tools/audit-report.mjs
 *   node tools/audit-report.mjs --days 3 --sessions 20
 *   node tools/audit-report.mjs --json        # 给定时任务/看板吃
 *
 * 退出码：0 = 全部 PASS/WARN；1 = 有 FAIL（可以直接拿它当 CI 断言）。
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import * as zlib from 'node:zlib'

const argv = process.argv.slice(2)
const flag = (name, fallback) => {
  const at = argv.indexOf(`--${name}`)
  return at === -1 ? fallback : argv[at + 1]
}
const asJson = argv.includes('--json')
const days = Math.max(1, Number(flag('days', 7)) || 7)
const hours = Math.max(1, Number(flag('hours', 24)) || 24)
const maxSessions = Math.max(1, Number(flag('sessions', 40)) || 40)

const HOME = homedir()
const AUDIT = join(HOME, '.dsh', 'zhishu-memory', 'audit.jsonl')
const GUARD_LOG = join(HOME, '.dsh', 'bot', 'workspace', 'logs', 'ctx-guard.jsonl')
const SETTINGS = join(HOME, '.dsh', 'zhishu-memory', 'settings.json')
const SESSIONS = join(HOME, '.dsh', 'sessions')

/** Read a JSONL file, skipping unparsable lines. */
function readJsonl(file) {
  if (!existsSync(file)) return []
  const out = []
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const text = line.trim()
    if (!text) continue
    try {
      out.push(JSON.parse(text))
    } catch {
      // A torn last line is normal for an append-only log.
    }
  }
  return out
}

/** Nearest-rank percentile over unsorted numbers. */
function percentile(values, p) {
  if (values.length === 0) return null
  const sorted = [...values].sort((a, b) => a - b)
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1))
  return sorted[index]
}

const round = (value, digits = 0) => (value === null || value === undefined ? '—' : Number(value).toFixed(digits))

/* ── 1. 记忆桥 ─────────────────────────────────────────────── */

const audit = readJsonl(AUDIT)
const since = Date.now() - hours * 3_600_000

/**
 * Counters for one time window of the bridge log.
 * @param {ReadonlyArray<any>} rows - Audit rows inside the window.
 * @returns {Record<string, any>} Aggregates.
 */
function bridgeStats(rows) {
  const gate = rows.filter((row) => row.event === 'gate' || row.event === 'gate-failed')
  const writes = rows.filter((row) => row.event === 'write-back')
  return {
    gateTotal: gate.length,
    hit: gate.filter((row) => row.event === 'gate' && row.class === 'hit').length,
    empty: gate.filter((row) => row.event === 'gate' && row.class === 'empty').length,
    failed: gate.filter((row) => row.event === 'gate-failed').length,
    ms: gate.map((row) => Number(row.ms)).filter(Number.isFinite),
    chars: gate.filter((row) => row.event === 'gate').map((row) => Number(row.chars) || 0),
    writes: writes.length,
    ok: writes.filter((row) => row.ok === true).length,
    retried: writes.filter((row) => row.attempts > 1).length,
    writeMs: writes.map((row) => Number(row.ms)).filter(Number.isFinite),
    skipped: rows.filter((row) => row.event === 'write-back-skipped').length,
    plans: rows.filter((row) => row.event === 'ctx-plan' || row.event === 'ctx-plan-failed').length,
    lastPlan: [...rows].reverse().find((row) => row.event === 'ctx-plan' || row.event === 'ctx-plan-failed'),
  }
}

const recent = bridgeStats(audit.filter((row) => Date.parse(row.ts) >= since))
const lifetime = bridgeStats(audit)
const loads = audit.filter((row) => row.event === 'plugin-loaded')

/* ── 2. 预算自检 ───────────────────────────────────────────── */

const guard = readJsonl(GUARD_LOG)
const guardNotes = guard.filter((row) => row.chars !== undefined).length
const guardErrors = guard.filter((row) => row.error !== undefined)

/* ── 3. 真实会话日志（唯一能证明"这一轮跑完了"的一层）─────────── */

const decodeFrames = async (buffer) => {
  if (typeof zlib.createZstdDecompress !== 'function') return null
  const magic = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])
  const offsets = []
  for (let from = 0; ;) {
    const at = buffer.indexOf(magic, from)
    if (at === -1) break
    offsets.push(at)
    from = at + 4
  }
  let text = ''
  for (const at of offsets) {
    const stream = zlib.createZstdDecompress()
    const chunks = []
    const done = new Promise((resolve, reject) => {
      stream.on('data', (chunk) => chunks.push(chunk))
      stream.on('end', resolve)
      stream.on('error', reject)
    })
    stream.end(buffer.subarray(at))
    try {
      await done
      text += Buffer.concat(chunks).toString('utf8')
    } catch {
      // A trailing partial frame is expected while a session is live.
    }
  }
  return text
}

const sessionFiles = []
if (existsSync(SESSIONS)) {
  const cutoff = Date.now() - days * 86_400_000
  for (const workspace of readdirSync(SESSIONS, { withFileTypes: true })) {
    if (!workspace.isDirectory()) continue
    const workspacePath = join(SESSIONS, workspace.name)
    for (const session of readdirSync(workspacePath, { withFileTypes: true })) {
      if (!session.isDirectory()) continue
      const file = join(workspacePath, session.name, 'session.v4.jsonl.zstd')
      if (!existsSync(file)) continue
      const stat = statSync(file)
      if (stat.mtimeMs < cutoff || stat.size > 8 * 1024 * 1024) continue
      sessionFiles.push({ file, mtime: stat.mtimeMs })
    }
  }
}
sessionFiles.sort((a, b) => b.mtime - a.mtime)
const scanned = sessionFiles.slice(0, maxSessions)

let turnsCompleted = 0
let turnsErrored = 0
let turnsOther = 0
let recentTurns = 0
let recentErrored = 0
let lastErrorAt = 0
let crossInjected = 0
let guardInjected = 0
let sessionsRead = 0
let zstdUnavailable = false

for (const entry of scanned) {
  const text = await decodeFrames(readFileSync(entry.file))
  if (text === null) {
    zstdUnavailable = true
    break
  }
  sessionsRead += 1
  for (const line of text.split('\n')) {
    if (!line.includes('"turn/end"') && !line.includes('"kind":"zhishu-memory"') && !line.includes('"kind":"ctx-guard"')) continue
    let event
    try {
      event = JSON.parse(line)
    } catch {
      continue
    }
    if (event.type === 'turn/end') {
      const kind = event.data?.reason?.kind ?? 'unknown'
      if (kind === 'completed') turnsCompleted += 1
      else if (kind === 'error') {
        turnsErrored += 1
        lastErrorAt = Math.max(lastErrorAt, Number(event.time) || 0)
      } else turnsOther += 1
      if (Number(event.time) >= since) {
        recentTurns += 1
        if (kind === 'error') recentErrored += 1
      }
    } else if (event.type === 'user/message') {
      if (event.data?.source?.kind === 'zhishu-memory') crossInjected += 1
      if (event.data?.source?.kind === 'ctx-guard') guardInjected += 1
    }
  }
}

/* ── 记分卡 ───────────────────────────────────────────────── */
/* 判定看最近 `--hours` 窗口，历史全量只作对照：老故障不该永远压着"现在好不好"。 */

const rows = []
const add = (name, value, verdict, note = '') => rows.push({ name, value, verdict, note })

const rate = (ok, total) => (total === 0 ? null : ok / total)

add('插件加载行', `${loads.length} 次`, loads.length > 0 ? 'PASS' : 'FAIL', loads.at(-1)?.ts ?? '没有 plugin-loaded 记录')

const gateRate = rate(recent.hit + recent.empty, recent.gateTotal)
add('记忆检索可用率', gateRate === null ? '无数据' : `${round(gateRate * 100, 1)}%`,
  gateRate === null ? 'WARN' : gateRate >= 0.9 ? 'PASS' : gateRate >= 0.7 ? 'WARN' : 'FAIL',
  `近 ${hours}h ${recent.gateTotal} 次：命中 ${recent.hit} / 无命中 ${recent.empty} / 失败 ${recent.failed}`)
const hitRate = rate(recent.hit, recent.hit + recent.empty)
add('命中率（参考，不是越高越好）', hitRate === null ? '无数据' : `${round(hitRate * 100, 1)}%`,
  'PASS', '命中=真查到相关记录；无命中=库里确实没有')
const gateP95 = percentile(recent.ms, 95)
add('检索 P95 延迟', `${round(gateP95)} ms`,
  gateP95 === null ? 'WARN' : gateP95 <= 3000 ? 'PASS' : gateP95 <= 8000 ? 'WARN' : 'FAIL',
  `P50 ${round(percentile(recent.ms, 50))} ms`)
add('每轮注入体积', recent.chars.length === 0 ? '无数据' : `${Math.round(recent.chars.reduce((a, b) => a + b, 0) / recent.chars.length)} 字`,
  'PASS', `最大 ${recent.chars.length ? Math.max(...recent.chars) : 0} 字`)

const writeRate = rate(recent.ok, recent.writes)
add('写回成功率', writeRate === null ? '无数据' : `${round(writeRate * 100, 1)}%`,
  writeRate === null ? 'WARN' : writeRate >= 0.95 ? 'PASS' : writeRate >= 0.8 ? 'WARN' : 'FAIL',
  `近 ${hours}h ${recent.writes} 次写回，${recent.skipped} 次跳过，重试 ${recent.retried} 次`)
add('写回 P95 延迟', `${round(percentile(recent.writeMs, 95))} ms`, 'PASS',
  `P50 ${round(percentile(recent.writeMs, 50))} ms`)

const lastLoadedAt = Math.max(0, ...loads.map((row) => Date.parse(row.ts) || 0))
// 一次插件加载就是一次冷启动：比它更早的 error 是上一代代码留下的，不该压着现在。
const staleErrors = lastErrorAt > 0 && lastLoadedAt > 0 && lastErrorAt < lastLoadedAt
const errorRate = rate(recentErrored, recentTurns)
add('回合收尾错误率', errorRate === null ? '无数据' : `${round(errorRate * 100, 1)}%`,
  errorRate === null ? 'WARN' : recentErrored === 0 || staleErrors ? 'PASS' : errorRate <= 0.01 ? 'PASS' : errorRate <= 0.05 ? 'WARN' : 'FAIL',
  zstdUnavailable
    ? '当前 Node 不支持 zstd，这一层没测'
    : staleErrors
      ? `近 ${hours}h 的 ${recentErrored} 个 error 全部早于最近一次插件加载（${new Date(lastLoadedAt).toLocaleString()}）`
      : `近 ${hours}h ${recentTurns} 个回合：error ${recentErrored}；全量 ${turnsCompleted + turnsErrored + turnsOther} 回合里 error ${turnsErrored}`)

add('历史里的记忆注入', `${crossInjected} 条`, 'PASS', `另有 ${guardInjected} 条预算自检提示`)
add('预算自检报错', `${guardErrors.length} 行`, guardErrors.length === 0 ? 'PASS' : 'FAIL',
  guardErrors.length ? String(guardErrors.at(-1).error).slice(0, 80) : `${guardNotes} 次正常注入`)
add('上下文编排', `${recent.plans} 次`, 'PASS',
  recent.lastPlan ? `最近动作 ${recent.lastPlan.action ?? '失败'}` : '近窗口内没触发')

const settings = existsSync(SETTINGS) ? JSON.parse(readFileSync(SETTINGS, 'utf8')) : {}
add('运行参数覆盖', Object.keys(settings).length === 0 ? '全部出厂值' : Object.keys(settings).join(', '), 'PASS',
  existsSync(SETTINGS) ? SETTINGS : '没有 settings.json，走 loader config')

/* ── 输出 ─────────────────────────────────────────────────── */

const verdict = rows.some((row) => row.verdict === 'FAIL') ? 'FAIL' : rows.some((row) => row.verdict === 'WARN') ? 'WARN' : 'PASS'
const summarize = (stats) => ({
  gate: stats.gateTotal,
  hit: stats.hit,
  empty: stats.empty,
  failed: stats.failed,
  p50: percentile(stats.ms, 50),
  p95: percentile(stats.ms, 95),
  writes: stats.writes,
  writeOk: stats.ok,
  retried: stats.retried,
})

if (asJson) {
  console.log(JSON.stringify({
    at: new Date().toISOString(),
    verdict,
    hours,
    days,
    sessions: sessionsRead,
    rows,
    numbers: {
      window: summarize(recent),
      lifetime: summarize(lifetime),
      turns: {
        windowTotal: recentTurns,
        windowError: recentErrored,
        lifetimeCompleted: turnsCompleted,
        lifetimeError: turnsErrored,
        lifetimeOther: turnsOther,
      },
      injected: { memory: crossInjected, guard: guardInjected },
    },
  }, null, 2))
} else {
  const width = Math.max(...rows.map((row) => row.name.length)) + 2
  console.log(`\n智枢记忆套件 · 有效性体检`)
  console.log(`判定窗口：最近 ${hours} 小时（对照 ${days} 天全量，${sessionsRead} 个会话日志）\n`)
  for (const row of rows) {
    console.log(`${row.verdict === 'PASS' ? '✓' : row.verdict === 'WARN' ? '!' : '✗'} ${row.name.padEnd(width)}${String(row.value).padEnd(16)}${row.note}`)
  }
  const failed = rows.filter((row) => row.verdict === 'FAIL').length
  const warned = rows.filter((row) => row.verdict === 'WARN').length
  console.log(`\n结论：${failed ? `${failed} 项 FAIL` : warned ? `${warned} 项 WARN` : '全部通过'}（✗ FAIL / ! WARN / ✓ PASS）`)
  console.log('口径：检索可用率=命中或明确无命中 ÷ 全部 gate（4xx/超时算失败）；回合错误率=会话日志 turn/end.reason.kind=error 的占比。')
  console.log(`历史全量对照：gate ${lifetime.gateTotal} 次（失败 ${lifetime.failed}），写回 ${lifetime.writes} 次（成功 ${lifetime.ok}）。\n`)
}

process.exit(verdict === 'FAIL' ? 1 : 0)
