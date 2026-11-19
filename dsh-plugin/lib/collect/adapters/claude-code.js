/**
 * Claude Code 会话适配器。
 *
 * 位置：`<home>/.claude/projects/**\/*.jsonl`
 *
 * 行口径：
 *   - `{"type":"user"|"assistant","message":{"content":[{"type":"text","text":...}]}}`
 *   - 没有 `message` 字段时正文在顶层 `content`（可能是字符串，也可能是数组）
 *   - 其它类型（`permission-mode`、`file-history-snapshot`、`summary`、`system`…）
 *     只取元信息：`sessionId` / `cwd` / `timestamp` / `summary`（标题）。
 *
 * 增量、64 MB 上限、坏行跳过与 WorkBuddy 一致：按字节 offset 续读完整行。
 */
import { join } from 'node:path'

import { clip, isoOf, readJsonlChunk, stemOf, textOf, walkJsonl } from '../util.js'

/**
 * List Claude Code session files, bounded.
 * @param {string} home - User home.
 * @returns {string[]} Absolute jsonl paths.
 */
function walk(home) {
  return walkJsonl(join(home, '.claude', 'projects'))
}

/**
 * Extract one text message from a Claude Code event line.
 * @param {any} event - Parsed JSON line.
 * @returns {{role: string, text: string}|null} Text message, or null.
 */
function parseEvent(event) {
  if (!event || typeof event !== 'object') return null
  const type = String(event.type || '')
  if (type !== 'user' && type !== 'assistant') return null
  const message = event.message
  const content = message && typeof message === 'object' && message.content !== undefined
    ? message.content
    : event.content
  let text = textOf(content)
  if (!text && typeof content === 'string') text = content
  return text ? { role: type, text } : null
}

export default {
  id: 'claude-code',
  label: 'Claude Code',

  /**
   * @param {{home: string}} env - Environment.
   * @returns {{found: boolean, root: string, sessions: number}} Detection result.
   */
  detect(env) {
    const root = join(env.home, '.claude', 'projects')
    const files = walk(env.home)
    return { found: files.length > 0, root, sessions: files.length }
  },

  /**
   * @param {{files?: Record<string, any>}} state - Per-tool state.
   * @param {{home: string, limit: number}} env - Scan environment.
   * @returns {{sessions: any[], docs: any[], next: {files: Record<string, any>, documents: Record<string, any>}, pending: number, skipped: number}}
   *   Incremental scan result.
   */
  scan(state, env) {
    const files = state && state.files && typeof state.files === 'object' ? state.files : {}
    const nextFiles = {}
    const sessions = []
    let pending = 0
    let skipped = 0
    let limit = Number(env.limit)
    // 直接调用适配器（不走 index）时给一个安全默认值，避免 0 条。
    if (!Number.isFinite(limit) || limit < 0) limit = 2000
    let budget = limit

    for (const file of walk(env.home)) {
      const previous = files[file]
      if (budget <= 0) {
        // 本轮配额用完：不再读新行，保留旧状态，下一轮继续。
        if (previous) nextFiles[file] = previous
        pending += 1
        continue
      }
      const chunk = readJsonlChunk(file, previous)
      if (!chunk) continue

      if (chunk.tooLarge) {
        nextFiles[file] = previous && previous.offset
          ? { ...previous, tooLarge: true, mtimeMs: chunk.stat.mtimeMs, size: chunk.stat.size }
          : { tooLarge: true, mtimeMs: chunk.stat.mtimeMs, size: chunk.stat.size }
        skipped += 1
        continue
      }

      const reset = Boolean(previous)
        && (Number(previous.size) > chunk.stat.size
          || (Number(previous.offset) || 0) > chunk.stat.size
          || Number(previous.mtimeMs) > chunk.stat.mtimeMs)
      const meta = reset ? { externalId: stemOf(file), cwd: '', title: '', createdAt: null } : {
        externalId: previous?.sessionId || stemOf(file),
        cwd: previous?.cwd || '',
        title: previous?.title || '',
        createdAt: previous?.createdAt || null,
      }
      const sent = reset ? 0 : Math.max(0, Number(previous?.sent) || 0)
      const messages = []
      let consumed = 0
      let processed = 0

      for (const line of chunk.lines) {
        if (messages.length >= budget) break
        const bytes = Buffer.byteLength(line, 'utf8') + 1
        processed += 1
        consumed += bytes
        if (!line.trim()) continue
        let event
        try {
          event = JSON.parse(line)
        } catch {
          continue
        }

        if (event && typeof event.sessionId === 'string' && event.sessionId) meta.externalId = event.sessionId
        if (event && typeof event.cwd === 'string' && event.cwd && !meta.cwd) meta.cwd = event.cwd
        if (event && event.type === 'summary' && typeof event.summary === 'string' && event.summary.trim()) {
          meta.title = event.summary.trim()
        }
        if (!meta.createdAt) meta.createdAt = isoOf(event && event.timestamp)

        const message = parseEvent(event)
        if (!message) continue
        const body = clip(message.text)
        if (!body) continue
        messages.push({
          seq: sent + messages.length,
          role: message.role,
          kind: 'message',
          text: body,
          time: isoOf(event && event.timestamp),
        })
      }

      if (budget > 0 && messages.length >= budget && processed < chunk.lines.length) {
        pending += chunk.lines.length - processed
      }
      budget -= messages.length

      nextFiles[file] = {
        offset: chunk.offset + consumed,
        mtimeMs: chunk.stat.mtimeMs,
        size: chunk.stat.size,
        sent: sent + messages.length,
        sessionId: meta.externalId,
        cwd: meta.cwd,
        title: meta.title,
        createdAt: meta.createdAt,
      }
      if (messages.length > 0) {
        sessions.push({
          externalId: meta.externalId,
          title: meta.title || meta.externalId,
          cwd: meta.cwd,
          createdAt: meta.createdAt,
          messages,
        })
      }
    }

    return {
      sessions,
      docs: [],
      next: { files: nextFiles, documents: {} },
      pending,
      skipped,
    }
  },
}
