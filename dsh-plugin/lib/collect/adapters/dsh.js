/**
 * DSH 会话适配器。
 *
 * 位置：`<home>/.dsh/sessions/<workspace>/<session>/session.v4.jsonl.zstd`
 *
 * 文件不是「一个大 zstd 帧」，而是**追加写成的一串 zstd 帧**：DSH 每 flush 一次
 * 就追加一帧。Node 的 `zstdDecompressSync` / `createZstdDecompress` 只解第一帧，
 * 直接喂整个文件只会拿到 `session` 头（实测 7.8 MB 的文件只解出 1 行）。所以这里
 * 按 magic（`28 b5 2f fd`）切帧逐帧解。
 *
 * 事件口径（只收文本段，不带 reasoning / tool-call 参数）：
 *   - `user/message`      → role user       text = data.content[*].text
 *   - `assistant/message` → role assistant  text = data.message.content[*].text
 *   - `tool/result`       → role tool       text = data.message.content[*].text
 *
 * 插件自己注入的「记忆召回 / 上下文守卫」user 消息（source.kind 命中
 * memory-recall / ctx-guard / zhishu-memory）不算用户发言，跳过，否则每轮都会把
 * 召回快照重复写进 mem.conv。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { zstdDecompressSync } from 'node:zlib'

import { MAX_FILE_BYTES, clip, isoOf, textOf } from '../util.js'

const SESSION_FILE = 'session.v4.jsonl.zstd'
const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])
const MAX_DEPTH = 6
const INJECTED = /(memory-recall|ctx-guard|zhishu-memory)/i

/**
 * Recurse into the sessions tree and collect session files.
 * @param {string} dir - Directory to walk.
 * @param {string[]} out - Accumulator.
 * @param {number} depth - Current depth.
 * @returns {string[]} Absolute file paths.
 */
function findSessionFiles(dir, out = [], depth = 0) {
  let entries = []
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return out
  }
  for (const entry of entries) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (depth < MAX_DEPTH) findSessionFiles(full, out, depth + 1)
      continue
    }
    if (entry.isFile() && entry.name === SESSION_FILE) out.push(full)
  }
  return out
}

/**
 * Decompress a concatenation of zstd frames, one frame at a time.
 * A corrupt or partially flushed frame is skipped instead of taking the file down.
 * @param {Buffer} buffer - Raw file bytes.
 * @returns {string} Decoded JSONL text (frames concatenated).
 */
function decodeFrames(buffer) {
  if (typeof zstdDecompressSync !== 'function') return ''
  const parts = []
  let start = 0
  while (start < buffer.length) {
    const next = buffer.indexOf(ZSTD_MAGIC, start + ZSTD_MAGIC.length)
    const end = next === -1 ? buffer.length : next
    try {
      parts.push(zstdDecompressSync(buffer.subarray(start, end)).toString('utf8'))
    } catch {
      // 半截帧 / 坏帧：跳过，后面的帧继续解
    }
    if (next === -1) break
    start = next
  }
  return parts.join('')
}

/**
 * Parse one DSH session file into a session object with text messages.
 * @param {string} text - Decoded JSONL.
 * @param {string} file - Session file path (for id fallback).
 * @returns {{externalId: string, title: string, cwd: string, createdAt: string|null, messages: Array<Record<string, unknown>>}}
 *   Parsed session.
 */
function parseSession(text, file) {
  let externalId = basename(dirname(file))
  let title = ''
  let cwd = ''
  let createdAt = null
  const messages = []
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    let event
    try {
      event = JSON.parse(line)
    } catch {
      continue
    }
    if (!event || typeof event !== 'object') continue

    if (event.type === 'session') {
      if (typeof event.id === 'string' && event.id) externalId = event.id
      if (typeof event.cwd === 'string') cwd = event.cwd
      createdAt = isoOf(event.createdAt)
      continue
    }
    if (event.type === 'session/title') {
      const value = event.data && event.data.title
      if (typeof value === 'string' && value.trim()) title = value.trim()
      continue
    }

    let role = ''
    let text = ''
    if (event.type === 'user/message') {
      const source = event.data && event.data.source
      if (source && typeof source.kind === 'string' && INJECTED.test(source.kind)) continue
      role = 'user'
      text = textOf(event.data && event.data.content)
    } else if (event.type === 'assistant/message') {
      const message = event.data && event.data.message
      role = 'assistant'
      text = textOf(message && message.content !== undefined ? message.content : event.data && event.data.content)
    } else if (event.type === 'tool/result') {
      const message = event.data && event.data.message
      role = 'tool'
      text = textOf(message && message.content !== undefined ? message.content : event.data && event.data.content)
    } else {
      continue
    }

    const body = clip(text)
    if (!body) continue
    messages.push({
      seq: messages.length,
      role,
      kind: role === 'tool' ? 'tool-result' : 'message',
      text: body,
      time: isoOf(event.time),
    })
  }
  return { externalId, title: title || externalId, cwd, createdAt, messages }
}

