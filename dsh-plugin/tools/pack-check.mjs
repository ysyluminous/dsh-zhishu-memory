#!/usr/bin/env node
/**
 * 打包/收录前自检：不发网络、不改文件，只回答一个问题——
 * 「这个包现在 `npm publish` 出去、别人装上，会不会红？」
 *
 * 检查五类：
 *   1. manifest 完整性（private / license / engines / publishConfig / exports / files）
 *   2. exports 与 files 指向的文件是否真的存在（少一个就是装上即崩）
 *   3. locale 的 meta.title / meta.description 是否齐全、JSON 是否合法
 *   4. 图标格式与体积（市场要求，Host 上限 256 KiB）
 *   5. 任何随包发布的文本里不得出现本机绝对路径（盘符 / 用户目录）
 *
 * 用法：
 *   node tools/pack-check.mjs
 *   node tools/pack-check.mjs --json
 *
 * 退出码 0 = 可发布；1 = 有阻塞项。
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const asJson = process.argv.includes('--json')

const results = []
const check = (name, ok, detail = '') => results.push({ name, ok: Boolean(ok), detail })
const readJson = (file) => JSON.parse(readFileSync(join(root, file), 'utf8'))

// ── 1. manifest ───────────────────────────────────────────────
const manifest = readJson('package.json')
check('no "private" flag', manifest.private !== true, String(manifest.private))
check('has license', typeof manifest.license === 'string' && manifest.license.length > 0, String(manifest.license))
check('has repository', Boolean(manifest.repository), manifest.repository ? 'set' : '缺失（市场要求公开源码）')
check('engines.node declared', Boolean(manifest.engines?.node), String(manifest.engines?.node))
check('engines.dsh declared', Boolean(manifest.engines?.dsh), String(manifest.engines?.dsh))
check('publishConfig public', manifest.publishConfig?.access === 'public', String(manifest.publishConfig?.access))
check('dsh.bundle.patch declared', Boolean(manifest.dsh?.bundle?.patch), String(manifest.dsh?.bundle?.patch))
check('dsh.client declared', Boolean(manifest.dsh?.client?.platform), String(manifest.dsh?.client?.platform))
check('exports includes ./package.json', Boolean(manifest.exports?.['./package.json']), '')
check('files is a non-empty array', Array.isArray(manifest.files) && manifest.files.length > 0, String(manifest.files?.length))

// ── 2. exports / files 目标存在 ───────────────────────────────
const glue = (target) => String(target).replace(/^\.\//, '')
const wildcardToPath = (pattern) => pattern.replace(/\*/g, 'zh')
for (const [name, target] of Object.entries(manifest.exports || {})) {
  if (name.endsWith('*.json') || name === './package.json') continue
  if (typeof target !== 'string') continue
  check(`export ${name} exists`, existsSync(join(root, glue(target))), glue(target))
}
for (const [name, target] of Object.entries(manifest.exports || {})) {
  if (!name.includes('*')) continue
  check(`export ${name} resolves`, existsSync(join(root, wildcardToPath(glue(target)))), wildcardToPath(glue(target)))
}

// files 里列到的目录/文件必须存在；* 通配按目录检查
for (const entry of manifest.files || []) {
  const clean = entry.replace(/\/\*.*$/, '').replace(/\/$/, '')
  check(`files entry "${entry}" exists`, existsSync(join(root, clean)), clean)
}

// ── 3. locale ─────────────────────────────────────────────────
for (const locale of ['zh', 'en']) {
  const file = `locale/${locale}.json`
  let parsed = null
  try {
    parsed = readJson(file)
  } catch (error) {
    check(`${file} parses`, false, String(error.message))
    continue
  }
  check(`${file} parses`, true, '')
  check(`${file} meta.title`, typeof parsed?.meta?.title === 'string' && parsed.meta.title.length > 0, String(parsed?.meta?.title))
  check(`${file} meta.description`, typeof parsed?.meta?.description === 'string' && parsed.meta.description.length > 20, `${String(parsed?.meta?.description).length} chars`)
  const detail = parsed?.meta?.detail
  if (detail) check(`${file} meta.detail exists`, existsSync(join(root, detail)), String(detail))
}

// ── 4. icon ───────────────────────────────────────────────────
const icon = join(root, 'icon.svg')
if (existsSync(icon)) {
  const size = statSync(icon).size
  check('icon within 256 KiB', size <= 256 * 1024, `${size} bytes`)
  const text = readFileSync(icon, 'utf8')
  check('icon is an SVG document', /<svg[\s>]/i.test(text), '')
  check('icon has no script', !/<script/i.test(text) && !/\son\w+=/i.test(text), '')
} else {
  check('icon.svg exists', false, 'missing')
}

// ── 5. 随包文件里不得有本机绝对路径 ───────────────────────────
const shipped = []
for (const entry of manifest.files || []) {
  const clean = entry.replace(/\/\*.*$/, '').replace(/\/$/, '')
  const full = join(root, clean)
  if (!existsSync(full)) continue
  if (statSync(full).isDirectory()) {
    const walk = (dir) => {
      for (const item of readdirSafe(dir)) {
        const child = join(dir, item.name)
        if (item.isDirectory()) walk(child)
        else shipped.push(child)
      }
    }
    walk(full)
  } else {
    shipped.push(full)
  }
}
function readdirSafe(dir) {
  try {
    return readdirSync(dir, { withFileTypes: true })
  } catch {
    return []
  }
}
const textFiles = shipped.filter((file) => /\.(js|mjs|cjs|json|yml|yaml|md|svg)$/i.test(file))
const offenders = []
for (const file of textFiles) {
  const text = readFileSync(file, 'utf8')
  for (const match of text.matchAll(/[A-Za-z]:\\[^\s'"`)]*/g)) {
    // 文档里示例路径也拦：发布出去的包里不该出现作者机器的目录。
    if (/\\Users\\[^\\]+/i.test(match[0]) || /\\Program Files/i.test(match[0])) {
      offenders.push(`${file.replace(root, '.')}: ${match[0].slice(0, 60)}`)
    }
  }
}
check('no author machine paths in shipped files', offenders.length === 0, offenders.slice(0, 5).join(' | '))

// ── 6. 入口文件语法（用当前 Node 逐个 --check）────────────────
for (const file of ['index.js', 'client.js', 'lib/settings.js', 'ctx-guard/index.js', 'redline/index.js', 'mem-float/index.js', 'tools/audit-report.mjs', 'tools/fresh-profile.mjs']) {
  if (!existsSync(join(root, file))) {
    check(`syntax ${file}`, false, 'missing')
    continue
  }
  try {
    execFileSync(process.execPath, ['--check', join(root, file)], { stdio: 'pipe' })
    check(`syntax ${file}`, true, '')
  } catch (error) {
    check(`syntax ${file}`, false, String(error.stderr || error.message).slice(0, 160))
  }
}

const blocking = results.filter((item) => !item.ok)
if (asJson) {
  console.log(JSON.stringify({ ok: blocking.length === 0, checks: results }, null, 2))
} else {
  for (const item of results) {
    console.log(`${item.ok ? '✓' : '✗'} ${item.name}${item.detail ? `  [${item.detail}]` : ''}`)
  }
  console.log(`\n${results.length - blocking.length}/${results.length} passed${blocking.length ? `, ${blocking.length} blocking` : ''}`)
}
process.exit(blocking.length === 0 ? 0 : 1)
