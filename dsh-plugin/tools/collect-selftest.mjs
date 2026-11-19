#!/usr/bin/env node
/**
 * 内置汇集（collect）自检 —— 不连真实记忆服务、不碰真实 `~/.dsh`。
 *
 * 做法：
 *   1. 在系统临时目录造一个一次性 USERPROFILE（结束时删掉）；
 *   2. 造假数据：2 个 WorkBuddy jsonl、1 个 DSH 多帧 zstd、1 个 MEMORY.md；
 *   3. 直接跑 `scan`：断言会话数 / 消息数 / 文档数；
 *   4. 用第一次的 next 状态再跑一次：断言 0 新增；
 *   5. 再起一个本机假服务，跑完整的 `collectNow`：断言 /ingest 与 /remember
 *      的落库口径（conversation_id、event_id 唯一、kind=doc），第二次 0 新增，
 *      追加一行后只同步 1 条；
 *   6. 单独验证单次上限：limit=2 时只出 2 条、下一轮补 1 条。
 *
 * 用法：
 *   node tools/collect-selftest.mjs
 *
 * 退出码：0 = 全部 PASS；1 = 有 FAIL。
 */
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { zstdCompressSync } from 'node:zlib'

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok: Boolean(ok), detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  [${detail}]` : ''}`)
}

/** Write one zstd frame (DSH appends one frame per flush). */
function frame(lines) {
  return zstdCompressSync(Buffer.from(`${lines.join('\n')}\n`, 'utf8'))
}

/** Make one WorkBuddy event line. */
function wbMessage(role, text) {
  return JSON.stringify({
    type: 'message',
    role,
    content: [{ type: role === 'user' ? 'input_text' : 'output_text', text }],
    timestamp: Date.UTC(2026, 10, 19, 1, 0, 0),
    sessionId: 'fixture',
  })
}

/** Fail hard (thrown errors become one FAIL row). */
function need(condition, name, detail) {
  check(name, condition, detail)
}

const home = mkdtempSync(join(tmpdir(), 'dsh-collect-'))
// 一次性 USERPROFILE：模块里的 homedir() 也只会看到这个临时目录。
process.env.USERPROFILE = home
process.env.HOME = home

