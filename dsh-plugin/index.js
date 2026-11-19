/**
 * Zhishu memory bridge.
 *
 * Two hooks, one store: the owner's pg16 memory through `memory-mcp` (127.0.0.1:41012).
 *
 * - `agent/pre-step` (step 1 of each turn): POST /gate with the user prompt. The
 *   service logs the prompt and returns the matching decisions/pitfalls. The
 *   result is appended to the messages entering the step, tagged with source
 *   `zhishu-memory`, so the first model call of the turn already sees it.
 * - `agent/turn-stopping`: double write. The verbatim answer (up to 8000
 *   chars) goes to `mem.conv` via POST /ingest for faithful replay; a digest
 *   (conclusions, facts, evidence pointers, at most 300 chars) goes to
 *   `mem.doc` via POST /remember for retrieval. Both audit lines carry
 *   conversation_id, turn, raw_chars and digest_chars. Turns that store
 *   nothing are logged as `write-back-skipped` with a reason.
 *
 * Nothing here depends on the harness's own memory; the store is the MCP service.
 * Failures are logged and never block the turn.
 */
import { appendFileSync, mkdirSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { homedir } from 'node:os'
import { join } from 'node:path'

import { snapshotHistory, selectSurfaceRange, hashMessages } from './lib/ctx-plan.js'
import { readSettings } from './lib/settings.js'
import {
  OUTCOME,
  THROTTLE_AFTER,
  classify,
  failClass,
  emptyText,
  timeoutText,
  failedText,
  throttleText,
  recoveredText,
} from './lib/degrade.js'

/** No service injection: the bridge only listens on agent events. */
export const inject = []

/** Defaults; a profile patch can override any of them. */
const DEFAULTS = {
  baseUrl: 'http://127.0.0.1:41012',
  limit: 3,
  // 4s was cutting real turns off: 5/101 write-backs aborted at 4000-4014ms
  // while the P50 is 280ms, so the wall clock is a tail problem, not slowness.
  timeoutMs: 10_000,
  retries: 1,
  enabled: true,
  writeBack: true,
  // 上下文编排（2026-11-17）：默认只观测，MEM_CTX_SWAP=1 才真替换。
  ctxPlan: true,
  ctxSwap: process.env.MEM_CTX_SWAP === '1',
  // 阈值判定只用宿主 tokenMeter 的口径（projectedTokens / contextWindow）：
  //   DSH 按 4 字符/token 估，本服务按 1.7 字符/token 估，我们自己的估算只用于
  //   「选哪几段丢」的相对排序，不参与「要不要压」。0.2 是 11-16 针对中文被低估
  //   实测调过的值，别再用我们自己的口径换算一个「等价阈值」出来。
  ctxThreshold: Number(process.env.MEM_CTX_THRESHOLD) || 0.2,
  ctxKeepRecent: 6,
  ctxBudget: 128_000,
  // 快照上限：一次 pre-step 只传最近这么多条，避免把几 MB 历史塞进秒级路径。
  ctxMaxMessages: 300,
  ctxMaxChars: 1500,
  // 摘要调用超时：超时就放弃本次压缩、原样返回，交给内置 request-error 兜底。
  ctxTimeoutMs: 20_000,
  // 溢出兜底：auto:false 后内置不再注册 request-error 监听器，这一道由我们接管。
  ctxOverflowRecovery: process.env.MEM_CTX_OVERFLOW !== '0',
  // 上下文编排走哪个服务实例：默认本地 41016（连本地快照库）。
  // 生产 41012 还跑着旧代码、没有 /context/plan，指过去只会 404。
  ctxBaseUrl: process.env.MEM_CTX_BASE_URL || 'http://127.0.0.1:41016',
}

/**
 * Resolve where this plugin keeps its audit log.
 *
 * Always the shared `~/.dsh/zhishu-memory` path: the memory store itself is one
 * shared pg16 for every bot and profile, so the audit trail must be one file too
 * (a profile-scoped path split the evidence across two logs on 2026-11-14).
 * @returns {string} Absolute directory path.
 */
function stateDirectory() {
  // 测试时（tests/*.mjs）把审计目录指到 TEMP，免得 harness 的假记录混进真实
  // audit.jsonl，把 verify 的「插件加载行」匹配带偏（2026-11-17 踩过）。
  const override = process.env.MEM_AUDIT_DIR
  if (override) return override
  return join(homedir(), '.dsh', 'zhishu-memory')
}

/**
 * Append one audit line; logging failures never change behavior.
 * @param {string} dir - State directory.
 * @param {Record<string, unknown>} entry - Audit fields.
 */
function audit(dir, entry) {
  try {
    mkdirSync(dir, { recursive: true })
    appendFileSync(join(dir, 'audit.jsonl'), `${JSON.stringify(entry)}\n`, 'utf8')
  } catch {
    // The bridge still works without its log.
  }
}

/**
 * Extract text from one content part.
 * @param {any} part - Content part.
 * @returns {string} Text or an empty string.
 */
function partText(part) {
  if (!part || typeof part !== 'object') return ''
  return typeof part.text === 'string' ? part.text : ''
}

/**
 * Extract plain text from a message content value.
 * @param {unknown} content - String or content-part array.
 * @returns {string} Joined text.
 */
function contentText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.map(partText).join('')
}