export default {
  id: 'dsh',
  label: 'DSH',

  /**
   * @param {{home: string}} env - Environment with resolved home.
   * @returns {{found: boolean, root: string, sessions: number}} Detection result.
   */
  detect(env) {
    const root = join(env.home, '.dsh', 'sessions')
    let exists = false
    try {
      exists = statSync(root).isDirectory()
    } catch {
      exists = false
    }
    const files = exists ? findSessionFiles(root) : []
    return { found: files.length > 0, root, sessions: files.length }
  },

  /**
   * @param {{files?: Record<string, any>}} state - Per-tool state.
   * @param {{home: string, limit: number}} env - Scan environment.
   * @returns {{sessions: any[], docs: any[], next: {files: Record<string, any>}, pending: number, skipped: number}}
   *   Incremental scan result.
   */
  scan(state, env) {
    const root = join(env.home, '.dsh', 'sessions')
    const files = state && state.files && typeof state.files === 'object' ? state.files : {}
    const nextFiles = {}
    const sessions = []
    let pending = 0
    let skipped = 0
    let limit = Number(env.limit)
    // 直接调用适配器（不走 index）时给一个安全默认值，避免 0 条。
    if (!Number.isFinite(limit) || limit < 0) limit = 2000
    let budget = limit

    for (const file of findSessionFiles(root)) {
      let stat
      try {
        stat = statSync(file)
      } catch {
        continue
      }
      const previous = files[file]
      if (previous && previous.skipped && previous.mtimeMs === stat.mtimeMs && previous.size === stat.size) {
        nextFiles[file] = previous
        skipped += 1
        continue
      }
      if (stat.size > MAX_FILE_BYTES) {
        nextFiles[file] = { skipped: true, mtimeMs: stat.mtimeMs, size: stat.size }
        skipped += 1
        continue
      }
      // 没变且已经读完才跳过；上一轮被单次上限截断时 sent < total，必须继续读。
      const consumedAll = previous ? previous.total === undefined || previous.sent >= previous.total : false
      if (previous && consumedAll && previous.mtimeMs === stat.mtimeMs && previous.size === stat.size) {
        nextFiles[file] = previous
        continue
      }
      if (budget <= 0) {
        // 本轮配额用完：不再整文件解，保留旧状态，下一轮从断点继续。
        if (previous) nextFiles[file] = previous
        pending += 1
        continue
      }

      let parsed
      try {
        parsed = parseSession(decodeFrames(readFileSync(file)), file)
      } catch {
        // 读不动就当没变，下一轮再试
        if (previous) nextFiles[file] = previous
        continue
      }

      const reset = !previous
        || Number(previous.size) > stat.size
        || Number(previous.mtimeMs) > stat.mtimeMs
      const sent = reset ? 0 : Math.min(Number(previous && previous.sent) || 0, parsed.messages.length)
      const fresh = parsed.messages.slice(sent, sent + budget)
      budget -= fresh.length
      if (fresh.length < parsed.messages.length - sent) pending += parsed.messages.length - sent - fresh.length
      nextFiles[file] = {
        mtimeMs: stat.mtimeMs,
        size: stat.size,
        sent: sent + fresh.length,
        total: parsed.messages.length,
        lastId: parsed.externalId,
      }
      if (fresh.length > 0) {
        sessions.push({
          externalId: parsed.externalId,
          title: parsed.title,
          cwd: parsed.cwd,
          createdAt: parsed.createdAt,
          messages: fresh,
        })
      }
    }

    return { sessions, docs: [], next: { files: nextFiles }, pending, skipped }
  },
}
