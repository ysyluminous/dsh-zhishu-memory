#!/usr/bin/env node
/**
 * 全新 profile 自检：证明这个 bundle 在「别人的 DSH_HOME」里装得上、组合得出来。
 *
 * 两种模式：
 *   （默认）link 模式——把包目录 junction 进临时 profile，只验「四行能不能组合」；
 *   --install 模式——真的 `npm pack` 出 tarball，再用 DSH 自己的
 *     `dsh plugin --profile probe add <tarball>` 装一遍（pnpm），最后 dump-config。
 *     这是「发布物在干净 profile 里安装」的最强本地证据。
 *
 * 全程用一个一次性的 DSH_HOME，**不启动任何服务**：`--dump-config` 组合配置就退出。
 * 需要 DSH_NODE_EXECUTABLE（Electron 可执行文件）与 DSH_INSTALL_DIR（@deepseek-ai/dsh 包，
 * 内有 lib/bin.js）；拿不到就报「跳过」并退出码 2，不假装通过。
 *
 * 用法：
 *   node tools/fresh-profile.mjs
 *   node tools/fresh-profile.mjs --install
 *   node tools/fresh-profile.mjs --json
 *
 * 退出码 0 = 通过；1 = 失败；2 = 环境不具备。
 *
 * 关于输出（踩过的坑）：Electron-as-node 下 `console.log` 走的是异步管道，
 * 紧接着 `process.exit()` 会把最后一行吞掉；而 `writeSync(1, …)` 在它的 fd 上会抛错。
 * 所以这里统一用 `process.stdout.write` + 设置 `exitCode` + 自然退出，并用一个哨兵
 * 异常把控制流停住（模块顶层不能 `return`）。
 */
import { existsSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(here, '..')
const asJson = process.argv.includes('--json')
const installMode = process.argv.includes('--install')

/** Stop the script after reporting; the code travels on the sentinel. */
class Reported extends Error {
  constructor(code) {
    super('reported')
    this.code = code
  }
}

const finish = (ok, message, detail = {}, code = ok ? 0 : 1) => {
  const text = asJson
    ? JSON.stringify({ ok, mode: installMode ? 'install' : 'link', message, ...detail }, null, 2)
    : `${ok ? '✓' : '✗'} ${message}${detail.stderr ? `\n${detail.stderr}` : ''}${detail.stdout ? `\n${detail.stdout}` : ''}`
  // console.log 在这套 Electron-as-node 下是唯一稳定能被父进程捕获的输出方式
  // （process.stdout.write + exitCode、writeSync(1, …) 都试过：前者偶尔一个字都不出）。
  console.log(text)
  process.exit(code)
}

const main = () => {
  const exe = process.env.DSH_NODE_EXECUTABLE
  const installDir = process.env.DSH_INSTALL_DIR
  const bin = installDir ? join(installDir, 'lib', 'bin.js') : null
  if (!exe || !bin || !existsSync(exe) || !existsSync(bin)) {
    finish(false, '跳过：需要 DSH_NODE_EXECUTABLE 与 DSH_INSTALL_DIR', { skipped: true, reason: 'env' }, 2)
  }

  const expectedRows = ['zhishu-memory', 'ctx-guard', 'redline-guard', 'mem-float']
  const stamp = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
  const home = join(tmpdir(), `dsh-fresh-${stamp}`)
  const dshHome = join(home, '.dsh')
  const profileDir = join(dshHome, 'profiles', 'probe')
  const env = { ...process.env, ELECTRON_RUN_AS_NODE: '1', USERPROFILE: home, HOME: home, DSH_HOME: dshHome }
  const runCli = (args, timeout = 240000) => spawnSync(exe, [bin, ...args], { encoding: 'utf8', timeout, windowsHide: true, env })

  mkdirSync(profileDir, { recursive: true })
  const profileManifest = {
    name: 'dsh-profile-probe',
    private: true,
    ...(installMode ? {} : {
      dependencies: { '@asywr/dsh-zhishu-memory': `link:${packageRoot.replace(/\\/g, '/')}` },
      dsh: { profile: { bundles: ['@asywr/dsh-zhishu-memory'] } },
    }),
  }
  writeFileSync(join(profileDir, 'package.json'), `${JSON.stringify(profileManifest, null, 2)}\n`, 'utf8')
  writeFileSync(join(profileDir, 'cordis.yml'), '[]\n', 'utf8')

  if (installMode) {
    // 真打包 + 真安装：tarball 直接生成到临时 home，不在仓库目录留垃圾。
    // 两个坑都在这里：Node 不能直接 spawnSync('npm.cmd')（对 .cmd 要求 shell）；
    // 而把整条命令拼成一个字符串给 cmd /c，引号会被 cmd 多加一层，npm 会把
    // `"C:\..."` 当成相对路径。所以拆成参数数组交给 cmd 自己拼。
    const packed = process.platform === 'win32'
      ? spawnSync('cmd.exe', ['/c', 'npm', 'pack', '--pack-destination', home], { cwd: packageRoot, encoding: 'utf8', timeout: 180000, windowsHide: true })
      : spawnSync('npm', ['pack', '--pack-destination', home], { cwd: packageRoot, encoding: 'utf8', timeout: 180000 })
    if (packed.status !== 0) {
      finish(false, 'npm pack 失败', {
        stderr: String(packed.stderr || packed.error?.message || '').slice(0, 400),
        stdout: String(packed.stdout || '').slice(0, 400),
      })
    }
    const tarball = String(packed.stdout || '').trim().split(/\r?\n/).pop()
    const tarballPath = join(home, tarball)
    if (!existsSync(tarballPath)) finish(false, '找不到打出来的 tarball', { tarball })

    const added = runCli(['plugin', '--profile', 'probe', 'add', tarballPath.replace(/\\/g, '/')])
    const addedOut = `${added.stdout || ''}${added.stderr || ''}`
    if (added.status !== 0) finish(false, `dsh plugin add 退出码 ${added.status}`, { stdout: addedOut.slice(0, 600) })
    const installed = join(profileDir, 'node_modules', '@asywr', 'dsh-zhishu-memory')
    if (!existsSync(join(installed, 'package.json'))) finish(false, 'dsh plugin add 之后 node_modules 里没有这个包', { stdout: addedOut.slice(0, 400) })
  } else {
    const linkDir = join(profileDir, 'node_modules', '@asywr')
    mkdirSync(linkDir, { recursive: true })
    try {
      symlinkSync(packageRoot, join(linkDir, 'dsh-zhishu-memory'), 'junction')
    } catch (error) {
      finish(false, '无法建立 node_modules 链接', { reason: String(error.message) })
    }
  }

  const dumped = runCli(['--profile', 'probe', '--dump-config'], 120000)
  if (dumped.status !== 0) finish(false, `--dump-config 退出码 ${dumped.status}`, { stderr: String(dumped.stderr || '').slice(0, 400) })

  const stdout = String(dumped.stdout || '')
  const missing = expectedRows.filter((id) => !new RegExp(`id: ${id}\\b`).test(stdout))
  const drives = stdout.match(/[A-Za-z]:\\[^\s'"]*/g) || []
  const problems = []
  if (missing.length) problems.push(`缺少行：${missing.join(', ')}`)
  if (drives.length) problems.push(`输出里有盘符路径：${drives.slice(0, 3).join(', ')}`)
  if (!stdout.includes("name: '@asywr/dsh-zhishu-memory'")) problems.push('没有解析到包根入口')
  if (problems.length) finish(false, problems.join('；'), { stdout: stdout.slice(0, 800) })

  // 安装模式再确认一次：装进去的是完整包，版本和工作副本一致。
  if (installMode) {
    const installedManifest = JSON.parse(readFileSync(join(profileDir, 'node_modules', '@asywr', 'dsh-zhishu-memory', 'package.json'), 'utf8'))
    const localVersion = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')).version
    if (installedManifest.version !== localVersion) {
      finish(false, '装进去的版本和当前工作副本不一致', { installed: installedManifest.version, local: localVersion })
    }
  }

  finish(true, `${installMode ? '打包并安装' : '链接'}后，全新 profile 组合出 ${expectedRows.length} 行，无盘符路径（${home}）`)
}

try {
  main()
} catch (error) {
  if (!(error instanceof Reported)) throw error
}
