/**
 * 内置汇集（collect）· 注册表与执行器
 *
 * 目标：插件自己把本机各 AI 工具的会话与记忆增量同步进记忆库，**不依赖
 * omnihist、不调外部 CLI、不上传别处**。只读本机文件。
 *
 * 落库有两条路：
 *   1. **本地直写**（首选）：配了 `pgContainer` 且 docker 可用时，用
 *      `docker exec psql` 的 COPY 把消息批量写进 `mem.conv_message`（口径与
 *      `tools/omnihist-migrate.mjs` 一致，见 `pg.js`）。约 5000 条/秒，不再受
 *      服务端单条 127ms 的拖累。
 *   2. 回落：拿不到本地库时才逐条 POST `/ingest` 给记忆服务。
 *   文档（Markdown 记忆）始终走 `POST {serviceUrl}/remember`。
 *
 * 契约（见 docs/COLLECT.md）：
 *   适配器 = `{ id, label, detect(env), scan(state, env) }`，纯对象，无 class。
 *   `scan` 返回 `{ sessions, docs, next, pending, skipped }`：
 *     sessions[i] = { externalId, title, cwd, createdAt, messages: [{seq, role, kind, text, time}] }
 *     next        = { files: {...}, documents: {...} }  ← 只有上传全部成功才写回状态
 *
 * 三条护栏：
 *   - 单文件 > 64 MB 跳过（适配器里判断）；
 *   - 单次每工具 ≤ 500 万条、总计 ≤ 1000 万条消息，超出留到下一轮；
 *   - 单次运行加锁，绝不并发（定时器与手动点同时来也只有一个真跑）。
 *
 * 跳过已存在：本地直写时先查 mem 里每个会话的消息数（conv_message + 未对账的
 * live_event），≥ 本轮本地条数就只推进状态、不发任何 HTTP/写库请求，所以「已经
 * 全同步过」的一轮只剩扫描 + 一次计数查询。
 *
 * 失败只写 `audit.jsonl`、返回 `ok:false`，不抛给宿主；状态不前进，下一轮重试
 * （本地直写按 (conversation_id, seq, content_hash) 幂等，/ingest 按 `event_id`
 * 去重，重跑都安全）。
 */
import { appendFileSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

import { readSettings } from '../settings.js'
import dsh from './adapters/dsh.js'
import workbuddy from './adapters/workbuddy.js'
import claudeCode from './adapters/claude-code.js'
import codex from './adapters/codex.js'
import { conversationTotal, localStore, readCounts, writeSessions } from './pg.js'
import { lastRunOf, readState, toolState, writeState } from './state.js'
import { sha256 } from './util.js'

/** 注册表；顺序就是面板与审计里的顺序。 */
export const ADAPTERS = [dsh, workbuddy, claudeCode, codex]

/** 合法的适配器 id。 */
export const TOOL_IDS = ADAPTERS.map((adapter) => adapter.id)

/** 单次运行每个工具的消息上限。 */
export const MAX_TOOL_MESSAGES = 5_000_000

/** 单次运行全部工具的消息上限。 */
export const MAX_TOTAL_MESSAGES = 10_000_000

/** 单次运行最多处理的 Markdown 文档数（文档不进消息配额）。 */
export const MAX_DOCS_PER_RUN = 200

const DEFAULT_SERVICE_URL = 'http://127.0.0.1:41012'
const REQUEST_TIMEOUT_MS = 15_000
const HEALTH_TIMEOUT_MS = 2500
const MAX_CONSECUTIVE_FAILURES = 3

/** 当前正在跑的那次（null = 空闲）；用模块级变量实现进程内互斥。 */
let running = null

/**
 * Normalise the configured tool list.
 * Unknown ids are dropped; an empty value means "all known tools".
 * @param {unknown} value - Comma string or array.
 * @returns {string[]} Adapter ids in registry order.
 */
export function resolveTools(value) {
  if (value === undefined || value === null) return [...TOOL_IDS]
  const list = Array.isArray(value) ? value : String(value).split(',')
  const wanted = list.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean)
  if (wanted.length === 0) return [...TOOL_IDS]
  return TOOL_IDS.filter((id) => wanted.includes(id))
}