let server = null
try {
  const { default: dsh } = await import('../lib/collect/adapters/dsh.js')
  const { default: workbuddy } = await import('../lib/collect/adapters/workbuddy.js')
  const { collectNow, collectStatus } = await import('../lib/collect/index.js')

  // ── 造数据 ────────────────────────────────────────────────
  const wbRoot = join(home, '.workbuddy', 'projects')
  const wbA = join(wbRoot, 'demo-a', 'session-a.jsonl')
  const wbB = join(wbRoot, 'demo-b', 'session-b.jsonl')
  mkdirSync(dirname(wbA), { recursive: true })
  mkdirSync(dirname(wbB), { recursive: true })
  writeFileSync(wbA, [
    JSON.stringify({ type: 'session-meta', sessionId: 'wb-a', timestamp: Date.UTC(2026, 10, 19, 1, 0, 0) }),
    wbMessage('user', 'WB user 1'),
    wbMessage('assistant', 'WB assistant 1'),
    wbMessage('user', 'WB user 2'),
    '',
  ].join('\n'), 'utf8')
  writeFileSync(wbB, [
    JSON.stringify({ type: 'session-meta', sessionId: 'wb-b', timestamp: Date.UTC(2026, 10, 19, 1, 0, 0) }),
    wbMessage('user', 'WB user b1'),
    '',
  ].join('\n'), 'utf8')

  const memory = join(home, '.workbuddy', 'MEMORY.md')
  writeFileSync(memory, '# 记忆\n\n- 内置汇集自检用文档\n', 'utf8')

  const dshFile = join(home, '.dsh', 'sessions', 'ws', 'session-fixture-1', 'session.v4.jsonl.zstd')
  mkdirSync(dirname(dshFile), { recursive: true })
  const frames = [
    // 每个帧单独压，模拟 DSH 的追加写；只解第一帧的实现会漏掉后三帧。
    frame([JSON.stringify({
      type: 'session',
      version: 4,
      id: 'session-fixture-1',
      createdAt: Date.UTC(2026, 10, 19, 1, 0, 0),
      cwd: 'C:/work/demo',
    })]),
    frame([JSON.stringify({
      type: 'user/message',
      seq: 2,
      time: Date.UTC(2026, 10, 19, 1, 0, 1),
      data: { role: 'user', content: [{ type: 'text', text: 'DSH user 1' }] },
    })]),
    // 插件注入的召回快照：不该算用户发言。
    frame([JSON.stringify({
      type: 'user/message',
      seq: 3,
      time: Date.UTC(2026, 10, 19, 1, 0, 2),
      data: {
        role: 'user',
        content: [{ type: 'text', text: '## Possibly relevant memory\nshould be skipped' }],
        source: { kind: 'ds-bot-memory-recall' },
      },
    })]),
    frame([JSON.stringify({
      type: 'assistant/message',
      seq: 4,
      time: Date.UTC(2026, 10, 19, 1, 0, 3),
      data: { message: { role: 'assistant', content: [{ type: 'text', text: 'DSH assistant 1' }] } },
    })]),
    frame([JSON.stringify({
      type: 'tool/result',
      seq: 5,
      time: Date.UTC(2026, 10, 19, 1, 0, 4),
      data: { message: { role: 'tool', content: [{ type: 'text', text: 'DSH tool 1' }] } },
    })]),
  ]
  writeFileSync(dshFile, Buffer.concat(frames))

  // ── 1. 直接 scan ──────────────────────────────────────────
  const wbState = { files: {}, documents: {} }
  const wbFirst = workbuddy.scan(wbState, { home, limit: 2000 })
  const wbMessages = wbFirst.sessions.reduce((sum, session) => sum + session.messages.length, 0)
  need(wbFirst.sessions.length === 2, 'workbuddy: 2 个会话', `got ${wbFirst.sessions.length}`)
  need(wbMessages === 4, 'workbuddy: 4 条消息', `got ${wbMessages}`)
  need(wbFirst.docs.length === 1, 'workbuddy: MEMORY.md 入文档', `got ${wbFirst.docs.length}`)
  need(wbFirst.docs[0] && wbFirst.docs[0].text.includes('内置汇集自检用文档'), 'workbuddy: 文档正文正确')

  const wbSecond = workbuddy.scan(
    { files: wbFirst.next.files, documents: wbFirst.next.documents },
    { home, limit: 2000 },
  )
  const wbSecondMessages = wbSecond.sessions.reduce((sum, session) => sum + session.messages.length, 0)
  need(wbSecondMessages === 0 && wbSecond.docs.length === 0, 'workbuddy: 第二次 0 新增', `msg=${wbSecondMessages} doc=${wbSecond.docs.length}`)

  const dshState = { files: {} }
  const dshFirst = dsh.scan(dshState, { home, limit: 2000 })
  const dshMessages = dshFirst.sessions.reduce((sum, session) => sum + session.messages.length, 0)
  need(dshFirst.sessions.length === 1, 'dsh: 1 个会话', `got ${dshFirst.sessions.length}`)
  need(dshMessages === 3, 'dsh: 3 条消息（召回注入被跳过）', `got ${dshMessages}`)
  need(
    dshFirst.sessions[0] && dshFirst.sessions[0].messages.map((item) => item.role).join(',') === 'user,assistant,tool',
    'dsh: 角色顺序 user,assistant,tool',
  )

  const dshSecond = dsh.scan({ files: dshFirst.next.files }, { home, limit: 2000 })
  const dshSecondMessages = dshSecond.sessions.reduce((sum, session) => sum + session.messages.length, 0)
  need(dshSecondMessages === 0, 'dsh: 第二次 0 新增', `msg=${dshSecondMessages}`)

  // ── 2. 单次上限 ────────────────────────────────────────────
  const capFirst = dsh.scan({ files: {} }, { home, limit: 2 })
  const capFirstMessages = capFirst.sessions.reduce((sum, session) => sum + session.messages.length, 0)
  need(capFirstMessages === 2, 'dsh: limit=2 只出 2 条', `got ${capFirstMessages}`)
  const capSecond = dsh.scan({ files: capFirst.next.files }, { home, limit: 2000 })
  const capSecondMessages = capSecond.sessions.reduce((sum, session) => sum + session.messages.length, 0)
  need(capSecondMessages === 1, 'dsh: 下一轮补 1 条', `got ${capSecondMessages}`)
  need(capFirst.pending >= 1, 'dsh: 超出部分记 pending', `got ${capFirst.pending}`)

  // ── 3. 完整 run（本机假服务，不出网）──────────────────────
  const received = { ingest: [], remember: [] }
  server = createServer((req, res) => {
    const chunks = []
    req.on('data', (chunk) => chunks.push(chunk))
    req.on('end', () => {
      const body = chunks.length ? Buffer.concat(chunks).toString('utf8') : ''
      res.setHeader('content-type', 'application/json')
      if (req.method === 'GET' && req.url === '/health') {
        res.end(JSON.stringify({ ok: true, service: 'collect-selftest' }))
        return
      }
      if (req.method === 'POST' && req.url === '/ingest') {
        received.ingest.push(JSON.parse(body))
        res.end(JSON.stringify({ ok: true, id: 'evt' }))
        return
      }
      if (req.method === 'POST' && req.url === '/remember') {
        received.remember.push(JSON.parse(body))
        res.end(JSON.stringify({ ok: true, id: 'doc-1' }))
        return
      }
      res.statusCode = 404
      res.end(JSON.stringify({ ok: false, error: 'not found' }))
    })
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const serviceUrl = `http://127.0.0.1:${server.address().port}`
  const statePath = join(home, 'state', 'collect-state.json')

  const firstRun = await collectNow({ home, serviceUrl, statePath, tools: ['dsh', 'workbuddy'] })
  need(firstRun.ok === true, 'run: 第一次 ok', firstRun.error || '')
  need(firstRun.messages === 7, 'run: 第一次 7 条消息', `got ${firstRun.messages}`)
  need(firstRun.docs === 1, 'run: 第一次 1 份文档', `got ${firstRun.docs}`)
  need(received.ingest.length === 7, 'run: /ingest 收到 7 条', `got ${received.ingest.length}`)
  need(received.remember.length === 1, 'run: /remember 收到 1 份', `got ${received.remember.length}`)
  const eventIds = new Set(received.ingest.map((item) => item.event_id))
  need(eventIds.size === received.ingest.length, 'run: event_id 互不重复', `${eventIds.size}/${received.ingest.length}`)
  need(
    received.ingest.every((item) => typeof item.conversation_id === 'string' && item.conversation_id.includes(':')),
    'run: conversation_id = "<tool>:<session>"',
  )
  const workbuddyIds = received.ingest.filter((item) => item.source === 'workbuddy').map((item) => item.conversation_id)
  need(
    workbuddyIds.length > 0 && workbuddyIds.every((id) => /^workbuddy:(wb-a|wb-b)-[0-9a-f]{8}$/.test(id)),
    'run: workbuddy 会话 id 带文件哈希后缀（与 mem 口径一致）',
    workbuddyIds[0] || '',
  )
  need(
    received.ingest.every((item) => item.source === 'dsh' || item.source === 'workbuddy'),
    'run: source 是适配器 id',
  )
  const remembered = received.remember[0]
  need(remembered && remembered.kind === 'doc' && remembered.title.endsWith('MEMORY.md'), 'run: /remember 口径 kind=doc + title=路径')

  const secondRun = await collectNow({ home, serviceUrl, statePath, tools: ['dsh', 'workbuddy'] })
  need(secondRun.ok === true, 'run: 第二次 ok')
  need(secondRun.messages === 0 && secondRun.docs === 0, 'run: 第二次 0 新增', `msg=${secondRun.messages} doc=${secondRun.docs}`)
  need(received.ingest.length === 7 && received.remember.length === 1, 'run: 第二次没有重复上传')

  appendFileSync(wbB, `${wbMessage('assistant', 'WB assistant b2')}\n`, 'utf8')
  const thirdRun = await collectNow({ home, serviceUrl, statePath, tools: ['dsh', 'workbuddy'] })
  need(thirdRun.messages === 1, 'run: 追加一行后只同步 1 条', `got ${thirdRun.messages}`)

  const status = await collectStatus({ home, serviceUrl, statePath })
  const wbStatus = status.tools.find((item) => item.id === 'workbuddy')
  need(Boolean(wbStatus && wbStatus.found), 'status: workbuddy 被发现', wbStatus ? `sessions=${wbStatus.localSessions}` : '')
  need(Boolean(wbStatus && wbStatus.syncedMessages >= 5), 'status: 已同步计数前进', wbStatus ? `synced=${wbStatus.syncedMessages}` : '')
  need(status.tools.length === 4, 'status: 四个工具都在', `got ${status.tools.length}`)

  // ── 4. 加锁：并发第二次直接 busy ────────────────────────────
  const concurrent = collectNow({ home, serviceUrl, statePath, tools: ['workbuddy'] })
  const busyRun = await collectNow({ home, serviceUrl, statePath, tools: ['workbuddy'] })
  need(busyRun.busy === true && busyRun.ok === false, 'run: 并发第二次被锁住', `busy=${busyRun.busy}`)
  const concurrentResult = await concurrent
  need(concurrentResult.ok === true, 'run: 并发第一次正常收尾')

  // ── 5. 服务不可达：只降级 + 写审计，不抛 ─────────────────────
  const down = await collectNow({ home, serviceUrl: 'http://127.0.0.1:1', statePath, tools: ['workbuddy'] })
  need(down.ok === false && /不可达/.test(String(down.error || '')), 'run: 服务不可达时优雅降级', String(down.error || '').slice(0, 40))
  const auditText = readFileSync(join(home, 'state', 'audit.jsonl'), 'utf8')
  need(auditText.includes('"tool":"*"') && auditText.includes('"ok":false'), 'run: 失败写进 audit.jsonl')

  const okCount = results.filter((item) => item.ok).length
  console.log(`\nRESULT: ${okCount === results.length ? 'PASS' : 'FAIL'} (${okCount}/${results.length})`)
  if (okCount === results.length) rmSync(home, { recursive: true, force: true })
  else console.log(`（保留失败现场：${home}）`)
  process.exitCode = okCount === results.length ? 0 : 1
} catch (error) {
  console.log(`\nFAIL  自检异常：${String((error && error.stack) || error)}`)
  console.log(`（保留失败现场：${home}）`)
  process.exitCode = 1
} finally {
  if (server) await new Promise((resolve) => server.close(resolve))
}
