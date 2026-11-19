#!/usr/bin/env node
/**
 * 一把过三道闸门：发布自检 → 全新 profile 组合 → 全新 profile 安装。
 *
 * 为什么用 stdio: 'inherit' 而不是收集输出：这套 Electron-as-node 下，
 * 子进程的 stdout 一旦走管道 + process.exit，最后一行会被吞掉（实测过）。
 * 继承父进程的句柄就不会，而且人看日志也更直接。
 *
 * 用法：
 *   node tools/verify.mjs
 *
 * 退出码：0 = 全过（或跳过的都属环境不具备）；1 = 有失败。
 */
import { dirname, join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const gates = [
  { name: 'pack-check', file: join(here, 'pack-check.mjs'), args: [] },
  { name: 'fresh-profile (link)', file: join(here, 'fresh-profile.mjs'), args: [] },
  { name: 'fresh-profile (install)', file: join(here, 'fresh-profile.mjs'), args: ['--install'] },
]

const results = []
for (const gate of gates) {
  process.stdout.write(`\n── ${gate.name} ──\n`)
  const started = Date.now()
  const result = spawnSync(process.execPath, [gate.file, ...gate.args], {
    stdio: 'inherit',
    env: process.env,
    timeout: 600000,
  })
  results.push({
    name: gate.name,
    code: result.status,
    skipped: result.status === 2,
    ms: Date.now() - started,
  })
}

const failed = results.filter((item) => item.code !== 0 && !item.skipped)
const skipped = results.filter((item) => item.skipped)
process.stdout.write('\n── 汇总 ──\n')
for (const item of results) {
  const verdict = item.skipped ? 'SKIP' : item.code === 0 ? 'PASS' : 'FAIL'
  process.stdout.write(`${verdict.padEnd(5)} ${item.name}  ${(item.ms / 1000).toFixed(1)}s\n`)
}
if (skipped.length) process.stdout.write('（SKIP = 环境不具备，比如缺少 DSH_NODE_EXECUTABLE）\n')
process.exit(failed.length === 0 ? 0 : 1)
