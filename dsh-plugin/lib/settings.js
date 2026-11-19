/**
 * 智枢记忆套件的共享参数表。
 *
 * 四行 loader（zhishu-memory / ctx-guard / redline / mem-float）跑在同一个宿主进程里，
 * 所以它们读同一个模块实例、同一个文件：`~/.dsh/zhishu-memory/settings.json`。
 * 悬浮胶囊的滑杆写这个文件，各行在**下一次用到参数时**重新读（按 mtime 缓存，
 * 没改就不重复解析），因此改完下一轮就生效，不用重启 DSH、也不用改 profile YAML。
 *
 * 优先级：文件 > loader 行 config（出厂默认）> KNOBS 里的 default。
 * 行 config 仍然是「出厂设置」，用户通过面板改出来的才是运行值；删掉文件即回到出厂。
 */
import { mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

/** 所有行共用的参数文件；和 audit.jsonl 放在同一个目录。 */
export const SETTINGS_PATH = join(homedir(), '.dsh', 'zhishu-memory', 'settings.json')

/**
 * 可调参数的唯一真源：面板按它渲染控件，写入时按它做范围校验。
 *
 * `type` 决定控件形态：number → 滑杆，boolean → 开关，enum → 单选，text → 输入框。
 * `group` 决定它出现在面板的哪一段：`connection` 进工作台（探活用），`tuning` 进参数区。
 * `collect` 这组目前由 mem-float 的汇集接口读取（面板里先用原始键名渲染，专属标签/页签下一轮补）。
 * 界面上的名称和「这是什么意思」的说明在 client.js 里（要跟随语言），这里只放约束，
 * 免得中英文案和校验规则各写一份、迟早对不上。
 */
export const KNOBS = [
  { key: 'noteEveryTurns', type: 'number', group: 'tuning', min: 1, max: 20, step: 1, default: 8 },
  { key: 'noteMaxChars', type: 'number', group: 'tuning', min: 60, max: 600, step: 10, default: 220 },
  { key: 'limit', type: 'number', group: 'tuning', min: 1, max: 10, step: 1, default: 3 },
  { key: 'timeoutMs', type: 'number', group: 'tuning', min: 1000, max: 30000, step: 500, default: 10000 },
  { key: 'retries', type: 'number', group: 'tuning', min: 0, max: 3, step: 1, default: 1 },
  { key: 'ctxThreshold', type: 'number', group: 'tuning', min: 0.05, max: 0.9, step: 0.05, default: 0.2 },
  { key: 'ctxKeepRecent', type: 'number', group: 'tuning', min: 0, max: 20, step: 1, default: 6 },
  { key: 'writeBack', type: 'boolean', group: 'tuning', default: true },
  { key: 'ctxGuardEnabled', type: 'boolean', group: 'tuning', default: true },
  { key: 'ctxGuardMode', type: 'enum', group: 'tuning', values: ['off', 'smart', 'hard'], default: 'smart' },
  // 工作台的探活目标。只影响面板怎么探，不改变 memory-mcp 服务自己用的 MEM_DSN。
  { key: 'pgHost', type: 'text', group: 'connection', format: 'host', default: '127.0.0.1' },
  { key: 'pgPort', type: 'text', group: 'connection', format: 'port', default: '41023' },
  { key: 'pgDatabase', type: 'text', group: 'connection', format: 'name', default: 'mem' },
  { key: 'pgUser', type: 'text', group: 'connection', format: 'name', default: 'postgres' },
  { key: 'qdrantUrl', type: 'text', group: 'connection', format: 'url', default: 'http://127.0.0.1:41024' },

  // 环境：全部留空即「不碰用户的机器」，只在 Setup 里显式执行时才用。
  { key: 'setupDone', type: 'boolean', group: 'setup', default: false },
  { key: 'pgContainer', type: 'text', group: 'setup', format: 'name', default: '' },
  { key: 'qdrantContainer', type: 'text', group: 'setup', format: 'name', default: '' },
  { key: 'memoryMcpDir', type: 'text', group: 'setup', format: 'path', default: '' },
  // 默认给一个公开仓库：新机器上「一键装环境」的 memory 步因此直接可用，
  // 不用先自己去找 Git 地址；想换就改这一项。
  { key: 'memoryMcpRepo', type: 'text', group: 'setup', format: 'git', default: 'https://github.com/ysyluminous/dsh-zhishu-memory.git' },
  { key: 'pythonBin', type: 'text', group: 'setup', format: 'path', default: '' },

  // 记忆服务本体（原来是 .env 里的那些项）。面板改完重启服务即生效；
  // 启动时由 mem-float 作为环境变量传给 python，服务不再依赖 .env 文件。
  { key: 'memDsn', type: 'text', group: 'service', default: '' },
  { key: 'zhishuDsn', type: 'text', group: 'service', default: '' },
  { key: 'memDataDir', type: 'text', group: 'service', format: 'path', default: '' },
  { key: 'memReviewToken', type: 'text', group: 'service', default: '' },
  { key: 'memDefaultUser', type: 'text', group: 'service', default: 'owner' },
  { key: 'memRequireKey', type: 'boolean', group: 'service', default: false },
  { key: 'memDbLabel', type: 'text', group: 'service', default: '' },
  { key: 'memEnv', type: 'enum', group: 'service', values: ['prod', 'local-dev'], default: 'prod' },

  // 备份：0 = 关闭定时，只在手动点「立即备份」时跑。
  { key: 'backupEveryHours', type: 'number', group: 'backup', min: 0, max: 168, step: 1, default: 0 },
  { key: 'backupKeep', type: 'number', group: 'backup', min: 1, max: 30, step: 1, default: 7 },

  // 内置汇集：默认关（没配服务地址前不该自己跑）；0 = 只在手动点「立即汇集」。
  { key: 'collectEnabled', type: 'boolean', group: 'collect', default: false },
  { key: 'collectEveryMinutes', type: 'number', group: 'collect', min: 0, max: 1440, step: 1, default: 60 },
  { key: 'collectTools', type: 'text', group: 'collect', default: 'dsh,workbuddy,claude-code,codex' },
]

const BY_KEY = new Map(KNOBS.map((knob) => [knob.key, knob]))

/** Parsed file cache, invalidated by mtime+size. */
let cache = { stamp: '', data: {} }

/**
 * Read the settings file, reusing the previous parse while its stamp is unchanged.
 * @returns {Record<string, unknown>} Raw object from disk, or an empty object.
 */
function rawFile() {
  let stamp = ''
  try {
    const stat = statSync(SETTINGS_PATH)
    stamp = `${stat.mtimeMs}:${stat.size}`
  } catch {
    // No file is a valid state: everything stays at its shipped default.
    cache = { stamp: '', data: {} }
    return cache.data
  }
  if (cache.stamp === stamp) return cache.data
  try {
    const parsed = JSON.parse(readFileSync(SETTINGS_PATH, 'utf8'))
    cache = { stamp, data: parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {} }
  } catch {
    // A half-written or hand-broken file must not take the plugin down.
    cache = { stamp, data: {} }
  }
  return cache.data
}

/**
 * Round one number to its declared step without leaving float dust (0.30000000000000004).
 * @param {number} value - Candidate value.
 * @param {number} step - Declared step, when any.
 * @returns {number} Stepped value.
 */
function stepOf(value, step) {
  if (!(step > 0)) return value
  const stepped = Math.round(value / step) * step
  const decimals = (String(step).split('.')[1] ?? '').length
  return decimals === 0 ? Math.round(stepped) : Number(stepped.toFixed(decimals))
}

/**
 * Validate one value against its knob; unknown or unusable values return undefined.
 * @param {{type: string, min?: number, max?: number, step?: number, values?: string[], format?: string}} knob - Knob definition.
 * @param {unknown} value - Candidate value.
 * @returns {number | boolean | string | undefined} Accepted value.
 */
function coerce(knob, value) {
  if (value === undefined || value === null || value === '') return undefined
  if (knob.type === 'number') {
    const number = Number(value)
    if (!Number.isFinite(number)) return undefined
    return stepOf(Math.min(knob.max, Math.max(knob.min, number)), knob.step)
  }
  if (knob.type === 'boolean') {
    if (value === true || value === 'true') return true
    if (value === false || value === 'false') return false
    return undefined
  }
  if (knob.type === 'enum') return knob.values.includes(value) ? value : undefined
  if (knob.type === 'text') return textValue(knob, value)
  return undefined
}

/**
 * Accept one connection string: trimmed, bounded, and checked against its format.
 * @param {{format?: string}} knob - Text knob definition.
 * @param {unknown} value - Candidate value.
 * @returns {string | undefined} Accepted text.
 */
function textValue(knob, value) {
  if (typeof value !== 'string' && typeof value !== 'number') return undefined
  const text = String(value).trim().slice(0, 200)
  if (!text) return undefined
  if (knob.format === 'port') {
    const port = Number(text)
    if (!Number.isInteger(port) || port < 1 || port > 65535) return undefined
    return String(port)
  }
  if (knob.format === 'url') {
    if (!/^https?:\/\//i.test(text)) return undefined
    try {
      const parsed = new URL(text)
      return parsed.origin
    } catch {
      return undefined
    }
  }
  if (knob.format === 'host') return /^[A-Za-z0-9._:-]+$/.test(text) ? text : undefined
  if (knob.format === 'name') return /^[A-Za-z0-9_.-]+$/.test(text) ? text : undefined
  if (knob.format === 'path') return text.length <= 200 ? text : undefined
  // Git 地址要原样保留（不能像 'url' 那样被裁成 origin），只做协议白名单。
  if (knob.format === 'git') {
    return /^(https?:\/\/[\w.-]+|git@[\w.-]+:|ssh:\/\/)/.test(text) && text.length <= 300 ? text : undefined
  }
  return text
}

/**
 * Pick only known keys, coerced; used for both the file and a row's shipped config.
 * @param {Record<string, unknown>} source - Object to filter.
 * @returns {Record<string, unknown>} Accepted subset.
 */
function sanitize(source) {
  /** @type {Record<string, unknown>} */
  const out = {}
  if (!source || typeof source !== 'object') return out
  for (const [key, value] of Object.entries(source)) {
    const knob = BY_KEY.get(key)
    if (knob === undefined) continue
    const accepted = coerce(knob, value)
    if (accepted !== undefined) out[key] = accepted
  }
  return out
}

/**
 * Effective values for one row.
 * @param {Record<string, unknown>} [rowConfig] - That row's loader `config` (shipped defaults).
 * @returns {Record<string, unknown>} Complete parameter set.
 */
export function readSettings(rowConfig = {}) {
  /** @type {Record<string, unknown>} */
  const values = {}
  for (const knob of KNOBS) values[knob.key] = knob.default
  Object.assign(values, sanitize(rowConfig), sanitize(rawFile()))
  return values
}

/**
 * Merge a patch into the file and persist it atomically.
 * @param {Record<string, unknown>} patch - Requested values; unknown keys are ignored.
 * @returns {{values: Record<string, unknown>, accepted: string[], ignored: string[]}} Result.
 */
export function writeSettings(patch) {
  const accepted = []
  const ignored = []
  /** @type {Record<string, unknown>} */
  const next = { ...sanitize(rawFile()) }
  if (patch && typeof patch === 'object') {
    for (const [key, value] of Object.entries(patch)) {
      const knob = BY_KEY.get(key)
      const coerced = knob === undefined ? undefined : coerce(knob, value)
      if (knob === undefined || coerced === undefined) {
        ignored.push(key)
        continue
      }
      next[key] = coerced
      accepted.push(key)
    }
  }
  mkdirSync(dirname(SETTINGS_PATH), { recursive: true })
  const temporary = `${SETTINGS_PATH}.tmp`
  writeFileSync(temporary, `${JSON.stringify(next, null, 2)}\n`, 'utf8')
  renameSync(temporary, SETTINGS_PATH)
  cache = { stamp: '', data: next }
  return { values: readSettings(), accepted, ignored }
}

/**
 * Drop every override, returning all rows to their shipped config.
 * @returns {{values: Record<string, unknown>}} Reset result.
 */
export function resetSettings() {
  mkdirSync(dirname(SETTINGS_PATH), { recursive: true })
  const temporary = `${SETTINGS_PATH}.tmp`
  writeFileSync(temporary, '{}\n', 'utf8')
  renameSync(temporary, SETTINGS_PATH)
  cache = { stamp: '', data: {} }
  return { values: readSettings() }
}
