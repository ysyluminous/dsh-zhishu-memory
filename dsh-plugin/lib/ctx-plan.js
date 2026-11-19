/**
 * Context-plan helpers for the zhishu memory bridge.
 *
 * Pure functions only: message snapshotting, turn splitting, and translating a
 * server-side plan ("drop the oldest N turns") into one balanced surface range
 * the compaction engine accepts. Nothing here throws into the pre-step path.
 *
 * Why the snapshot is capped: `deriveMessages()` on a long-lived session returns
 * thousands of blocks, and posting all of them on every turn would add
 * megabytes to a path whose whole budget is a few hundred milliseconds. The cap
 * is recorded in the trace (`truncated` / `dropped`) so a decision made on a
 * partial history is never mistaken for one made on the whole.
 */

import { createHash } from 'node:crypto'

/**
 * Extract plain text from one content part.
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
export function contentText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.map(partText).join('')
}

/**
 * Take a bounded, text-flattened snapshot of the derived model history.
 *
 * `nodes` is the parallel array of surface seqs, and it is only returned when
 * its length matches the message count exactly — a substitution on a guessed
 * mapping would compact the wrong span, so a mismatch means "observe only".
 *
 * @param {any} session - Agent-owned session.
 * @param {{maxMessages?: number, maxChars?: number}} [opts] - Caps.
 * @returns {{messages: Array<{role: string, content: string}>, nodes: number[] | null,
 *            total: number, truncated: boolean, dropped: number}} Snapshot.
 */
export function snapshotHistory(session, opts = {}) {
  const maxMessages = Math.max(1, Number(opts.maxMessages) || 300)
  const maxChars = Math.max(200, Number(opts.maxChars) || 1500)
  let all = []
  try {
    all = session.deriveMessages() || []
  } catch {
    all = []
  }
  const total = all.length
  const start = Math.max(0, total - maxMessages)
  const messages = []
  for (let index = start; index < total; index += 1) {
    const message = all[index] || {}
    const text = contentText(message.content)
    messages.push({
      role: typeof message.role === 'string' ? message.role : '',
      content: text.length > maxChars ? text.slice(0, maxChars) : text,
    })
  }
  let nodes = null
  try {
    const surfaceNodes = session.surface && session.surface.nodes
    if (Array.isArray(surfaceNodes) && surfaceNodes.length === total) {
      nodes = surfaceNodes.slice(start)
    }
  } catch {
    nodes = null
  }
  return { messages, nodes, total, truncated: total > messages.length, dropped: start }
}

/**
 * Split messages into turns starting at each user message.
 *
 * System/developer messages are not part of a turn and are never candidates for
 * removal; a non-user prefix (rare) is folded into the first turn so indices
 * stay contiguous.
 *
 * @param {ReadonlyArray<{role: string}>} messages - Snapshot messages.
 * @returns {Array<{firstIdx: number, lastIdx: number, msgCount: number}>} Turns.
 */
export function splitTurns(messages) {
  const turns = []
  let current = null
  for (let index = 0; index < messages.length; index += 1) {
    const role = String(messages[index] && messages[index].role)
    if (role === 'system' || role === 'developer') {
      if (current) {
        turns.push(current)
        current = null
      }
      continue
    }
    if (role === 'user' || current === null) {
      if (current) turns.push(current)
      current = { firstIdx: index, lastIdx: index, msgCount: 1 }
    } else {
      current.lastIdx = index
      current.msgCount += 1
    }
  }
  if (current) turns.push(current)
  return turns
}

/**
 * Translate "drop the oldest N turns" into one inclusive surface seq range.
 *
 * The first turn (the anchor that carries the original task) and the most recent
 * `keepRecent` turns are protected here exactly as they are on the server, so
 * both sides choose the same turns from the same messages.
 *
 * @param {ReadonlyArray<{role: string}>} messages - Snapshot messages.
 * @param {ReadonlyArray<number> | null} nodes - Parallel surface seqs.
 * @param {number} dropCount - Turns the server asked to drop.
 * @param {number} [keepRecent] - Turns protected at the tail.
 * @returns {{startSeq: number, endSeq: number, turnCount: number} | null} Range.
 */
export function selectSurfaceRange(messages, nodes, dropCount, keepRecent = 6) {
  if (!Array.isArray(nodes) || nodes.length !== messages.length) return null
  const requested = Math.max(0, Math.floor(Number(dropCount) || 0))
  if (requested === 0) return null
  const turns = splitTurns(messages)
  if (turns.length === 0) return null
  const keep = Math.max(0, Math.floor(Number(keepRecent) || 0))
  const protectedIdx = new Set()
  protectedIdx.add(turns[0].firstIdx)
  for (const turn of turns.slice(Math.max(0, turns.length - keep))) protectedIdx.add(turn.firstIdx)
  const candidates = turns.filter(turn => !protectedIdx.has(turn.firstIdx))
  if (candidates.length === 0) return null
  const chosen = candidates.slice(0, Math.min(requested, candidates.length))
  const first = chosen[0]
  const last = chosen[chosen.length - 1]
  const startSeq = nodes[first.firstIdx]
  const endSeq = nodes[last.lastIdx]
  if (!Number.isInteger(startSeq) || !Number.isInteger(endSeq) || startSeq > endSeq) return null
  return { startSeq, endSeq, turnCount: chosen.length }
}

/**
 * Stable digest of a message array, used to prove what a trace replaced.
 * @param {ReadonlyArray<unknown>} messages - Messages to hash.
 * @returns {string} `sha256:<hex>`.
 */
export function hashMessages(messages) {
  try {
    return `sha256:${createHash('sha256').update(JSON.stringify(messages)).digest('hex')}`
  } catch {
    return ''
  }
}
