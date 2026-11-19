/**
 * WorkBuddy 会话适配器。
 *
 * 会话：`<home>/.workbuddy/projects/**\/*.jsonl`，每行一个事件：
 *   - `{"type":"message","role":"user","content":[{"type":"input_text","text":...}]}`
 *   - `{"type":"message","role":"assistant","content":[{"type":"output_text","text":...}]}`
 *   - `{"type":"session-meta",...}` / `{"type":"ai-title",...}` / `function_call*`：只取元信息
 *     （sessionId / cwd / 标题），正文不入会话消息。
 *
 * 记忆文档：`<home>/.workbuddy/MEMORY.md`、`SOUL.md`、`USER.md`（根目录与一级
 * 子目录，见 `markdown.js`），走 `/remember` 落成 `kind: 'doc'`。
 *
 * 增量：按字节 offset 续读，只处理以 \n 结尾的完整行；文件变小或 mtime 回退
 * 视为被重写，从 0 重读。单文件 > 64 MB 整文件跳过。
 *
 * 会话 id：`<sessionId>-<sha256(文件绝对路径)[0:8]>`，与迁移工具/mem 里
 * omnihist 导入用的口径一致（见 `conversationIdOf`），否则「跳过已存在」失效。
 */
import { createHash } from 'node:crypto'
import { join } from 'node:path'

import { clip, isoOf, readJsonlChunk, stemOf, textOf, walkJsonl } from '../util.js'
import { scanDocs } from './markdown.js'

/**
 * Conversation id suffix: `sha256(absolute file path)` 前 8 位。
 *
 * 为什么要带这个后缀：mem 里的 workbuddy 会话是 omnihist 导入的，它的
 * `(source, external_id)` 有唯一约束，而同一个 sessionId 可能出现在多个 jsonl 里
 * （主会话 + subagents/、以及被复制到别的 project 目录的文件），所以它给每个
 * workbuddy 会话的 external_id 都加了「文件路径哈希」后缀，形如
 * `<sessionId>-61fc73b0`。汇集如果还用裸 sessionId，就会在 mem 里再建一套会话，
 * 「跳过已存在」永远命中不了，同一批消息也会被重复写一遍。
 * 这里按同一口径生成，两个写入方（迁移工具 / 汇集）落到同一个 conversation_id。
 * @param {string} file - Absolute session file path.
 * @returns {string} 8-char lowercase hex.
 */
function fileSuffix(file) {
  return createHash('sha256').update(file).digest('hex').slice(0, 8)
}

/**
 * Mem-compatible conversation id for one WorkBuddy session file.
 * @param {string} file - Absolute session file path.
 * @param {string} sessionId - Base session id (from session-meta or the file stem).
 * @returns {string} External id such as `d1b804b0-…-61fc73b0`.
 */
function conversationIdOf(file, sessionId) {
  return `${sessionId}-${fileSuffix(file)}`
}

/**
 * List WorkBuddy session files, bounded.
 * @param {string} home - User home.
 * @returns {string[]} Absolute jsonl paths.
 */
function walk(home) {
  return walkJsonl(join(home, '.workbuddy', 'projects'))
}

/**
 * Extract one message from a WorkBuddy event line.
 * @param {any} event - Parsed JSON line.
 * @returns {{role: string, text: string}|null} Text message, or null.
 */
function parseEvent(event) {
  if (!event || typeof event !== 'object') return null
  const type = String(event.type || '')
  if (type === 'message') {
    const role = String(event.role || '')
    const text = textOf(event.content)
    if (!text) return null
    if (role === 'user' || role === 'assistant' || role === 'system' || role === 'tool') return { role, text }
    return { role: 'assistant', text }
  }
  if (type === 'function_call_result') {
    const output = event.output
    const text = output && typeof output === 'object' && typeof output.text === 'string'
      ? output.text
      : textOf(output)
    return text ? { role: 'tool', text } : null
  }
  return null
}

export default {
  id: 'workbuddy',
  label: 'WorkBuddy',

  /**
   * @param {{home: string}} env - Environment.
   * @returns {{found: boolean, root: string, sessions: number}} Detection result.
   */
  detect(env) {
    const root = join(env.home, '.workbuddy', 'projects')
    const files = walk(env.home)
    return { found: files.length > 0, root, sessions: files.length }
  },

  /**
   * @param {{files?: Record<string, any>, documents?: Record<string, any>}} state - Per-tool state.
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

    const documents = scanDocs(join(env.home, '.workbuddy'), state && state.documents)
    skipped += documents.skipped

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

        if (event && event.type === 'session-meta') {
          if (typeof event.sessionId === 'string' && event.sessionId) meta.externalId = event.sessionId
          if (typeof event.cwd === 'string' && event.cwd) meta.cwd = event.cwd
        } else if (event && event.type === 'ai-title' && typeof event.aiTitle === 'string') {
          if (event.aiTitle.trim()) meta.title = event.aiTitle.trim()
        } else if (event && typeof event.cwd === 'string' && event.cwd && !meta.cwd) {
          meta.cwd = event.cwd
        }
        if (!meta.createdAt) meta.createdAt = isoOf(event && event.timestamp)

        const message = parseEvent(event)
        if (!message) continue
        const body = clip(message.text)
        if (!body) continue
        messages.push({
          seq: sent + messages.length,
          role: message.role,
          kind: message.role === 'tool' ? 'tool-result' : 'message',
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
          externalId: conversationIdOf(file, meta.externalId),
          title: meta.title || meta.externalId,
          cwd: meta.cwd,
          createdAt: meta.createdAt,
          messages,
        })
      }
    }

    return {
      sessions,
      docs: documents.docs,
      next: { files: nextFiles, documents: documents.next },
      pending,
      skipped,
    }
  },
}
