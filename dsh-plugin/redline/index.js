/**
 * Redline guard — irreversible actions must be approved before they run.
 *
 * Hooks `tools/pre-execute` (waterfall). When a call matches a redline rule it
 * returns `ask`, so the approval service prompts the user; without approval
 * support the harness turns that into a denial. Every hit is appended to an
 * audit log so the decision survives the session.
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

/** The tools service must exist before this plugin can hook its pipeline. */
export const inject = ['tools']

/** Rules the profile starts with; a rules.json in the state directory overrides them. */
const DEFAULT_RULES = [
  {
    id: 'git-push',
    tools: ['pwsh', 'bash'],
    pattern: '\\bgit\\b[^\\n]{0,120}?\\bpush\\b',
    note: '把本地提交推到远端仓库',
  },
  {
    id: 'publish',
    tools: ['pwsh', 'bash'],
    pattern: '\\b(npm|pnpm|yarn)\\b[^\\n]{0,40}\\bpublish\\b',
    note: '发布包到公开源',
  },
  {
    id: 'deploy',
    tools: ['pwsh', 'bash'],
    pattern:
      '\\b(docker\\s+push|docker\\s+compose\\s+(up|down|rm)|kubectl\\s+(apply|delete)|helm\\s+(install|upgrade)|flyctl\\s+deploy|vercel\\s+--prod)\\b',
    note: '部署或停止线上服务',
  },
  {
    id: 'destructive-delete',
    tools: ['pwsh', 'bash'],
    pattern: '(\\brm\\s+-[a-zA-Z]*[rf][a-zA-Z]*\\b|Remove-Item[^\\n]{0,80}-Recurse|\\brd\\s+/s\\b|\\bdel\\s+/[sqf]\\b)',
    note: '递归删除文件或目录',
  },
  {
    id: 'sql-destructive',
    tools: ['pwsh', 'bash'],
    pattern: '\\b(DROP\\s+(DATABASE|TABLE|SCHEMA)|TRUNCATE\\s+TABLE|DELETE\\s+FROM)\\b',
    note: '删库或清表',
  },
  {
    id: 'publish-outward',
    tools: ['qqbot', 'qq_draw', 'qq_sticker'],
    note: '对外发消息或发图',
  },
  {
    id: 'selftest',
    tools: ['pwsh', 'bash'],
    pattern: 'redline-guard-selftest',
    matchRaw: true,
    note: '守卫自检（只在命令里出现 redline-guard-selftest 时命中）',
  },
]

/**
 * Resolve where this plugin keeps its rules and audit log.
 * @returns {string} Absolute directory path.
 */
function stateDirectory() {
  const base = process.env.DSH_PROFILE_DIR || join(homedir(), '.dsh')
  return join(base, 'redline-guard')
}

/**
 * Read the optional rules.json override; a malformed file keeps the defaults.
 * @param {string} dir - State directory.
 * @returns {{mode: string, rules: Array<any>}} Effective configuration.
 */
function loadConfiguration(dir) {
  const config = { mode: 'ask', rules: DEFAULT_RULES }
  const file = join(dir, 'rules.json')
  if (!existsSync(file)) return config
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8'))
    if (parsed && typeof parsed.mode === 'string') config.mode = parsed.mode
    if (parsed && Array.isArray(parsed.rules)) {
      config.rules = parsed.rules
        .filter((rule) => rule && typeof rule.id === 'string' && typeof rule.note === 'string')
        .map((rule) => ({
          id: rule.id,
          note: rule.note,
          tools: Array.isArray(rule.tools) ? rule.tools : undefined,
          pattern: typeof rule.pattern === 'string' ? rule.pattern : undefined,
        }))
    }
  } catch {
    // Keep the defaults: a broken override must not disable the guard.
  }
  return config
}

/**
 * Drop quoted spans so text that merely mentions a command is not a command.
 * `git commit -m "fix push flow"` must pass; `git push origin main` must not.
 * @param {string} command - Raw command text.
 * @returns {string} Command text without single- or double-quoted spans.
 */
function stripQuoted(command) {
  return command
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .replace(/'(?:[^'\\]|\\.)*'/g, "''")
}

/**
 * Match one call against the rule list.
 * @param {string} tool - Tool name.
 * @param {unknown} args - Parsed arguments.
 * @param {ReadonlyArray<any>} rules - Rules to test.
 * @returns {any | undefined} The first matching rule.
 */
function matchRule(tool, args, rules) {
  const record = args && typeof args === 'object' ? args : {}
  const command = typeof record.command === 'string' ? record.command : ''
  const raw = command || safeJson(args)
  const stripped = command ? stripQuoted(command) : safeJson(args)
  for (const rule of rules) {
    if (rule.tools && !rule.tools.includes(tool)) continue
    if (rule.pattern) {
      let expression
      try {
        expression = new RegExp(rule.pattern, 'i')
      } catch {
        continue
      }
      if (!expression.test(rule.matchRaw ? raw : stripped)) continue
    }
    return rule
  }
  return undefined
}

/**
 * Serialize arguments without letting a hostile value throw.
 * @param {unknown} value - Value to stringify.
 * @returns {string} JSON text, or an empty string.
 */
function safeJson(value) {
  try {
    return JSON.stringify(value) ?? ''
  } catch {
    return ''
  }
}