/**
 * Find the last real user prompt in a claimed message batch.
 * @param {ReadonlyArray<any>} messages - Messages entering the step.
 * @returns {string} Prompt text, or an empty string.
 */
function lastUserText(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message && message.role === 'user') {
      const text = contentText(message.content).trim()
      if (text) return text
    }
  }
  return ''
}

/**
 * Find the last assistant answer in the derived model history.
 * @param {ReadonlyArray<any>} messages - Derived messages.
 * @returns {string} Answer text, or an empty string.
 */
function lastAssistantText(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message && message.role === 'assistant') {
      const text = contentText(message.content).trim()
      if (text) return text
    }
  }
  return ''
}

/**
 * Build the stable conversation key for one agent.
 * @param {any} agent - Live agent.
 * @returns {string} Conversation id understood by the memory service.
 */
function sessionKey(agent) {
  const id = agent && agent.session && agent.session.id
  return id ? `dsh:${id}` : 'dsh:unknown'
}

/**
 * POST JSON with a hard timeout; never throws.
 * @param {string} url - Target URL.
 * @param {Record<string, unknown>} body - JSON body.
 * @param {number} timeoutMs - Abort timeout.
 * @returns {Promise<any>} Parsed JSON, or a failure object.
 */
async function postJson(url, body, timeoutMs) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
      signal: controller.signal,
    })
    if (!response.ok) return { ok: false, status: response.status }
    return await response.json()
  } catch (error) {
    // timeout 打结构化标记：以前只能靠 error 字符串里有没有 "aborted" 去猜，
    // 而「超时」和「连不上」在模型侧必须区分开（老大 2026-11-18）。
    return {
      ok: false,
      error: error && error.message ? error.message : String(error),
      timeout: controller.signal.aborted === true,
    }
  } finally {
    clearTimeout(timer)
  }
}

/**
 * POST JSON with one retry on transport failure (timeouts included).
 * Idempotency is the caller's job: a retried body must carry the same
 * deterministic `event_id`, so the service dedupes instead of writing twice.
 * @param {string} url - Target URL.
 * @param {Record<string, unknown>} body - JSON body.
 * @param {number} timeoutMs - Per-attempt abort timeout.
 * @param {number} retries - Extra attempts after the first failure.
 * @returns {Promise<{data: any, attempts: number, ms: number}>} Outcome.
 */
async function postJsonRetry(url, body, timeoutMs, retries) {
  const attempts = Math.max(0, Number(retries) || 0) + 1
  const startedAt = Date.now()
  let data = null
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    data = await postJson(url, body, timeoutMs)
    if (data && data.ok) return { data, attempts: attempt, ms: Date.now() - startedAt }
    // 4xx is a real answer, not a transport hiccup: do not retry it.
    if (data && typeof data.status === 'number' && data.status >= 400 && data.status < 500) {
      return { data, attempts: attempt, ms: Date.now() - startedAt }
    }
  }
  return { data, attempts, ms: Date.now() - startedAt }
}

/**
 * Deterministic event id for one turn, so a retried write is deduped by the
 * service (`mem.live_event.event_id`) instead of stored twice.
 * @param {string} conversationId - Conversation key.
 * @param {number|null} turn - Turn number when known.
 * @param {string} role - Message role.
 * @returns {string} Stable id.
 */
function eventIdFor(conversationId, turn, role) {
  const key = `${conversationId}|${turn === null ? '-' : turn}|${role}`
  return createHash('sha256').update(key).digest('hex')
}

/**
 * Render retrieved rows as the model-facing context block.
 * @param {any} data - `/gate` response.
 * @returns {string} Markdown block, empty when nothing matched.
 */
function formatContext(data) {
  const results = Array.isArray(data && data.results) ? data.results : []
  if (results.length === 0) return ''
  const lines = [
    '## 记忆检索（zhishu-memory 自动注入）',
    `开工前从账号主 pg16 记忆库查到 ${results.length} 条相关记录。先对照下面的决定和踩过的坑；要更多再调 zhishu-memory.search。与本任务无关就忽略。`,
    '',
  ]
  results.forEach((item, index) => {
    const kind = item.kind || item.scope || ''
    const title = String(item.title || '').replace(/\s+/g, ' ').trim()
    const snippet = String(item.snippet || '').replace(/\s+/g, ' ').trim().slice(0, 300)
    const date = item.date ? ` · ${item.date}` : ''
    lines.push(`${index + 1}. [${kind}] ${title}${date}`)
    lines.push(`   ${snippet}`)
  })
  return lines.join('\n')
}

/**
 * Lines that are the model narrating its own process or addressing the user;
 * these must never enter the shared store.
 */
