/**
 * 内置汇集的增量状态。
 *
 * 文件：`~/.dsh/zhishu-memory/collect-state.json`
 *
 * ```json
 * { "version": 1, "tools": { "workbuddy": {
 *     "files":     { "<abs path>": { "offset": 1234, "mtimeMs": 1, "size": 2, "sent": 10 } },
 *     "documents": { "<abs path>": { "mtimeMs": 1, "size": 2, "hash": "..." } },
 *     "lastRun": "2026-11-19T...", "synced": 123, "pending": 0
 * } } }
 * ```
 *
 * `offset` 是已消费的**字节**（jsonl），`sent` 是已产出的**消息条数**（zstd 会话
 * 每次整文件解，用条数续传）。两者都只在「上传成功」之后才推进，所以中途失败
 * 下一轮会重发，服务端按 event_id 去重，重跑安全。
 *
 * 读写都容错：文件坏了当空状态，写盘用临时文件 + rename，避免半截 JSON。
 */
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

/** @typedef {{mtimeMs?: number, size?: number, offset?: number, sent?: number, hash?: string}} FileState */

/**
 * Read the state file; any parse/IO failure returns an empty state.
 * @param {string} file - Absolute state path.
 * @returns {{version: number, tools: Record<string, any>}} State object.
 */
export function readState(file) {
  let parsed = null
  try {
    parsed = JSON.parse(readFileSync(file, 'utf8'))
  } catch {
    parsed = null
  }
  const state = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  if (state.version !== 1) state.version = 1
  if (!state.tools || typeof state.tools !== 'object' || Array.isArray(state.tools)) state.tools = {}
  return state
}

/**
 * Persist state atomically.
 * @param {{version: number, tools: Record<string, any>}} state - State to write.
 * @param {string} file - Absolute state path.
 * @returns {boolean} True when the file was written.
 */
export function writeState(state, file) {
  try {
    mkdirSync(dirname(file), { recursive: true })
    const temporary = `${file}.tmp`
    writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, 'utf8')
    renameSync(temporary, file)
    return true
  } catch {
    return false
  }
}

/**
 * Get (and normalise) one tool's state slot.
 * @param {{tools: Record<string, any>}} state - Whole state.
 * @param {string} id - Adapter id.
 * @returns {{files: Record<string, FileState>, documents: Record<string, FileState>, lastRun?: string, synced?: number, pending?: number}} Tool state.
 */
export function toolState(state, id) {
  const current = state.tools[id]
  const next = current && typeof current === 'object' && !Array.isArray(current) ? current : {}
  if (!next.files || typeof next.files !== 'object' || Array.isArray(next.files)) next.files = {}
  if (!next.documents || typeof next.documents !== 'object' || Array.isArray(next.documents)) next.documents = {}
  state.tools[id] = next
  return next
}

/**
 * Newest lastRun among the given tools.
 * @param {{tools: Record<string, any>}} state - Whole state.
 * @param {string[]} ids - Adapter ids.
 * @returns {string|null} ISO timestamp, or null when never run.
 */
export function lastRunOf(state, ids) {
  let newest = null
  for (const id of ids) {
    const value = state.tools?.[id]?.lastRun
    if (typeof value !== 'string') continue
    if (newest === null || Date.parse(value) > Date.parse(newest)) newest = value
  }
  return newest
}
