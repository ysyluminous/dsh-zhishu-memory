/**
 * 内置汇集的公用小工具：文本抽取、截断、哈希、目录遍历、jsonl 增量读。
 *
 * 只读本机文件，不发网络；所有函数都做「读不到就返回空」的降级，
 * 一个坏文件或坏行不该让整次汇集失败。
 */
import { closeSync, openSync, readdirSync, readSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join } from 'node:path'

/** 单文件上限：超过就整文件跳过（保护内存，也保护一次运行的时间）。 */
export const MAX_FILE_BYTES = 64 * 1024 * 1024

/** 单条消息正文上限（字符数，按码点截，不切坏 emoji / 中文）。 */
export const MAX_TEXT_CHARS = 8000

/** Markdown 文档正文上限；再大的记忆文件按截断收，避免一次 /remember 过大。 */
export const MAX_DOC_CHARS = 200_000

/**
 * Trim and cap one text value without breaking surrogate pairs.
 * @param {unknown} value - Raw text.
 * @param {number} [max] - Character cap.
 * @returns {string} Trimmed text, possibly truncated.
 */
export function clip(value, max = MAX_TEXT_CHARS) {
  const text = String(value ?? '').trim()
  if (!text) return ''
  const points = [...text]
  return points.length > max ? points.slice(0, max).join('') : text
}

/**
 * Pull only text blocks out of a message content array.
 * Reasoning / thinking / tool_use / image blocks are deliberately ignored.
 * @param {unknown} content - String or array of content blocks.
 * @returns {string} Joined text.
 */
export function textOf(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts = []
  for (const block of content) {
    if (!block || typeof block !== 'object') continue
    const type = String(block.type || '')
    if (type !== 'text' && type !== 'input_text' && type !== 'output_text') continue
    if (typeof block.text === 'string' && block.text) parts.push(block.text)
  }
  return parts.join('\n')
}

/**
 * Stable sha256 hex digest.
 * @param {unknown} value - Anything stringifiable.
 * @returns {string} 64-char lowercase hex.
 */
export function sha256(value) {
  return createHash('sha256').update(String(value)).digest('hex')
}

/**
 * Turn a millisecond timestamp / date string into ISO, or null.
 * @param {unknown} value - Timestamp in ms or parseable string.
 * @returns {string|null} ISO timestamp.
 */
export function isoOf(value) {
  if (value === undefined || value === null || value === '') return null
  const date = typeof value === 'number' ? new Date(value) : new Date(String(value))
  return Number.isNaN(date.getTime()) ? null : date.toISOString()
}

/**
 * List `*.jsonl` files below a directory, bounded by depth and file count.
 * Missing / unreadable directories yield an empty list.
 * @param {string} root - Directory to walk.
 * @param {{maxDepth?: number, limit?: number}} [options] - Bounds.
 * @returns {string[]} Absolute file paths.
 */
export function walkJsonl(root, options = {}) {
  const maxDepth = options.maxDepth ?? 6
  const limit = options.limit ?? 5000
  const out = []
  const visit = (dir, depth) => {
    if (out.length >= limit) return
    let entries = []
    try {
      entries = readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      if (out.length >= limit) return
      if (entry.isDirectory()) {
        if (depth < maxDepth) visit(join(dir, entry.name), depth + 1)
        continue
      }
      if (entry.isFile() && /\.jsonl$/i.test(entry.name)) out.push(join(dir, entry.name))
    }
  }
  visit(root, 0)
  return out
}

/**
 * Read the complete new lines of one jsonl file, starting at the stored byte offset.
 *
 * Only complete lines (ending in \n) are returned, and the caller tracks how many
 * bytes it actually consumed, so a half-written last line is retried next run
 * instead of being parsed as broken JSON.
 * @param {string} file - Absolute path.
 * @param {{offset?: number, size?: number, mtimeMs?: number}} [previous] - Stored per-file state.
 * @returns {{stat: import('node:fs').Stats, offset: number, lines: string[], tooLarge?: boolean, partial?: boolean}|null}
 *   Chunk, or null when the file disappeared.
 */
export function readJsonlChunk(file, previous) {
  let stat
  try {
    stat = statSync(file)
  } catch {
    return null
  }
  if (!stat.isFile()) return null
  if (stat.size > MAX_FILE_BYTES) return { stat, offset: 0, lines: [], tooLarge: true }

  const previousOffset = Math.max(0, Number(previous?.offset) || 0)
  const shrunk = previous !== undefined
    && (Number(previous.size) > stat.size || previousOffset > stat.size)
  const offset = shrunk ? 0 : previousOffset
  if (offset >= stat.size) return { stat, offset, lines: [] }

  let handle = null
  try {
    handle = openSync(file, 'r')
    const length = stat.size - offset
    const buffer = Buffer.alloc(length)
    readSync(handle, buffer, 0, length, offset)
    const lastNewline = buffer.lastIndexOf(0x0a)
    if (lastNewline < 0) return { stat, offset, lines: [], partial: true }
    const text = buffer.subarray(0, lastNewline + 1).toString('utf8')
    const lines = text.split('\n')
    lines.pop() // 末尾 \n 之后的空串不是一行
    return { stat, offset, lines }
  } catch {
    return null
  } finally {
    if (handle !== null) {
      try {
        closeSync(handle)
      } catch {
        // 只读句柄关闭失败无需处理
      }
    }
  }
}

/**
 * Human-friendly session title fallback from a file path.
 * @param {string} file - Session file path.
 * @returns {string} File stem.
 */
export function stemOf(file) {
  const name = String(file).split(/[\\/]/).pop() || 'session'
  return name.replace(/\.(jsonl|zstd)$/i, '') || 'session'
}