const DROP_ALWAYS = [
  /\b(?:i should|i need to|i'll|i will|let me|let's|let us|we should|we'll|i'm going to)\b/i,
  /\b(?:the user|user)\s+(?:asks|asked|wants|wanted|said|says|is asking)\b/i,
  /^(?:now|next|then|so)\s+(?:i|we|let|the)\b/i,
  /^(?:i|i'm|i've|we|we're|my|the message|this message|note|ps)\b/i,
  /\b(?:my final text|my final answer|my reply|my answer|my instructions|my next step)\b/i,
  /\b(?:can't be sent|cannot be sent|isn't sent|is not sent|waiting for my|that might be useful|might be useful)\b/i,
  /^(?:现在|接下来|然后|接着|首先|其次|最后|另外|顺便)(?:我|我们|先|要|看|把|去|来|再)/,
  /^(?:我|我们的|这条|这轮|现在回复|好的|嗯)/,
  /^完成(?![:：])/,
  /(?:回复用户|告诉用户|给用户回|需要回复|再回复|不要回复|我的回答|我的回复|回复会回到|回到他那里|回到它那里)/,
  /^(?:完成|done|好了)[。.！!，,\s]*$/i,
  /^(?:步骤|step)\s*\d+/i,
  /^(?:todo|fixme|note to self)\b/i,
  /^the (?:system|prompt|instructions?)\b/i,
  /^(?:noted|got it|understood|received)\b[.!,;，。]?$/i,
  /^(?:收到|明白了|知道了)[。.！!,，\s]*$/,
]

/** Softer process openers: dropped unless the line also carries a conclusion or evidence. */
const SOFT_PROCESS = [
  /^(?:now|next|then|so|okay|ok|alright|well|first|second|third|finally|also)\b/i,
  /^(?:现在|接下来|然后|首先|其次|最后|接着|另外|顺便)/,
]

/** A line that points at something checkable: path, command, or command output. */
const EVIDENCE = [
  /[A-Za-z]:\\/,
  /\b(?:docs|apps|api|memory-mcp|plugins|scripts|migrations|extension|packages)\/[^\s，。；]+/,
  /\.(?:py|js|mjs|ts|tsx|go|sql|md|json|jsonl|yaml|yml|csv)\b/,
  /:\d+(?:-\d+)?\b/,
  /^(?:\$ |>>> )?(?:python|pip|npm|pnpm|node|go|git|curl|psql|select|insert|create|update|drop)\b/i,
  /\b(?:exit code|PASS|FAIL|ok=|ok:|count=|rows=|status=|ms=|ipv4|127\.0\.0\.1)\b/i,
]

/** A line that states an outcome, a decision, or a verdict. */
const CONCLUSION = /(?:结论|根因|原因|决定|否决|通过|已执行|已完成|已交付|已建|已改|已修|交付|修复|落地|真实|证据|验证|建议|卡点|阻塞|唯一真源)/

/** Verdict markers: lines carrying these are promoted to the head of the digest.
 *  A closing line without one of these words is never treated as a conclusion
 *  ("这事先这样" does not get to lead the digest). */
const PRIORITY = /(?:结论|决定|根因|验收|判定|结果|建议|通过|不通过|退回|否决|口径|唯一真源|已执行|已完成|已交付|已建|已改|已修)/

/** Normalize a line for near-duplicate detection. */
function normLine(line) {
  return line.replace(/[`"'“”‘’（）()[\]{}，,。.；;：:！!？?、\s]/g, '').toLowerCase()
}

/** The claim head of a verdict line: text before its first separator. */
function claimKey(line) {
  const head = normLine(String(line).split(/[，,；;：:。.！!？?]/)[0] || '')
  return head.length >= 5 ? head.slice(0, 16) : ''
}

/** Bigram Jaccard similarity in 0..1, for de-duplicating verdict lines. */
function lineSimilarity(a, b) {
  if (!a || !b) return 0
  if (a === b || a.includes(b) || b.includes(a)) return 1
  const grams = (value) => {
    const out = new Set()
    for (let i = 0; i < value.length - 1; i += 1) out.add(value.slice(i, i + 2))
    return out
  }
  const left = grams(a)
  const right = grams(b)
  if (left.size === 0 || right.size === 0) return 0
  let shared = 0
  for (const gram of left) if (right.has(gram)) shared += 1
  return shared / (left.size + right.size - shared)
}

/**
 * Compress one answer into a memory-worthy digest: conclusions first, then
 * facts and evidence pointers, at most 300 characters.
 *
 * Order matters: taking the answer's first N characters let narration at the
 * top push the closing conclusion out (audit D-2026-11-14-02). Verdict lines
 * are therefore collected from the whole answer before anything else is added.
 * @param {string} answer - Full assistant answer.
 * @returns {string} Digest, or an empty string when nothing is worth storing.
 */
function digestAnswer(answer) {
  const verdict = []
  const evidence = []
  const rest = []
  for (const raw of String(answer || '').split(/\r?\n/)) {
    const line = raw
      .trim()
      .replace(/\*\*/g, '')
      .replace(/^[-*+>]\s+/, '')
      .replace(/^\d+[.)]\s+/, '')
      .trim()
    if (!line) continue
    if (line.startsWith('```')) continue
    if (/^#{1,6}\s/.test(raw.trim())) continue
    if (DROP_ALWAYS.some((re) => re.test(line))) continue
    const hasEvidence = EVIDENCE.some((re) => re.test(line))
    const hasConclusion = CONCLUSION.test(line)
    if (SOFT_PROCESS.some((re) => re.test(line)) && !hasEvidence && !hasConclusion) continue
    if (/[：:]$/.test(line) && !hasEvidence) continue
    if (!hasEvidence && !hasConclusion && [...line].length < 12) continue
    if (PRIORITY.test(line)) verdict.push(line)
    else if (hasEvidence || hasConclusion) evidence.push(line)
    else rest.push(line)
  }

  const out = []
  const picked = []
  let size = 0
  // Latest verdict first: closing conclusions are the ones an answer ends on.
  for (const line of [...verdict.slice().reverse(), ...evidence, ...rest]) {
    const norm = normLine(line)
    const claim = claimKey(line)
    if (
      picked.some(
        (seen) =>
          lineSimilarity(seen.norm, norm) >= 0.7 ||
          (claim !== '' && seen.claim === claim),
      )
    ) continue
    const chars = [...line]
    if (out.length > 0 && size + chars.length + 1 > 300) continue
    if (out.length === 0 && chars.length > 300) {
      out.push(chars.slice(0, 300).join(''))
      picked.push({ norm: norm.slice(0, 300), claim })
      size = 300
      break
    }
    out.push(line)
    picked.push({ norm, claim })
    size += chars.length + 1
  }

  let text = out.join('\n').trim()
  if ([...text].length > 300) text = [...text].slice(0, 300).join('').trim()
  return text
}

/**
 * Install the bridge on the agent lifecycle.
 * @param {any} ctx - Cordis plugin context.
 * @param {Record<string, unknown>} [config] - Row configuration from the patch.
 */
export function apply(ctx, config = {}) {
  const cfg = { ...DEFAULTS, ...config }
  const dir = stateDirectory()
  const state = new WeakMap()

  // 一行启动日志：重启后靠它确认插件真的加载了、上下文编排处于哪种模式。
  try {
    const line = `[zhishu-memory] loaded ctx-plan=${cfg.ctxPlan ? 'on' : 'off'}`
      + ` swap=${cfg.ctxSwap ? 'on' : 'off'} threshold=${cfg.ctxThreshold}`
      + ` ctx-base=${cfg.ctxBaseUrl || cfg.baseUrl}`
    if (ctx.logger && typeof ctx.logger.info === 'function') ctx.logger.info(line)
    else console.log(line)
    audit(dir, { ts: new Date().toISOString(), event: 'plugin-loaded', line })
  } catch {
    // A logger failure must never block loading.
  }

  /**
   * Read or create the per-agent state entry.
   * @param {any} agent - Live agent.
   * @returns {{cache: string, gatedTurn: number, lastUser: string, lastWrite: string}} Mutable state.
   */
  function entryOf(agent) {
    let entry = state.get(agent)
    if (entry === undefined) {
      entry = { cache: '', gatedTurn: -1, lastUser: '', lastWrite: '',
                gateFailStreak: 0,
                ctxTurn: -1, ctxBroken: false, ctxSwapTurn: -1 }
      state.set(agent, entry)
    }
    return entry
  }

  /**
   * 往这一步的入参上追加一段记忆上下文。
   *
   * 为什么是追加一条 `role: 'user'` 的消息、不是走 systemPrompt：现行这一层没有可用的
   * 「admission 前重读」系统提示通道（`docs/DETAIL.md` 旧文案写过 `ctx.systemPrompt.context()`，
   * 代码里从来没有这个 API，2026-11-18 核对后改正）。追加消息是这里唯一确定生效的注入点；
   * `source.kind` 打标，方便把插件注入的和真用户消息分开。
   *
   * @param {any} decision - 下游 decision（kind 必须是 enter）。
   * @param {string} text - 要注入的 Markdown。
   * @returns {any} 追加后的 decision。
   */
  function appendMemory(decision, text) {
    return {
      ...decision,
      messages: [
        ...(Array.isArray(decision.messages) ? decision.messages : []),
        {
          id: `zhishu-memory-${randomUUID()}`,
          role: 'user',
          content: [{ type: 'text', text }],
          source: { kind: 'zhishu-memory' },
        },
      ],
    }
  }

  ctx.on('agent/pre-step', async (payload, next) => {
    if (!cfg.enabled) return next()
    const agent = payload && payload.agent
    const messages = Array.isArray(payload && payload.messages) ? payload.messages : []
    const step = payload && payload.step
    const turn = payload && payload.turn
    if (!agent || step !== 1) return next()

    const decision = await next()
    if (!decision || decision.kind !== 'enter') return decision

    const entry = entryOf(agent)
    if (entry.gatedTurn === turn) return decision
    entry.gatedTurn = turn

    const query = lastUserText(messages)
    entry.lastUser = query
    if (!query) return decision

    // 每轮重读：悬浮面板改完滑杆，下一轮就按新值走，不需要重启宿主。
    const live = readSettings(cfg)
    const limit = Math.max(1, Math.min(Number(live.limit) || 3, 10))
    const conversationId = sessionKey(agent)
    const timeoutMs = Number(live.timeoutMs) || 4000
    const startedAt = Date.now()
    const data = await postJson(`${cfg.baseUrl}/gate`, {
      source: 'dsh',
      conversation_id: conversationId,
      // 服务端 /gate 已经认 turn 并写进 mem.gate_log.turn（mcp_server.py:696）。
      // 不带的话客户端 audit 与服务端 gate_log 只能按时间窗对，对不准（2026-11-18）。
      turn: typeof turn === 'number' ? turn : undefined,
      query,
      role: 'user',
      limit,
    }, timeoutMs)
    const elapsedMs = Date.now() - startedAt
    const outcome = classify(data)
    const hitIds = Array.isArray(data && data.results)
      ? data.results.map((item) => item && item.id).filter(Boolean).slice(0, 10)
      : []
    const count = Number.isFinite(Number(data && data.count)) ? Number(data.count) : hitIds.length
    const streakBefore = Number(entry.gateFailStreak) || 0

    // 命中 / 无命中：都算检索成功，但要分别说话（老大 2026-11-18：三种空必须分开）。
    if (outcome === OUTCOME.HIT || outcome === OUTCOME.EMPTY) {
      entry.gateFailStreak = 0
      entry.cache = outcome === OUTCOME.HIT ? formatContext(data) : ''
      let text = outcome === OUTCOME.EMPTY ? emptyText() : entry.cache
      // 恢复声明必须在最前面且只出现一次：节流期间模型被告知"这轮没记忆"，
      // 恢复后不说这一句，它就永远以为自己瞎着。
      if (streakBefore > 0) {
        const recovered = recoveredText({ count, after: streakBefore })
        text = text ? `${recovered}\n\n${text}` : recovered
      }
      audit(dir, {
        ts: new Date().toISOString(),
        event: 'gate',
        class: outcome,
        turn,
        conversation_id: conversationId,
        query: query.slice(0, 200),
        ids: hitIds,
        count,
        recovered: streakBefore > 0,
        streak_before: streakBefore,
        ms: elapsedMs,
        chars: text.length,
      })
      if (!text) return decision
      return appendMemory(decision, text)
    }

    // 失败 / 超时：以前这里直接 return decision，模型拿到空上下文会当成"确实没有历史"。
    // 现在必须显性化，并且每轮都记一次 audit（节流只压文案，不压记录）。
    entry.cache = ''
    entry.gateFailStreak = streakBefore + 1
    audit(dir, {
      ts: new Date().toISOString(),
      event: 'gate-failed',
      class: failClass(data),
      status: data && typeof data.status === 'number' ? data.status : undefined,
      streak: entry.gateFailStreak,
      turn,
      conversation_id: conversationId,
      query: query.slice(0, 200),
      ids: [],
      ms: elapsedMs,
      error: data && (data.error || data.status),
    })
    const text = entry.gateFailStreak >= THROTTLE_AFTER
      ? throttleText({ streak: entry.gateFailStreak })
      : outcome === OUTCOME.TIMEOUT
        ? timeoutText({ timeoutMs })
        : failedText({ status: data && data.status })
    return appendMemory(decision, text)
  })

  /**
   * Post one completion record for a trace the service already opened.
   * @param {number} traceId - Trace id returned by /context/plan.
   * @param {Record<string, unknown>} patch - Completion fields.
   */
  async function completeTrace(traceId, patch) {
    if (!(traceId > 0)) return
    await postJson(`${cfg.ctxBaseUrl || cfg.baseUrl}/context/trace/complete`, { trace_id: traceId, ...patch }, 5000)
  }

  /**
   * Run one context-plan cycle: snapshot history, ask the service, optionally
   * swap, and always leave a trace. Never throws into the pre-step path.
   *
   * Three guards live here:
   * - one attempt per turn (the caller marks `entry.ctxTurn` before calling);
   * - a summary timeout that aborts and leaves the messages untouched;
   * - a circuit breaker that stops trying for the rest of the session once the
   *   engine reports an orphaned compaction lock.
   *
   * @param {any} ctx - Cordis context.
   * @param {any} agent - Agent about to enter a step.
   * @param {any} payload - pre-step payload (turn, step, signal).
   * @param {any} entry - Per-agent mutable state.
   */
  async function runContextPlan(ctx, agent, payload, entry) {
    const startedAt = Date.now()
    const live = readSettings(cfg)
    const session = agent.session
    const conversationId = sessionKey(agent)
    const snap = snapshotHistory(session, {
      maxMessages: cfg.ctxMaxMessages,
      maxChars: cfg.ctxMaxChars,
    })
    if (!snap.messages.length) return

    // Budget: prefer the live route capacity, fall back to the configured one.
    // Pressure is judged ONLY by the host token meter. Our own chars/1.7 estimate
    // runs 2.35x this one, so it is a separate label and never drives the threshold.
    //
    // 字段名必须按源码来，不能按文档猜：ctx.tokenMeter.contextPressure() 的返回类型是
    // Pick<ContextPressureProjection, 'pressureTokens' | 'contextWindow'>（token-meter.md:114），
    // projectedTokens 只存在于客户端 projection（:58），服务端这里永远拿不到。
    // 曾经只读 projectedTokens，结果是恒为 null、静默退回字符估算——错得不报错，最坑。
    let contextWindow = null
    let measuredTokens = null
    let measuredField = null
    try {
      const meter = typeof ctx.get === 'function' ? ctx.get('tokenMeter') : null
      if (meter && typeof meter.contextPressure === 'function') {
        const projected = meter.contextPressure(session)
        if (projected && Number.isFinite(projected.contextWindow)) contextWindow = projected.contextWindow
        if (projected && Number.isFinite(projected.projectedTokens)) {
          measuredTokens = projected.projectedTokens
          measuredField = 'projectedTokens'
        } else if (projected && Number.isFinite(projected.pressureTokens)) {
          measuredTokens = projected.pressureTokens
          measuredField = 'pressureTokens'
        }
      }
    } catch {
      // A missing meter only costs precision, never the cycle.
    }
    const budget = Number.isFinite(contextWindow) && contextWindow > 0
      ? contextWindow
      : (Number(cfg.ctxBudget) || 128_000)
    const dshPct = Number.isFinite(measuredTokens) && budget > 0
      ? (measuredTokens / budget) * 100
      : null
    // 来源要能分清是哪个字段给的：dsh-tokenmeter:pressureTokens / :projectedTokens
    const pressureSource = measuredField === null ? null : `dsh-tokenmeter:${measuredField}`

    const body = {
      messages: snap.messages,
      budget_tokens: budget,
      conversation_id: conversationId,
      session_id: conversationId,
      bot: cfg.bot || '',
      turn: payload.turn,
      step: payload.step,
      model: cfg.model || '',
      keep_recent_turns: Number(live.ctxKeepRecent) || 6,
      threshold: Number(live.ctxThreshold) || 0.2,
      // 本次决策基于多少条、总共多少条：被截断时这两个数必须分开记，否则
      // 将来没法解释「为什么它没压那段本该压的」。
      messages_used: snap.messages.length,
      messages_total: snap.total,
      truncated: snap.truncated,
      // 判定口径（百分数）；拿不到就不传，服务端会退回估算并标注来源。
      ...dshPct === null ? {} : { pressure_pct: dshPct, pressure_source: pressureSource },
    }
    const data = await postJson(`${cfg.ctxBaseUrl || cfg.baseUrl}/context/plan`, body, Number(live.timeoutMs) || cfg.timeoutMs)
    const traceId = data && data.ok && data.trace_id > 0 ? data.trace_id : -1
    const plan = (data && data.plan) || {}
    const base = {
      ts: new Date().toISOString(),
      event: 'ctx-plan',
      turn: payload.turn,
      step: payload.step,
      conversation_id: conversationId,
      trace_id: traceId,
      action: plan.action,
      // 两个口径分开记，别让人分不清哪个是判定用的。
      pressure_pct_dsh: dshPct === null ? null : Number(dshPct.toFixed(3)),
      pressure_pct_est: plan.pressure_pct_est,
      pressure_source: plan.pressure_source,
      // 实测 token 是从哪个字段拿的（pressureTokens / projectedTokens / 没有）
      measured_tokens: measuredTokens,
      measured_field: measuredField,
      tokens_before: plan.tokens_before,
      tokens_after: plan.tokens_after,
      dropped: Array.isArray(plan.dropped_turns) ? plan.dropped_turns.length : 0,
      // 本次决策基于多少条、总共多少条——被截断时必须能解释「为什么没压那段」。
      messages_used: snap.messages.length,
      messages_total: snap.total,
      truncated: snap.truncated,
      budget,
      ms: Date.now() - startedAt,
    }

    if (!data || !data.ok) {
      audit(dir, { ...base, event: 'ctx-plan-failed', error: data && (data.error || data.status) })
      return
    }
    audit(dir, base)

    // Observation mode: the plan is recorded and nothing is touched.
    if (!cfg.ctxSwap) return
    if (plan.action !== 'drop' || !Array.isArray(plan.dropped_turns) || plan.dropped_turns.length === 0) {
      await completeTrace(traceId, { swapped: false, status: 'keep' })
      return
    }
    if (!snap.nodes) {
      await completeTrace(traceId, { swapped: false, status: 'mapping-mismatch' })
      audit(dir, { ...base, event: 'ctx-swap-skipped', reason: 'mapping-mismatch' })
      return
    }

    // Approved order: prune first (model-free, idempotent), then re-measure is
    // the engine's job inside compactRegion — we only re-derive the range from
    // the post-prune surface, never from the pre-prune numbers.
    let pruner = null
    try {
      pruner = typeof ctx.get === 'function' ? ctx.get('toolResultPruner') : null
    } catch {
      pruner = null
    }
    if (pruner && typeof pruner.pruneSession === 'function') {
      try {
        pruner.pruneSession(session)
      } catch (error) {
        audit(dir, { ...base, event: 'ctx-prune-failed', error: String(error && error.message) })
      }
    }

    const postPrune = snapshotHistory(session, {
      maxMessages: cfg.ctxMaxMessages,
      maxChars: cfg.ctxMaxChars,
    })
    const range = selectSurfaceRange(
      postPrune.messages,
      postPrune.nodes,
      plan.dropped_turns.length,
      Number(live.ctxKeepRecent) || 6,
    )
    if (!range) {
      await completeTrace(traceId, { swapped: false, status: 'no-range' })
      return
    }

    let compaction = null
    try {
      compaction = typeof ctx.get === 'function' ? ctx.get('compaction') : null
    } catch {
      compaction = null
    }
    if (!compaction || typeof compaction.compactRegion !== 'function') {
      await completeTrace(traceId, { swapped: false, status: 'no-engine' })
      return
    }

    // Timeout guard: a summary that hangs must not hold the step hostage.
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), Number(cfg.ctxTimeoutMs) || 20_000)
    const boundary = { start_seq: range.startSeq, end_seq: range.endSeq, turn_count: range.turnCount }
    try {
      const signal = payload.signal
        ? AbortSignal.any([payload.signal, controller.signal])
        : controller.signal
      const result = await compaction.compactRegion(range.startSeq, range.endSeq, agent, signal)
      const after = snapshotHistory(session, {
        maxMessages: cfg.ctxMaxMessages,
        maxChars: cfg.ctxMaxChars,
      })
      await completeTrace(traceId, {
        swapped: true,
        status: 'ok',
        ...boundary,
        after_hash: hashMessages(after.messages),
        shadowed_token_count: result && result.shadowedTokenCount,
        duration_ms: Date.now() - startedAt,
      })
      audit(dir, {
        ...base,
        event: 'ctx-swap',
        ...boundary,
        shadowed_tokens: result && result.shadowedTokenCount,
        ms: Date.now() - startedAt,
      })
    } catch (error) {
      const message = String((error && error.message) || error)
      const aborted = controller.signal.aborted
      const orphan = /busy|already in progress|lock/i.test(message)
      if (orphan) entry.ctxBroken = true
      await completeTrace(traceId, {
        swapped: false,
        status: aborted ? 'timeout' : orphan ? 'lock-orphan' : 'error',
        ...boundary,
        error: message,
        duration_ms: Date.now() - startedAt,
      })
      audit(dir, {
        ...base,
        event: aborted ? 'ctx-swap-timeout' : orphan ? 'ctx-lock-orphan' : 'ctx-swap-failed',
        ...boundary,
        error: message,
      })
    } finally {
      clearTimeout(timer)
    }
  }

  // 上下文编排入口：每轮一次（step===1），默认只观测。
  // 放在 gate 之后注册，所以它在 waterfall 里排在 gate 下游、内置压缩之前。
  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    if (!cfg.ctxPlan || !decision || decision.kind !== 'enter') return decision
    const agent = payload && payload.agent
    if (!agent || payload.step !== 1) return decision
    const entry = entryOf(agent)
    if (entry.ctxTurn === payload.turn || entry.ctxBroken) return decision
    entry.ctxTurn = payload.turn
    try {
      await runContextPlan(ctx, agent, payload, entry)
    } catch (error) {
      audit(dir, {
        ts: new Date().toISOString(),
        event: 'ctx-plan-crashed',
        turn: payload.turn,
        error: String((error && error.message) || error),
      })
    }
    return decision
  })

  // 溢出兜底：auto:false 之后内置的 request-error 监听器不再注册，这一道由我们补。
  // 用的是 compactIfNeeded(agent, 'context-overflow', signal)，不是 compactNow：
  //   - compactNow 要求 idle agent、无 open turn（region.ts:193-195），在 request-error
  //     这个时点调会直接 busy；
  //   - compactIfNeeded 自带 pruner + 选范围 + compactRegion，正是我们要的那条路径。
  ctx.on('agent/request-error', async (payload, next) => {
    if (!cfg.ctxOverflowRecovery) return next()
    const agent = payload && payload.agent
    const failure = payload && payload.failure
    if (!agent || !failure || failure.code !== 'CONTEXT_WINDOW_EXCEEDED') return next()
    const entry = entryOf(agent)
    if (entry.ctxOverflowBroken) return next()
    const generation = agent.session && agent.session.surface
      ? agent.session.surface.replaceGeneration
      : 0
    try {
      const compaction = typeof ctx.get === 'function' ? ctx.get('compaction') : null
      if (!compaction || typeof compaction.compactIfNeeded !== 'function') return next()
      await compaction.compactIfNeeded(agent, 'context-overflow', payload.signal)
      // Only claim the retry when the surface actually advanced; otherwise the
      // original provider error stays authoritative.
      if (agent.session.surface.replaceGeneration > generation) {
        audit(dir, {
          ts: new Date().toISOString(),
          event: 'ctx-overflow-recovered',
          turn: payload.turn,
          generation_before: generation,
          generation_after: agent.session.surface.replaceGeneration,
        })
        return { kind: 'retry' }
      }
    } catch (error) {
      const message = String((error && error.message) || error)
      if (/busy|already in progress|lock/i.test(message)) entry.ctxOverflowBroken = true
      audit(dir, {
        ts: new Date().toISOString(),
        event: 'ctx-overflow-failed',
        turn: payload.turn,
        error: message,
      })
    }
    return next()
  })

  ctx.on('agent/turn-stopping', async (payload) => {
    const live = readSettings(cfg)
    if (!cfg.enabled || !live.writeBack) return
    const agent = payload && payload.agent
    if (!agent) return
    const entry = entryOf(agent)

    let messages = []
    try {
      messages = agent.session.deriveMessages()
    } catch {
      return
    }
    const answer = lastAssistantText(messages)
    if (!answer) return
    const fingerprint = `${answer.slice(0, 80)}|${answer.length}`
    if (entry.lastWrite === fingerprint) return
    entry.lastWrite = fingerprint

    const conversationId = sessionKey(agent)
    const turn = payload && payload.turn !== undefined ? payload.turn : null
    const timeoutMs = Number(live.timeoutMs) || 10_000
    const retries = Number.isFinite(Number(live.retries)) ? Number(live.retries) : 1
    const rawChars = [...answer].length
    const digest = digestAnswer(answer)
    const digestChars = [...digest].length

    // Too short to carry anything and no conclusion: skip, but never silently.
    if (rawChars < 20 && !digest) {
      audit(dir, {
        ts: new Date().toISOString(),
        event: 'write-back-skipped',
        conversation_id: conversationId,
        turn,
        reason: 'too-short',
        chars: rawChars,
      })
      return
    }

    // 1) Verbatim answer -> mem.conv (role=assistant), capped at 8000 chars.
    const full = [...answer].slice(0, 8000).join('')
    const truncated = rawChars > 8000
    const eventId = eventIdFor(conversationId, turn, 'assistant')
    const ingested = await postJsonRetry(`${cfg.baseUrl}/ingest`, {
      source: 'dsh',
      conversation_id: conversationId,
      role: 'assistant',
      text: full,
      title: '',
      event_id: eventId,
      meta: {
        via: 'dsh-zhishu-memory-plugin',
        stage: 'full',
        turn,
        raw_chars: rawChars,
        digest_chars: digestChars,
        truncated,
      },
    }, timeoutMs, retries)
    audit(dir, {
      ts: new Date().toISOString(),
      event: 'write-back',
      conversation_id: conversationId,
      turn,
      ok: Boolean(ingested.data && ingested.data.ok),
      chars: rawChars,
      stored_chars: [...full].length,
      truncated,
      digest_chars: digestChars,
      event_id: eventId,
      duplicate: Boolean(ingested.data && ingested.data.duplicate),
      attempts: ingested.attempts,
      retry: ingested.attempts > 1,
      ms: ingested.ms,
      error: ingested.data && (ingested.data.error || ingested.data.status),
    })

    // 2) Digest -> mem.doc (kind=event), at most 300 chars, for retrieval.
    if (!digest) {
      audit(dir, {
        ts: new Date().toISOString(),
        event: 'write-back-skipped',
        conversation_id: conversationId,
        turn,
        reason: 'no-conclusion',
        chars: rawChars,
      })
      return
    }
    const shortId = conversationId.replace(/^dsh:/, '').slice(0, 24)
    const remembered = await postJsonRetry(`${cfg.baseUrl}/remember`, {
      text: digest,
      kind: 'event',
      title: `${shortId} t${turn === null ? '-' : turn} r${rawChars}/d${digestChars}`,
      topics: '',
    }, timeoutMs, retries)
    audit(dir, {
      ts: new Date().toISOString(),
      event: 'write-back-digest',
      conversation_id: conversationId,
      turn,
      ok: Boolean(remembered.data && remembered.data.ok),
      raw_chars: rawChars,
      digest_chars: digestChars,
      path: remembered.data && remembered.data.id,
      attempts: remembered.attempts,
      retry: remembered.attempts > 1,
      ms: remembered.ms,
      error: remembered.data && (remembered.data.error || remembered.data.status),
    })
  })
}
