/**
 * Markdown 记忆文档助手。
 *
 * 「任意 memory 目录下的 `*.md` → 文档入 mem」这件事本身没有固定的目录约定，
 * 所以这一层只提供两个可复用能力，由具体适配器决定扫哪里：
 *
 *   - `findMemoryDocs(root)`：找 `MEMORY.md` / `SOUL.md` / `USER.md`（根目录 + 一级子目录）
 *   - `scanDocs(root, previous)`：按 mtime+size+sha256 增量，只返回变过的文档
 *
 * WorkBuddy 适配器用它把 `<home>/.workbuddy` 下的三份记忆文件通过 `/remember`
 * 写成 `kind: 'doc'`。文档内容不改一个字节，只做长度上限保护。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { MAX_DOC_CHARS, clip, sha256 } from '../util.js'

/** 约定的记忆文件名（大小写敏感，与各工具落盘一致）。 */
export const MEMORY_FILE_NAMES = ['MEMORY.md', 'SOUL.md', 'USER.md']

/** 单份 Markdown 文件的读入上限；超过按截断收，不让一次 /remember 过大。 */
export const MAX_DOC_BYTES = 1024 * 1024

/**
 * Find memory markdown files in a root directory and its direct children.
 * Bounded on purpose: a WorkBuddy home has dozens of large sibling folders and
 * only the root / per-user folders carry these three files.
 * @param {string} root - Base directory (for example `<home>/.workbuddy`).
 * @returns {string[]} Absolute paths, de-duplicated.
 */
export function findMemoryDocs(root) {
  const found = []
  const push = (file) => {
    if (!found.includes(file)) found.push(file)
  }
  for (const name of MEMORY_FILE_NAMES) push(join(root, name))

  let entries = []
  try {
    entries = readdirSync(root, { withFileTypes: true })
  } catch {
    return found.filter((file) => exists(file))
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    for (const name of MEMORY_FILE_NAMES) push(join(root, entry.name, name))
  }
  return found.filter((file) => exists(file))
}

/**
 * @param {string} file - Candidate path.
 * @returns {boolean} Whether it is a regular file.
 */
function exists(file) {
  try {
    return statSync(file).isFile()
  } catch {
    return false
  }
}

/**
 * Read one memory document, truncated when needed.
 * @param {string} file - Absolute path.
 * @returns {{path: string, title: string, text: string, hash: string, mtimeMs: number, size: number, truncated: boolean}|null}
 *   Document, or null when unreadable.
 */
export function readDoc(file) {
  let stat
  try {
    stat = statSync(file)
  } catch {
    return null
  }
  if (!stat.isFile() || stat.size > MAX_DOC_BYTES) return null
  let raw
  try {
    raw = readFileSync(file, 'utf8')
  } catch {
    return null
  }
  const text = clip(raw, MAX_DOC_CHARS)
  if (!text) return null
  return {
    path: file,
    title: file,
    text,
    hash: sha256(text),
    mtimeMs: stat.mtimeMs,
    size: stat.size,
    truncated: text.length < raw.trim().length,
  }
}

/**
 * Incrementally scan the memory documents of one root.
 * @param {string} root - Base directory.
 * @param {Record<string, any>} [previous] - Stored document state.
 * @returns {{docs: any[], next: Record<string, any>, skipped: number}} New/changed docs plus next state.
 */
export function scanDocs(root, previous = {}) {
  const docs = []
  const next = {}
  let skipped = 0
  for (const file of findMemoryDocs(root)) {
    const before = previous[file]
    let stat
    try {
      stat = statSync(file)
    } catch {
      continue
    }
    if (stat.size > MAX_DOC_BYTES) {
      skipped += 1
      continue
    }
    if (before && before.mtimeMs === stat.mtimeMs && before.size === stat.size) {
      next[file] = before
      continue
    }
    const doc = readDoc(file)
    if (!doc) {
      if (before) next[file] = before
      continue
    }
    next[file] = { mtimeMs: stat.mtimeMs, size: stat.size, hash: doc.hash }
    if (before && before.hash === doc.hash) continue
    docs.push(doc)
  }
  return { docs, next, skipped }
}