/**
 * Append one audit line; logging failures never change the decision.
 * @param {string} dir - State directory.
 * @param {Record<string, unknown>} entry - Audit fields.
 */
function audit(dir, entry) {
  try {
    mkdirSync(dir, { recursive: true })
    appendFileSync(join(dir, 'audit.jsonl'), `${JSON.stringify(entry)}\n`, 'utf8')
  } catch {
    // The guard still works without its log.
  }
}

/** Approval card labels; the answer echoes the chosen label. */
const APPROVE_LABEL = '允许'
const REJECT_LABEL = '拒绝'

/** How long one approval card may stay unanswered before the call is refused. */
const approvalTimeoutMs = Number(process.env.DSH_REDLINE_TIMEOUT_MS ?? 120000)

/**
 * One-line command digest for the question title: phone cards only render that
 * line, so the action must be readable without the detail block.
 * @param {unknown} args - Parsed arguments.
 * @returns {string} At most 30 characters, single line.
 */
function commandDigest(args) {
  const record = args && typeof args === 'object' ? args : {}
  const text = typeof record.command === 'string' ? record.command : safeJson(args)
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > 30 ? `${flat.slice(0, 29)}…` : flat
}

/**
 * Install the guard on the tools pipeline.
 * @param {any} ctx - Cordis plugin context.
 * @param {Record<string, unknown>} [config] - Row configuration from the patch.
 */
export function apply(ctx, config = {}) {
  const dir = stateDirectory()
  const loaded = loadConfiguration(dir)
  const mode = String(config.mode ?? loaded.mode ?? 'ask')
  const rules = loaded.rules

  /**
   * Ask the user to approve one matched call; any failure counts as a refusal.
   * @param {any} exec - Pending call.
   * @param {any} rule - Matched rule.
   * @param {string} preview - Short argument preview for the card.
   * @returns {Promise<{allowed: boolean, error?: string}>} Whether the user approved this single call.
   */
  async function askApproval(exec, rule, preview) {
    const questions = ctx.get('userQuestions')
    if (questions === undefined) return { allowed: false, error: 'NO_SERVICE' }
    if (!exec.agent) return { allowed: false, error: 'NO_AGENT' }
    let timer
    try {
      const expiry = new Promise((resolve) => {
        timer = setTimeout(() => resolve('timeout'), approvalTimeoutMs)
      })
      const request = questions.ask({
        agent: exec.agent,
        signal: exec.signal,
        questions: [
          {
            id: `redline-${rule.id}`,
            header: '红线守卫',
            question: `确认执行：${rule.note}（${commandDigest(exec.arguments)}）`,
            detail: `命令 / 参数：\n${preview}\n\n规则：${rule.id}`,
            options: [{ label: APPROVE_LABEL }, { label: REJECT_LABEL }],
            intent: {
              kind: 'approval',
              approve: APPROVE_LABEL,
              reject: REJECT_LABEL,
              subject: { name: '红线守卫' },
              display: {
                question: { en: 'Approve this action?', zh: '这次要执行吗？' },
                detail: { en: 'Irreversible action requested by the agent.', zh: '这是一次不可逆动作，批了才会执行。' },
              },
            },
          },
        ],
      })
      request.catch(() => 'aborted')
      const answer = await Promise.race([request, expiry])
      if (answer === 'timeout') return { allowed: false, error: 'TIMEOUT' }
      if (answer === 'aborted') return { allowed: false, error: 'ABORTED' }
      const selected = answer && answer.answers && answer.answers[0] && answer.answers[0].selected
      return { allowed: Array.isArray(selected) && selected.includes(APPROVE_LABEL) }
    } catch (error) {
      const code = error && (error.code ?? error.name) ? String(error.code ?? error.name) : 'UNKNOWN'
      return { allowed: false, error: `${code}: ${String(error && error.message ? error.message : error).slice(0, 200)}` }
    } finally {
      if (timer !== undefined) clearTimeout(timer)
    }
  }

  ctx.on('tools/pre-execute', async (exec, next) => {
    let rule
    try {
      rule = matchRule(exec.name, exec.arguments, rules)
    } catch {
      return next()
    }
    if (!rule) return next()

    const preview = safeJson(exec.arguments).slice(0, 400)
    const intercept = `红线拦截：${rule.note}（规则 ${rule.id}）。这是不可逆动作，需要用户批红。`

    if (mode === 'deny') {
      audit(dir, {
        ts: new Date().toISOString(),
        tool: exec.name,
        rule: rule.id,
        decision: 'deny',
        agent: exec.agent && (exec.agent.id ?? exec.agent.name),
        arguments: preview,
      })
      return { kind: 'deny', reason: intercept }
    }

    const outcome = await askApproval(exec, rule, preview)
    audit(dir, {
      ts: new Date().toISOString(),
      tool: exec.name,
      rule: rule.id,
      decision: outcome.allowed ? 'allowed' : 'denied',
      error: outcome.error,
      agent: exec.agent && (exec.agent.id ?? exec.agent.name),
      arguments: preview,
    })

    if (outcome.allowed) return next()
    return {
      kind: 'deny',
      reason: `用户拒绝了这次「${rule.note}」（规则 ${rule.id}）。不要重试，也不要换一种写法绕过；如果确实要做，先问清用户改什么条件再做。`,
    }
  })
}