/**
 * Resolve one run's environment. Everything stays relative to `home`; no
 * absolute machine path is baked into the module.
 * @param {Record<string, any>} [input] - Caller overrides.
 * @returns {{home: string, statePath: string, auditPath: string, serviceUrl: string, tools: string[], pg: {container: string, user: string, database: string}}} Environment.
 */
function environment(input = {}) {
  const home = input.home || homedir()
  const statePath = input.statePath || join(home, '.dsh', 'zhishu-memory', 'collect-state.json')
  const settings = readSettings()
  // 设置表里没有 serviceUrl 这一项（它是 mem-float 的 loader config）；
  // 仍然先看 readSettings()，这样以后加了这个键也不用改这里。
  const rawUrl = input.serviceUrl || settings.serviceUrl || DEFAULT_SERVICE_URL
  // 本地直写用的 pg 目标来自设置（工作台 → 配置 → pg16 容器名）；留空即只走 /ingest。
  const container = input.pgContainer !== undefined ? input.pgContainer : settings.pgContainer
  return {
    home,
    statePath,
    auditPath: join(dirname(statePath), 'audit.jsonl'),
    serviceUrl: String(rawUrl).replace(/\/+$/, ''),
    tools: resolveTools(input.tools !== undefined ? input.tools : settings.collectTools),
    pg: {
      container: String(container || '').trim(),
      user: String(settings.pgUser || 'postgres').trim() || 'postgres',
      database: String(settings.pgDatabase || 'mem').trim() || 'mem',
    },
  }
}

/**
 * Append one audit row; never throws.
 * @param {{auditPath: string}} env - Environment.
 * @param {Record<string, unknown>} row - Row fields.
 * @returns {void}
 */
function audit(env, row) {
  try {
    mkdirSync(dirname(env.auditPath), { recursive: true })
    appendFileSync(env.auditPath, `${JSON.stringify({ ts: new Date().toISOString(), ...row })}\n`, 'utf8')
  } catch {
    // 审计写不进去也不能影响汇集本身
  }
}

/**
 * POST one JSON body with a hard timeout.
 * @param {string} url - Absolute endpoint.
 * @param {Record<string, unknown>} payload - Body.
 * @param {number} [timeoutMs] - Give-up delay.
 * @returns {Promise<{ok: boolean, status?: number, error?: string, data?: any}>} Outcome.
 */
