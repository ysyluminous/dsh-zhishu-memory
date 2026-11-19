/**
 * Codex 会话适配器。
 *
 * 位置：`<home>/.codex/**\/*.jsonl`
 *
 * Codex 的本地日志版本差异较大，这里的原则是「能解析就收，解析不出就跳过」，
 * 永不因为一行不认识而报错。识别顺序：
 *   1. `{"type":"message","role":...,"content":[...]}`（Codex CLI 的 message 事件）
 *   2. `{"type":"response_item","payload":{"type":"message","role":...,"content":[...]}}`
 *   3. `{"type":"event_msg","payload":{"type":"agent_message"|"user_message","message":...}}`
 *   4. 兜底：顶层有 `role` + `content`/`text` 就当消息
 *
 * 只收文本段；`reasoning`、`function_call`、`token_count`、`session_meta` 等一律跳过。
 */
import { join } from 'node:path'

import { clip, isoOf, readJsonlChunk, stemOf, textOf, walkJsonl } from '../util.js'

const ROLES = new Set(['user', 'assistant', 'system', 'tool'])

/**
 * List Codex jsonl files, bounded.
 * @param {string} home - User home.
 * @returns {string[]} Absolute jsonl paths.
 */
function walk(home) {
  return walkJsonl(join(home, '.codex'), { maxDepth: 6, limit: 2000 })
}

/**
 * Normalise a role string.
 * @param {unknown} role - Raw role.
 * @returns {string} One of user / assistant / system / tool.
 */
function roleOf(role) {
  const value = String(role || '').toLowerCase()
  return ROLES.has(value) ? value : 'assistant'
}

/**
 * Try to read one message out of a Codex line; unknown shapes return null.
 * @param {any} event - Parsed JSON line.
 * @returns {{role: string, text: string}|null} Text message, or null.
 */
function parseEvent(event) {
  if (!event || typeof event !== 'object') return null
  const type = String(event.type || '')
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : null

  if (type === 'message' && event.role) {
    const text = textOf(event.content)
    return text ? { role: roleOf(event.role), text } : null
  }
  if (payload && payload.type === 'message' && payload.role) {
    const text = textOf(payload.content)
    return text ? { role: roleOf(payload.role), text } : null
  }
  if (payload && (payload.type === 'agent_message' || payload.type === 'user_message'
    || payload.type === 'assistant_message')) {
    const text = typeof payload.message === 'string' ? payload.message : textOf(payload.content)
    if (text) return { role: roleOf(payload.role || (payload.type === 'user_message' ? 'user' : 'assistant')), text }
  }
  if (event.role && event.content !== undefined) {
    const text = textOf(event.content)
    if (text) return { role: roleOf(event.role), text }
  }
  if (typeof event.text === 'string' && event.text.trim() && event.role) {
    return { role: roleOf(event.role), text: event.text }
  }
  return null
}

export default {
  id: 'codex',
  label: 'Codex',

  /**
   * @param {{home: string}} env - Environment.
   * @returns {{found: boolean, root: string, sessions: number}} Detection result.
   */
  detect(env) {
    const root = join(env.home, '.codex')
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

        if (event && typeof event.session_id === 'string' && event.session_id) meta.externalId = event.session_id
        if (event && typeof event.cwd === 'string' && event.cwd && !meta.cwd) meta.cwd = event.cwd
        if (!meta.createdAt) meta.createdAt = isoOf(event && (event.timestamp || event.ts))

        const message = parseEvent(event)
        if (!message) continue
        const body = clip(message.text)
        if (!body) continue
        messages.push({
          seq: sent + messages.length,
          role: message.role,
          kind: message.role === 'tool' ? 'tool-result' : 'message',
          text: body,
          time: isoOf(event && (event.timestamp || event.ts)),
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
