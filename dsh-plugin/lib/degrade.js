/**
 * 门控降级的分类与文案（纯函数，便于单测）。
 *
 * 老大 2026-11-18 定的口径：
 *   1. 三种空必须分开：真的无命中（empty）/ 检索失败（failed）/ 超时（timeout）。
 *      以前三者在模型眼里长得一模一样（都是"没有那条消息"），这是最坏的失败模式：
 *      模型把"检索没跑成"当成"确实没有历史"。
 *   2. empty 的文案必须中性：明说「不是失败」，不许让模型怀疑系统。
 *   3. failed / timeout 必须明说「这不是没有相关记忆」，并给出动作（自己去 search）。
 *   4. 连续失败到阈值后压成一行（省 token），但**每轮仍然写 audit**；
 *      恢复的那一轮必须明确说一次「已恢复」，否则模型会一直以为自己没有记忆。
 *
 * 为什么抽成纯函数：注入文案写错了不会有任何报警，只有人读得出来。抽出来才能被 harness 断言。
 */

/** 四种结局。hit 走原有渲染，其余三种都要注入说明。 */
export const OUTCOME = {
  HIT: 'hit',
  EMPTY: 'empty',
  TIMEOUT: 'timeout',
  FAILED: 'failed',
}

/** 连续失败到第几次开始压缩文案（老大 2026-11-18 定 3）。 */
export const THROTTLE_AFTER = 3

const HEAD = '## 记忆检索（zhishu-memory）'

/**
 * 把 `/gate` 的返回值归成四种结局。
 * @param {any} data - postJson 的返回值。
 * @returns {string} OUTCOME 之一。
 */
export function classify(data) {
  if (data && data.ok) {
    const count = Number(data.count)
    const hits = Array.isArray(data.results) ? data.results.length : 0
    return (Number.isFinite(count) ? count : hits) > 0 ? OUTCOME.HIT : OUTCOME.EMPTY
  }
  return data && data.timeout === true ? OUTCOME.TIMEOUT : OUTCOME.FAILED
}

/**
 * 失败细类，只进 audit，不进模型文案。
 * @param {any} data - postJson 的返回值。
 * @returns {'timeout'|'http'|'network'} 细类。
 */
export function failClass(data) {
  if (data && data.timeout === true) return 'timeout'
  if (data && typeof data.status === 'number') return 'http'
  return 'network'
}

/** 真的没命中：中性说明，重点是否掉「系统坏了」。 */
export function emptyText() {
  return `${HEAD}：本轮检索无命中
检索正常返回 0 条（**不是失败**，服务是通的）。需要历史的判断自己调 zhishu-memory.search。`
}

/**
 * 超时：必须点明「不是没有相关记忆」。
 * @param {{timeoutMs: number}} info - 本次超时预算。
 * @returns {string} 注入文本。
 */
export function timeoutText({ timeoutMs }) {
  const seconds = Math.max(1, Math.round((Number(timeoutMs) || 10000) / 1000))
  return `${HEAD}：本轮检索超时（>${seconds}s）
**这不是「没有相关记忆」**：这一轮没拿到检索结果。拿不准的事先调 zhishu-memory.search 再下结论。`
}

/**
 * 其他失败（连不上 / 4xx / 5xx）：同样必须点明。
 * @param {{status?: number}} info - HTTP 状态码（有则带上）。
 * @returns {string} 注入文本。
 */
export function failedText({ status } = {}) {
  const reason = typeof status === 'number' ? `HTTP ${status}` : '连不上 41012'
  return `${HEAD}：本轮检索失败（${reason}）
**这不是「没有相关记忆」**：这一轮没拿到检索结果。需要历史的判断先调 zhishu-memory.search。`
}

/**
 * 连续失败节流文案：只保一句，但每轮仍写 audit。
 * @param {{streak: number}} info - 当前连续失败轮数。
 * @returns {string} 注入文本。
 */
export function throttleText({ streak }) {
  return `${HEAD}：连续第 ${Math.max(1, Number(streak) || 1)} 轮检索失败（同上，这轮没拿到检索结果）`
}

/**
 * 恢复说明：节流期间模型被告知"没记忆"，恢复后不说一次，它就永远以为自己瞎着。
 * @param {{count: number, after: number}} info - 本轮命中数与恢复前的连续失败轮数。
 * @returns {string} 注入文本。
 */
export function recoveredText({ count, after }) {
  const n = Math.max(1, Number(after) || 1)
  const hit = Math.max(0, Number(count) || 0)
  return `${HEAD}：已恢复（上一轮${n > 1 ? `连续 ${n} 轮` : ''}失败/超时已结束，本轮正常返回 ${hit} 条）`
}