async function postJson(url, payload, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    const text = await response.text()
    let data = null
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      data = null
    }
    const rejected = data && data.ok === false
    return {
      ok: response.ok && !rejected,
      status: response.status,
      data,
      error: response.ok ? (rejected ? String(data.error || 'service rejected') : undefined) : `http ${response.status}`,
    }
  } catch (error) {
    return {
      ok: false,
      error: error && error.name === 'AbortError' ? `timeout(${timeoutMs}ms)` : String((error && error.message) || error),
    }
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Probe the memory service before scanning, so a down service fails fast
 * instead of timing out once per message.
 * @param {string} serviceUrl - Origin.
 * @returns {Promise<{ok: boolean, error?: string}>} Reachability.
 */
async function probeService(serviceUrl) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS)
  try {
    const response = await fetch(`${serviceUrl}/health`, { signal: controller.signal, cache: 'no-store' })
    if (!response.ok) return { ok: false, error: `http ${response.status}` }
    return { ok: true }
  } catch (error) {
    return {
      ok: false,
      error: error && error.name === 'AbortError' ? `timeout(${HEALTH_TIMEOUT_MS}ms)` : String((error && error.message) || error),
    }
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Upload one session's new messages.
 * Stops after a few consecutive failures so an unreachable service does not
 * stall the whole run for hours.
 * @param {{id: string}} adapter - Adapter.
 * @param {any} session - Session from scan.
 * @param {{serviceUrl: string}} env - Environment.
 * @returns {Promise<{sent: number, failures: number, error: string|null, stopped: boolean}>} Outcome.
 */
async function uploadSession(adapter, session, env) {
  const externalId = typeof session.externalId === 'string' ? session.externalId.trim() : ''
  if (!externalId) return { sent: 0, failures: 0, error: null, stopped: false }
  const messages = (Array.isArray(session.messages) ? session.messages : [])
    .filter((message) => message && typeof message.text === 'string' && message.text)
  let sent = 0
  let failures = 0
  let error = null
  let stopped = false
  // 并发上传：逐条串行时 dsh 首轮要几分钟（实测 5 分钟还被客户端超时打断）。
  // 6 路实测只有 21 条/秒（服务端单条约 127ms），提到 24 路把回填压到可接受区间；
  // 真正的快车道是本地 COPY（移植自 tools/omnihist-migrate.mjs，约 5000 条/秒）。
  const CONCURRENCY = 24
  let cursor = 0
  const worker = async () => {
    while (!stopped) {
      const index = cursor
      cursor += 1
      if (index >= messages.length) return
      const message = messages[index]
      const seq = Number.isFinite(Number(message.seq)) ? Number(message.seq) : 0
      const role = ['user', 'assistant', 'tool', 'system'].includes(message.role) ? message.role : 'user'
      const eventId = sha256(`${adapter.id}|${externalId}|${seq}|${role}`)
      const outcome = await postJson(`${env.serviceUrl}/ingest`, {
        source: adapter.id,
        conversation_id: `${adapter.id}:${externalId}`,
        role,
        text: message.text,
        title: typeof session.title === 'string' ? session.title : '',
        event_id: eventId,
        meta: {
          via: 'dsh-zhishu-memory-collect',
          tool: adapter.id,
          session: externalId,
          seq,
          time: message.time === undefined ? null : message.time,
        },
      })
      if (outcome.ok) {
        sent += 1
        failures = 0
        error = null
        continue
      }
      failures += 1
      error = outcome.error || (outcome.status ? `http ${outcome.status}` : 'ingest failed')
      if (failures >= MAX_CONSECUTIVE_FAILURES) {
        stopped = true
        return
      }
    }
  }
  const workers = Math.max(1, Math.min(CONCURRENCY, messages.length))
  await Promise.all(Array.from({ length: workers }, worker))
  return { sent, failures, error, stopped }
}

/**
 * Upload one Markdown document through `/remember`.
 * @param {string} tool - Adapter id.
 * @param {any} doc - Document from scan.
 * @param {{serviceUrl: string}} env - Environment.
 * @returns {Promise<{ok: boolean, error?: string}>} Outcome.
 */
async function uploadDoc(tool, doc, env) {
  const text = typeof doc.text === 'string' ? doc.text : ''
  if (!text) return { ok: true }
  const path = typeof doc.path === 'string' ? doc.path : ''
  const outcome = await postJson(`${env.serviceUrl}/remember`, {
    text,
    kind: 'doc',
    title: path || (typeof doc.title === 'string' ? doc.title : ''),
    topics: '',
    meta: {
      via: 'dsh-zhishu-memory-collect',
      tool,
      path,
    },
  })
  return { ok: outcome.ok, error: outcome.error }
}

/**
 * Scan and upload one tool.
 * @param {{id: string, label: string, scan: Function}} adapter - Adapter.
 * @param {{serviceUrl: string, pg?: Record<string, any>}} env - Environment.
 * @param {any} state - Whole state (mutated only on a clean run).
 * @param {number} remaining - Remaining total message budget.
 * @param {{store?: Record<string, any>, allowDocs?: boolean}} [context] - Local writer availability + docs gate.
 * @returns {Promise<Record<string, any>>} Per-tool report.
 */
async function collectOne(adapter, env, state, remaining, context = {}) {
  const startedAt = Date.now()
  const stored = toolState(state, adapter.id)
  let local = context.store && context.store.ok ? context.store.local : null
  const report = {
    id: adapter.id,
    label: adapter.label,
    ok: false,
    mode: local ? 'pg' : 'http',
    sessions: 0,
    messages: 0,
    docs: 0,
    existing: 0,
    mismatch: 0,
    pending: 0,
    skipped: 0,
    errors: [],
    ms: 0,
  }
  const limit = Math.max(0, Math.min(MAX_TOOL_MESSAGES, remaining))
  if (limit <= 0) {
    report.ok = true
    report.pending = Number(stored.pending) || 0
    report.errors.push('总量已达上限，留到下一轮')
    report.ms = Date.now() - startedAt
    return report
  }

  let scan
  try {
    scan = await adapter.scan(stored, { ...env, limit })
  } catch (error) {
    report.errors.push(String((error && error.message) || error))
    report.ms = Date.now() - startedAt
    audit(env, {
      event: 'collect',
      tool: adapter.id,
      sessions: 0,
      messages: 0,
      ok: false,
      ms: report.ms,
      error: report.errors[0],
    })
    return report
  }

  const sessions = Array.isArray(scan && scan.sessions) ? scan.sessions : []
  const docs = Array.isArray(scan && scan.docs) ? scan.docs : []
  const next = scan && scan.next && typeof scan.next === 'object' ? scan.next : {}
  report.pending = Number(scan && scan.pending) || 0
  report.skipped = Number(scan && scan.skipped) || 0
  report.sessions = sessions.length

  // ── 跳过已存在：一次查询拿到本次全部会话在 mem 里的消息数 ──────────────
  // 计数读不到（mem 表缺失 / 权限 / psql 出错）就把本地直写关掉、整轮回落 /ingest，
  // 而不是猜「都已有」或「都没有」。
  let counts = null
  let foreign = null
  if (local && sessions.length > 0) {
    const ids = []
    for (const session of sessions) {
      const externalId = session && typeof session.externalId === 'string' ? session.externalId.trim() : ''
      if (externalId) ids.push(`${adapter.id}:${externalId}`)
    }
    const found = readCounts(local, ids)
    if (found.ok) {
      counts = found.counts
      foreign = found.foreign
    } else {
      local = null
      report.mode = 'http'
      report.errors.push(`本地库计数失败，本轮回落 /ingest：${found.error || 'unknown'}`)
    }
  }

  const queued = []
  if (counts && foreign) {
    for (const session of sessions) {
      const externalId = session && typeof session.externalId === 'string' ? session.externalId.trim() : ''
      if (!externalId) continue
      const id = `${adapter.id}:${externalId}`
      const have = counts.get(id) || 0
      // mem 里的条数 ≥ 本地条数 → 这个会话已经同步过，只推进状态。
      if (have >= conversationTotal(session)) {
        report.existing += 1
        continue
      }
      // 有行、但那些行是别的导入方写的（seq 口径不同）→ 不按 seq 追加，
      // 否则同一段历史会被写成乱序 + 内容重复。标记已处理，留给审计。
      if (have > 0 && (foreign.get(id) || 0) > 0) {
        report.mismatch += 1
        continue
      }
      queued.push(session)
    }
  } else {
    queued.push(...sessions)
  }

  let messageCount = 0
  let docCount = 0
  let failures = 0
  let error = null
  let stopped = false

  if (local) {
    // 本地直写：一次 COPY 一个事务，按 (conversation_id, seq, content_hash) 幂等。
    if (queued.length > 0) {
      const outcome = writeSessions(local, adapter.id, queued)
      messageCount = outcome.messages
      if (!outcome.ok) {
        failures += 1
        error = outcome.error || 'pg write failed'
        stopped = true
      }
    }
  } else {
    for (const session of queued) {
      const outcome = await uploadSession(adapter, session, env)
      messageCount += outcome.sent
      failures += outcome.failures
      if (outcome.error) error = outcome.error
      if (outcome.stopped) {
        stopped = true
        break
      }
    }
  }

  // 消息没全部成功时先不写文档：下一轮会从旧 offset 重跑，避免 /remember 重复落库。
  // 本地直写不依赖记忆服务；服务不可达时消息照写，文档留到下一轮（状态也不推进 documents）。
  if (!stopped && failures === 0 && context.allowDocs !== false) {
    for (const doc of docs.slice(0, MAX_DOCS_PER_RUN)) {
      const outcome = await uploadDoc(adapter.id, doc, env)
      if (outcome.ok) {
        docCount += 1
        continue
      }
      failures += 1
      error = outcome.error || 'remember failed'
      break
    }
  } else if (!stopped && failures === 0 && docs.length > 0) {
    report.errors.push('记忆服务不可达，文档本轮跳过')
  }

  report.messages = messageCount
  report.docs = docCount
  report.ok = failures === 0 && !stopped
  report.ms = Date.now() - startedAt

  if (report.ok) {
    state.tools[adapter.id] = {
      ...stored,
      files: next.files && typeof next.files === 'object' ? next.files : stored.files,
      // 文档没上传成功（服务不可达）时不能推进 documents，否则下一轮以为已经记过。
      documents: context.allowDocs !== false && next.documents && typeof next.documents === 'object'
        ? next.documents
        : stored.documents,
      lastRun: new Date().toISOString(),
      synced: (Number(stored.synced) || 0) + messageCount,
      docs: (Number(stored.docs) || 0) + docCount,
      mismatch: report.mismatch,
      pending: report.pending,
      lastError: null,
    }
  } else {
    // 状态不前进：下一轮从旧 offset 重读；本地直写按 (会话, seq, hash) 幂等，
    // /ingest 按 event_id 去重，重跑都安全。
    state.tools[adapter.id] = {
      ...stored,
      lastAttempt: new Date().toISOString(),
      lastError: error || 'upload failed',
      pending: report.pending,
    }
    report.errors.push(error || 'upload failed')
  }

  audit(env, {
    event: 'collect',
    tool: adapter.id,
    sessions: report.sessions,
    messages: report.messages,
    docs: report.docs,
    ok: report.ok,
    mode: report.mode,
    existing: report.existing,
    ...(report.mismatch > 0 ? { mismatch: report.mismatch } : {}),
    ms: report.ms,
    ...(report.errors.length > 0 ? { error: report.errors[0] } : {}),
  })
  return report
}

/**
 * Red/green data for every registered tool (does not upload anything).
 * @param {Record<string, any>} [input] - `{ home, statePath, serviceUrl, tools }`.
 * @returns {Promise<Record<string, any>>} Status payload.
 */
export async function collectStatus(input = {}) {
  const env = environment(input)
  const state = readState(env.statePath)
  const tools = []
  for (const adapter of ADAPTERS) {
    const stored = toolState(state, adapter.id)
    let detected = { found: false, root: '', sessions: 0 }
    let error = null
    try {
      detected = (await adapter.detect(env)) || detected
    } catch (caught) {
      error = String((caught && caught.message) || caught)
    }
    tools.push({
      id: adapter.id,
      label: adapter.label,
      enabled: env.tools.includes(adapter.id),
      found: Boolean(detected.found),
      root: detected.root || '',
      localSessions: Number(detected.sessions) || 0,
      syncedSessions: Object.keys(stored.files || {}).length,
      syncedMessages: Number(stored.synced) || 0,
      syncedDocs: Number(stored.docs) || 0,
      // 上一轮因为「mem 里这一行不是汇集写的、seq 口径不同」而没有追加的会话数。
      mismatch: Number(stored.mismatch) || 0,
      pending: Number(stored.pending) || 0,
      lastRun: stored.lastRun || null,
      lastError: stored.lastError || null,
      error,
    })
  }
  return {
    ok: true,
    busy: Boolean(running),
    serviceUrl: env.serviceUrl,
    statePath: env.statePath,
    selected: env.tools,
    // 有 pgContainer 走本地直写（/ingest 只是回落）；没有就仍然是 HTTP。
    storage: env.pg.container ? 'pg' : 'http',
    tools,
  }
}

/**
 * Newest `lastRun` among the selected tools (used by the scheduler).
 * @param {Record<string, any>} [input] - Same input as {@link collectNow}.
 * @returns {string|null} ISO timestamp, or null when never run.
 */
export function collectLastRun(input = {}) {
  const env = environment(input)
  return lastRunOf(readState(env.statePath), env.tools)
}

/**
 * Run one collection pass over the selected tools.
 * Manual calls run even when `collectEnabled` is off; scheduling decides that.
 * @param {Record<string, any>} [input] - `{ home, statePath, serviceUrl, tools, now }`.
 * @returns {Promise<Record<string, any>>} Run report.
 */
export async function collectNow(input = {}) {
  if (running) {
    return { ok: false, busy: true, error: '已有一次汇集在跑', results: [], messages: 0, docs: 0, sessions: 0 }
  }
  const env = environment(input)
  const state = readState(env.statePath)
  running = { startedAt: new Date().toISOString() }
  const startedAt = Date.now()
  const results = []
  let remaining = MAX_TOTAL_MESSAGES
  try {
    // 先看有没有本地库：有就走 COPY 直写，没有才要求记忆服务可达。
    const store = localStore(env)
    const health = await probeService(env.serviceUrl)
    if (!health.ok && !store.ok) {
      const ms = Date.now() - startedAt
      const reason = store.error && env.pg.container ? `${health.error}；${store.error}` : String(health.error)
      audit(env, { event: 'collect', tool: '*', sessions: 0, messages: 0, ok: false, ms, error: reason })
      return {
        ok: false,
        busy: false,
        error: `记忆服务不可达：${env.serviceUrl}（${reason}）`,
        results: [],
        messages: 0,
        docs: 0,
        sessions: 0,
        ms,
        serviceUrl: env.serviceUrl,
        statePath: env.statePath,
      }
    }
    const context = { store, allowDocs: health.ok }

    for (const id of env.tools) {
      const adapter = ADAPTERS.find((item) => item.id === id)
      if (!adapter) continue
      let report
      try {
        report = await collectOne(adapter, env, state, remaining, context)
      } catch (error) {
        report = {
          id: adapter.id,
          label: adapter.label,
          ok: false,
          mode: store.ok ? 'pg' : 'http',
          sessions: 0,
          messages: 0,
          docs: 0,
          existing: 0,
          mismatch: 0,
          pending: 0,
          skipped: 0,
          errors: [String((error && error.message) || error)],
          ms: 0,
        }
        audit(env, { event: 'collect', tool: adapter.id, sessions: 0, messages: 0, ok: false, ms: 0, error: report.errors[0] })
      }
      results.push(report)
      remaining = Math.max(0, remaining - (Number(report.messages) || 0))
      writeState(state, env.statePath)
    }

    const totals = results.reduce((sum, item) => ({
      messages: sum.messages + (Number(item.messages) || 0),
      docs: sum.docs + (Number(item.docs) || 0),
      sessions: sum.sessions + (Number(item.sessions) || 0),
      existing: sum.existing + (Number(item.existing) || 0),
      mismatch: sum.mismatch + (Number(item.mismatch) || 0),
      pending: sum.pending + (Number(item.pending) || 0),
    }), { messages: 0, docs: 0, sessions: 0, existing: 0, mismatch: 0, pending: 0 })
    const finishedAt = new Date().toISOString()
    return {
      ok: results.length > 0 && results.every((item) => item.ok),
      busy: false,
      results,
      ...totals,
      mode: store.ok ? 'pg' : 'http',
      ms: Date.now() - startedAt,
      startedAt: running ? running.startedAt : finishedAt,
      finishedAt,
      serviceUrl: env.serviceUrl,
      statePath: env.statePath,
    }
  } finally {
    running = null
  }
}
