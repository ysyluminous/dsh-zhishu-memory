/**
 * mem-float · 宿主侧
 *
 * 一个只服务本机的 HTTP 小接口（默认 127.0.0.1:41033），给页面右下角的悬浮胶囊用：
 *   GET  /status          探测记忆服务（默认 127.0.0.1:41012）是否连得上，带 pid 与最近日志尾
 *   POST /start           用既定 launcher（launch-mem-service.ps1）把 python 服务拉起，等到 /health 真的通
 *   POST /stop            结束监听该端口的 python 进程
 *   GET  /workbench       工作台：记忆服务 / pg16 / Qdrant 三路探活 + 最近一次检索与写回
 *   GET  /settings        读出四行共用的参数（滑杆的当前值 + 取值范围）
 *   POST /settings        写入 { patch: {...} }，越界值会被夹回范围
 *   POST /settings/reset  清空覆盖，回到各 loader 行的出厂 config
 *   GET  /collect/status  本机各 AI 工具会话/文档的汇集红绿灯
 *   POST /collect/run     立即汇集一次（默认按设置里的工具列表）
 *
 * 为什么参数放在这里：四行宿主插件跑在同一个进程里，面板是它们唯一能共用的操作面。
 * 面板只跟 41033 说话，41033 写 ~/.dsh/zhishu-memory/settings.json；各行按 mtime
 * 缓存读取，所以改完下一轮就生效，不需要重启 DSH、也不去改 profile 的 YAML。
 *
 * 2026-11-18：探测端口改异步（execFile）。以前是 execSync 跑 netstat，会在宿主
 * 主线程上同步阻塞几十到上百毫秒，而页面每 5 秒轮询一次；同时修掉了一次 /status
 * 里把 listenerPid() 调两遍的冗余（原来三目两边写的是同一个表达式）。
 */
import { createServer } from 'node:http';
import { spawn, execSync, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import {
  appendFileSync, createWriteStream, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { createGzip } from 'node:zlib';
import { pipeline } from 'node:stream/promises';
import { Socket } from 'node:net';

import { KNOBS, SETTINGS_PATH, readSettings, resetSettings, writeSettings } from '../lib/settings.js';
import { collectLastRun, collectNow, collectStatus } from '../lib/collect/index.js';

export const name = '@asywr/dsh-mem-float';

const execFileAsync = promisify(execFile);

const DEFAULTS = {
  host: '127.0.0.1',
  port: 41033,
  serviceUrl: 'http://127.0.0.1:41012',
  servicePort: 41012,
  // 留空即按 homedir() 推导，不再写死某一台机器的绝对路径（公开发布阻塞项）。
  serviceDir: '',
  launcher: '',
  python: '',
  pollMs: 700,
  startTimeoutMs: 30000,
};

const MAX_BODY_BYTES = 64 * 1024;

function tail(file, bytes = 1500) {
  try {
    if (!file || !existsSync(file)) return '';
    const text = readFileSync(file, 'utf8');
    return text.length > bytes ? text.slice(-bytes) : text;
  } catch {
    return '';
  }
}

/**
 * Read one JSON request body with a hard size cap.
 * @param {import('node:http').IncomingMessage} req - Incoming request.
 * @returns {Promise<any>} Parsed body, or an empty object.
 */
function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        resolve({});
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('error', () => resolve({}));
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8').trim();
      if (!text) return resolve({});
      try {
        resolve(JSON.parse(text));
      } catch {
        resolve({});
      }
    });
  });
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config || {}) };
  const stateDir = join(homedir(), '.dsh', 'zhishu-memory');
  const serviceDir = cfg.serviceDir || join(stateDir, 'service');
  const backupsDir = join(stateDir, 'backups');
  const launcher = cfg.launcher || join(serviceDir, 'launch-mem-service.ps1');
  const pythonBin = cfg.python || '';
  const pluginRoot = dirname(dirname(fileURLToPath(import.meta.url)));
  let inFlight = null;

  /** 本次进程里用户是否手动点过「停止」；点过就不再自动启动。 */
  let stoppedByUser = false;

  /**
   * Directories that may hold a memory service, most specific first.
   * Every entry is derived (env / loader config / settings / DSH home / plugin
   * root) — no absolute path of any particular machine.
   * @returns {string[]} Candidate directories.
   */
  const serviceDirCandidates = () => {
    const settings = readSettings();
    const list = [
      process.env.MEM_SERVICE_DIR,
      cfg.serviceDir,
      settings.memoryMcpDir,
      join(stateDir, 'service'),
      join(pluginRoot, 'service'),
    ].filter((item) => typeof item === 'string' && item.length > 0);
    return [...new Set(list)];
  };

  /**
   * Whether a directory looks like the Python memory service.
   * @param {string} dir - Candidate directory.
   * @returns {boolean} True when an entry point is present.
   */
  const hasServiceEntry = (dir) => existsSync(join(dir, 'run.py'))
    || existsSync(join(dir, 'app', 'mcp_server.py'))
    || existsSync(join(dir, 'pyproject.toml'));

  /**
   * Pick the first candidate that actually contains the service.
   * @returns {string | null} Absolute directory, or null.
   */
  const resolveServiceDir = () => serviceDirCandidates().find((dir) => existsSync(dir) && hasServiceEntry(dir)) ?? null;

  let resolvedPython;

  /**
   * Locate a usable Python interpreter and return its real executable path.
   * PATH alone is not enough: the host process PATH often lacks user installs.
   * @returns {Promise<string | null>} Absolute python path, or null.
   */
  const resolvePython = async () => {
    if (resolvedPython !== undefined) return resolvedPython;
    const candidates = [];
    if (pythonBin) candidates.push([pythonBin, []]);
    if (process.env.PYTHON) candidates.push([process.env.PYTHON, []]);
    // py.exe 通常装在 C:\Windows，比 python 更早出现在系统 PATH 上。
    candidates.push(['py', ['-3']]);
    candidates.push(['python', []]);
    candidates.push(['python3', []]);
    for (const [bin, prefix] of candidates) {
      const result = await run(bin, [...prefix, '-c', 'import sys;print(sys.executable)'], 8000);
      if (!result.ok) continue;
      const line = String(result.stdout || '').trim().split(/\r?\n/).pop();
      if (line && existsSync(line)) {
        resolvedPython = line;
        return line;
      }
    }
    resolvedPython = null;
    return null;
  };

  /**
   * 记忆服务需要的环境变量：面板设置优先，缺的才用进程环境。
   *
   * 为什么这样拼：服务原本靠 `.env`，但那份文件用户看不见、改起来要开编辑器、
   * 备份也容易漏。现在统一放 `settings.json`（面板可看可改），启动时由这里
   * 翻译成环境变量传给 python；空值不传，留给服务自己的默认值。
   * @returns {Record<string, string>} Environment overrides for the child process.
   */
  const serviceEnv = () => {
    const values = readSettings();
    const pairs = [
      ['memDsn', 'MEM_DSN'],
      ['zhishuDsn', 'ZHISHU_DSN'],
      ['memDataDir', 'MEM_DATA_DIR'],
      ['memReviewToken', 'MEM_REVIEW_TOKEN'],
      ['memDefaultUser', 'MEM_DEFAULT_USER'],
      ['memDbLabel', 'MEM_DB_LABEL'],
      ['memEnv', 'DSH_ENV'],
    ];
    const env = {};
    for (const [key, name] of pairs) {
      const value = values && values[key];
      if (typeof value === 'string' && value.trim()) env[name] = value.trim();
    }
    if (values && typeof values.memRequireKey === 'boolean') {
      env.MEM_REQUIRE_KEY = values.memRequireKey ? '1' : '0';
    }
    return env;
  };

  /**
   * Start the Python service detached, so closing DSH does not take it down.
   * Windows needs the `cmd /c start` wrapper: a plain detached spawn was
   * observed to fail silently here (process never appears, no error).
   * @param {string} python - Interpreter path.
   * @param {string} dir - Service directory.
   * @param {string} entry - Entry file relative to `dir`.
   */
  const spawnService = (python, dir, entry) => {
    const env = { ...process.env, ...serviceEnv(), PORT: String(cfg.servicePort) };
    if (process.platform === 'win32') {
      const child = spawn('cmd.exe', ['/c', 'start', '""', '/b', python, entry], {
        cwd: dir, env, stdio: 'ignore', windowsHide: true,
      });
      child.on('error', (error) => console.error(`[mem-float] 启动失败：${error?.message}`));
      child.unref();
      return;
    }
    const child = spawn(python, [entry], { cwd: dir, env, stdio: 'ignore', detached: true });
    child.on('error', (error) => console.error(`[mem-float] 启动失败：${error?.message}`));
    child.unref();
  };

  const probe = async () => {
    const startedAt = Date.now();
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 2000);
      const res = await fetch(`${cfg.serviceUrl}/health`, { signal: controller.signal });
      clearTimeout(timer);
      const body = await res.text();
      let health = null;
      try { health = JSON.parse(body); } catch { /* 非 JSON 也算连通 */ }
      return {
        connected: res.ok && health?.ok !== false,
        httpStatus: res.status,
        latencyMs: Date.now() - startedAt,
        health,
        raw: body.slice(0, 300),
      };
    } catch (error) {
      return {
        connected: false,
        latencyMs: Date.now() - startedAt,
        error: error?.name === 'AbortError' ? 'timeout(2000ms)' : String(error?.message || error),
      };
    }
  };

  /**
   * Call the memory service's own REST surface on behalf of the panel.
   *
   * The browser cannot call 41012 directly (cross-origin), so every MCP
   * console/key call goes through this controller with the configured
   * `serviceUrl`. Kept read-mostly: only key create/revoke write anything.
   * @param {string} path - Path after the service origin, e.g. `/keys`.
   * @param {{method?: string, body?: Record<string, unknown>|null, timeoutMs?: number}} [options] - Request options.
   * @returns {Promise<{ok: boolean, status?: number, data?: any, error?: string}>} Result.
   */
  const serviceCall = async (path, options = {}) => {
    const { method = 'GET', body = null, timeoutMs = 8000 } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${cfg.serviceUrl}${path}`, {
        method,
        signal: controller.signal,
        cache: 'no-store',
        headers: body === null ? {} : { 'content-type': 'application/json' },
        body: body === null ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = null; }
      return {
        ok: response.ok && (!data || data.ok !== false),
        status: response.status,
        data,
        error: response.ok ? undefined : `http ${response.status}`,
      };
    } catch (error) {
      return {
        ok: false,
        error: error?.name === 'AbortError' ? `timeout(${timeoutMs}ms)` : String(error?.message || error),
      };
    } finally {
      clearTimeout(timer);
    }
  };

  /**
   * One MCP page's worth of state: service health + the service's own audit.
   * The audit endpoint lives on the console (`/console/api/mcp`) and works even
   * when the database is down, by design.
   * @returns {Promise<Record<string, unknown>>} `{ ok, health, audit, error }`.
   */
  const mcpStatus = async () => {
    const [health, audit] = await Promise.all([
      serviceCall('/health', { timeoutMs: 4000 }),
      serviceCall('/console/api/mcp', { timeoutMs: 8000 }),
    ]);
    return {
      ok: Boolean(health.ok || audit.ok),
      health: health.ok ? health.data : { ok: false, error: health.error },
      audit: audit.ok ? audit.data : { ok: false, error: audit.error },
      error: health.ok || audit.ok ? undefined : health.error || audit.error,
    };
  };


  /**
   * Find the PID listening on one TCP port. Async on purpose: netstat is a child
   * process, and execSync would stall the whole host event loop on every poll.
   * @param {number} port - TCP port.
   * @returns {Promise<number | null>} Listener PID, or null.
   */
  const listenerPid = async (port) => {
    try {
      const { stdout } = await execFileAsync('netstat', ['-ano', '-p', 'tcp'], {
        encoding: 'utf8',
        timeout: 4000,
        windowsHide: true,
      });
      for (const line of stdout.split(/\r?\n/)) {
        if (!line.includes('LISTENING')) continue;
        const cols = line.trim().split(/\s+/);
        if (cols.length < 5) continue;
        if (cols[1].endsWith(`:${port}`)) return Number(cols[4]);
      }
    } catch { /* 取不到就返回 null */ }
    return null;
  };

  const status = async () => {
    const probed = await probe();
    const pid = await listenerPid(cfg.servicePort);
    const dir = resolveServiceDir();
    const python = await resolvePython();
    return {
      ...probed,
      pid,
      busy: Boolean(inFlight),
      busyAction: inFlight,
      serviceUrl: cfg.serviceUrl,
      servicePort: cfg.servicePort,
      // 解析出来的真实位置，出问题时要能一眼看出「插件在找哪个目录、哪个 python」。
      serviceDir: dir,
      serviceDirSearched: serviceDirCandidates(),
      launcher: dir ? join(dir, 'launch-mem-service.ps1') : null,
      hasLauncher: Boolean(dir && existsSync(join(dir, 'launch-mem-service.ps1'))),
      python,
      launcherTail: dir ? tail(join(dir, 'launcher-exit.log'), 600) : '',
      outTail: dir ? tail(join(dir, `uvicorn-${cfg.servicePort}.out.log`), 1200) : '',
      checkedAt: new Date().toISOString(),
    };
  };

  const settingsPayload = () => ({
    path: SETTINGS_PATH,
    knobs: KNOBS,
    values: readSettings(),
  });

  /**
   * Open one TCP connection and report whether it was accepted.
   * This is a liveness probe, not an authentication or query probe: it proves the
   * listener answers, nothing more.
   * @param {string} host - Target host.
   * @param {number} port - Target port.
   * @param {number} timeoutMs - Give-up delay.
   * @returns {Promise<{reachable: boolean, latencyMs: number, error?: string}>} Probe result.
   */
  const tcpProbe = (host, port, timeoutMs = 1500) => new Promise((resolve) => {
    const startedAt = Date.now();
    const socket = new Socket();
    let settled = false;
    const finish = (reachable, error) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve({ reachable, latencyMs: Date.now() - startedAt, ...(error === undefined ? {} : { error }) });
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false, `timeout(${timeoutMs}ms)`));
    socket.once('error', (error) => finish(false, String(error?.code || error?.message || error)));
    socket.connect(port, host);
  });

  /**
   * Probe one Qdrant REST endpoint: version, collection names and point counts.
   * @param {string} url - Origin such as http://127.0.0.1:41024.
   * @returns {Promise<Record<string, unknown>>} Probe result.
   */
  const qdrantProbe = async (url) => {
    const startedAt = Date.now();
    const call = async (path) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 1500);
      try {
        const response = await fetch(`${url}${path}`, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) return { ok: false, status: response.status };
        return { ok: true, body: await response.json() };
      } catch (error) {
        return {
          ok: false,
          error: error?.name === 'AbortError' ? 'timeout(1500ms)' : String(error?.message || error),
        };
      } finally {
        clearTimeout(timer);
      }
    };

    const listed = await call('/collections');
    if (!listed.ok) {
      return {
        url,
        reachable: false,
        latencyMs: Date.now() - startedAt,
        error: listed.error || `http ${listed.status}`,
      };
    }
    const names = (listed.body?.result?.collections ?? []).map((item) => item?.name).filter(Boolean);
    const collections = [];
    for (const name of names.slice(0, 5)) {
      const one = await call(`/collections/${encodeURIComponent(name)}`);
      collections.push({ name, points: one.ok ? one.body?.result?.points_count ?? null : null });
    }
    const root = await call('/');
    return {
      url,
      reachable: true,
      latencyMs: Date.now() - startedAt,
      version: root.ok ? root.body?.version ?? null : null,
      collectionCount: names.length,
      collections,
      totalPoints: collections.reduce((sum, item) => sum + (Number(item.points) || 0), 0),
    };
  };

  /**
   * Last search and last write-back from the bridge's own audit log.
   * @returns {{gate: object|null, writeBack: object|null}|null} Recent entries.
   */
  const recentAudit = () => {
    const file = join(dirname(SETTINGS_PATH), 'audit.jsonl');
    try {
      if (!existsSync(file)) return null;
      const text = readFileSync(file, 'utf8');
      const rows = [];
      for (const line of text.slice(-64 * 1024).split(/\r?\n/)) {
        if (!line.trim()) continue;
        try {
          rows.push(JSON.parse(line));
        } catch { /* torn last line */ }
      }
      const lastWhere = (predicate) => [...rows].reverse().find(predicate);
      const gate = lastWhere((row) => row.event === 'gate' || row.event === 'gate-failed');
      const write = lastWhere((row) => row.event === 'write-back' || row.event === 'write-back-digest');
      return {
        gate: gate
          ? { ts: gate.ts, event: gate.event, class: gate.class ?? null, ms: gate.ms ?? null, count: gate.count ?? null }
          : null,
        writeBack: write
          ? { ts: write.ts, event: write.event, ok: write.ok ?? null, ms: write.ms ?? null }
          : null,
      };
    } catch {
      return null;
    }
  };

  /**
   * One workbench snapshot: the memory service, pg16 and Qdrant, probed together.
   * @returns {Promise<Record<string, unknown>>} Workbench payload.
   */
  const workbench = async () => {
    const values = readSettings();
    const [memory, pg, qdrant] = await Promise.all([
      probe(),
      tcpProbe(values.pgHost, Number(values.pgPort)),
      qdrantProbe(values.qdrantUrl),
    ]);
    return {
      checkedAt: new Date().toISOString(),
      memory: {
        connected: memory.connected,
        latencyMs: memory.latencyMs,
        url: cfg.serviceUrl,
        error: memory.error ?? null,
        raw: memory.raw ?? null,
      },
      pg: {
        host: values.pgHost,
        port: Number(values.pgPort),
        database: values.pgDatabase,
        ...pg,
      },
      qdrant,
      recent: recentAudit(),
    };
  };

  const waitUntilUp = async (timeoutMs) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const result = await probe();
      if (result.connected) return result;
      await new Promise((resolve) => setTimeout(resolve, cfg.pollMs));
    }
    return null;
  };

  const start = async () => {
    if (inFlight) return { ok: false, error: `另一个操作正在进行：${inFlight}` };
    const current = await probe();
    if (current.connected) return { ok: true, already: true, ...current };

    inFlight = 'start';
    try {
      const dir = resolveServiceDir();
      if (!dir) {
        return {
          ok: false,
          error: `没找到记忆服务目录。填「memory-mcp 目录」或「Git 地址」后点「执行安装」，默认装到 ${join(stateDir, 'service')}`,
          searched: serviceDirCandidates(),
        };
      }

      // 有 launcher 就尊重它（用户可能自己维护了退出码和日志）；没有就直接起 python。
      const serviceLauncher = join(dir, 'launch-mem-service.ps1');
      if (existsSync(serviceLauncher)) {
        const psArgs = [
          '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
          '-File', serviceLauncher, '-ServiceDir', dir, '-Port', String(cfg.servicePort),
        ];
        const child = spawn('cmd.exe', ['/c', 'start', '""', '/b', 'powershell.exe', ...psArgs], {
          cwd: dir, stdio: 'ignore', windowsHide: true,
        });
        child.on('error', (error) => console.error(`[mem-float] 启动失败：${error?.message}`));
        child.unref();
      } else {
        const python = await resolvePython();
        if (!python) {
          return {
            ok: false,
            error: '找不到 python：在「配置」里填 Python 可执行文件，或把它加进 PATH',
            serviceDir: dir,
          };
        }
        if (!existsSync(join(dir, 'run.py'))) {
          return {
            ok: false,
            error: `${dir} 里没有 run.py，看起来不是 memory-mcp 目录（也没有 launcher）`,
            serviceDir: dir,
            python,
          };
        }
        spawnService(python, dir, 'run.py');
      }

      const up = await waitUntilUp(cfg.startTimeoutMs);
      if (up) return { ok: true, started: true, serviceDir: dir, ...up };
      return {
        ok: false,
        serviceDir: dir,
        error: `${cfg.startTimeoutMs / 1000} 秒内没等到 /health`,
        log: tail(join(dir, 'launcher-exit.log'), 800) || tail(join(dir, 'uvicorn-41012.err.log'), 800) || tail(join(dir, 'watch-restart-latest.err.log'), 800),
      };
    } finally {
      inFlight = null;
    }
  };

  const stop = async () => {
    if (inFlight) return { ok: false, error: `另一个操作正在进行：${inFlight}` };
    inFlight = 'stop';
    // 用户手动停了就别在下次加载时自动拉起来（自动启动只帮「忘了开」的人）。
    stoppedByUser = true;
    try {
      const pid = await listenerPid(cfg.servicePort);
      if (!pid) return { ok: true, already: true, message: '端口上没有进程' };
      if (pid === process.pid) return { ok: false, error: '拒绝停止：那是我自己' };
      execSync(`taskkill /PID ${pid} /F`, { encoding: 'utf8', timeout: 8000, windowsHide: true });
      await new Promise((resolve) => setTimeout(resolve, 800));
      const after = await probe();
      return { ok: !after.connected, stoppedPid: pid, ...after };
    } catch (error) {
      return { ok: false, error: String(error?.message || error) };
    } finally {
      inFlight = null;
    }
  };

  /**
   * 装载后的一次性自动启动。
   *
   * 别人装完插件、目录里已经有服务（或一键装完过），最常踩的坑是「服务没起来，
   * 面板全红」。这里在后台探一次：没在跑、目录能解析、用户本次没手动停过 → 拉起来。
   * 不装东西（不 clone、不 pip install），只做「启动」这一件安全的事。
   * @returns {Promise<void>} Fulfilment after the attempt.
   */
  const maybeAutoStart = async () => {
    try {
      if (stoppedByUser || inFlight) return;
      const up = await probe();
      if (up.connected) return;
      const dir = resolveServiceDir();
      if (!dir) return;
      const result = await start();
      if (result && result.ok) console.log(`[mem-float] 自动启动记忆服务：${dir}`);
    } catch (error) {
      console.error(`[mem-float] 自动启动失败：${error?.message || error}`);
    }
  };

  const send = (res, code, payload) => {
    const body = JSON.stringify(payload ?? {});
    res.writeHead(code, {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'content-type',
      'Cache-Control': 'no-store',
    });
    res.end(body);
  };

  /**
   * Run one child process and capture its output; never throws.
   * @param {string} file - Executable.
   * @param {string[]} args - Arguments.
   * @param {number} timeoutMs - Kill delay.
   * @param {'utf8'|'buffer'} encoding - Output encoding.
   * @returns {Promise<{ok: boolean, stdout: any, stderr: string, error?: string}>} Outcome.
   */
  const run = (file, args, timeoutMs = 120000, encoding = 'utf8') => new Promise((resolve) => {
    execFile(file, args, { timeout: timeoutMs, windowsHide: true, maxBuffer: 16 * 1024 * 1024, encoding }, (error, stdout, stderr) => {
      resolve({
        ok: !error,
        stdout: stdout ?? (encoding === 'buffer' ? Buffer.alloc(0) : ''),
        stderr: String(stderr ?? ''),
        error: error ? String(error.code || error.message || error) : undefined,
      });
    });
  });

  /**
   * Candidate Docker executables, most specific first.
   *
   * 不能只靠 PATH：DSH 宿主进程的 PATH 常常和用户终端不一致（Docker Desktop
   * 装完只改了用户 PATH，宿主是更早启动的）。实测宿主里 `docker --version`
   * 直接 ENOENT，而同一个用户在 pwsh 里一切正常。
   * @returns {string[]} Candidates to try in order.
   */
  const dockerCandidates = () => {
    const list = []
    if (process.env.DOCKER_BIN) list.push(process.env.DOCKER_BIN)
    if (process.platform === 'win32') {
      const programFiles = process.env.ProgramFiles || 'C:\\Program Files'
      const programData = process.env.ProgramData || 'C:\\ProgramData'
      list.push(
        join(programFiles, 'Docker', 'Docker', 'resources', 'bin', 'docker.exe'),
        join(programData, 'DockerDesktop', 'version-bin', 'docker.exe'),
      )
    }
    list.push('docker')
    return list
  };

  let dockerPath

  /** Resolve a working Docker CLI once; null when none answers. */
  const dockerCommand = async () => {
    if (dockerPath !== undefined) return dockerPath
    for (const candidate of dockerCandidates()) {
      const probeResult = await run(candidate, ['--version'], 5000)
      if (probeResult.ok) {
        dockerPath = candidate
        return candidate
      }
    }
    dockerPath = null
    return null
  }

  /** Whether a Docker CLI answers on this machine. */
  const dockerReady = async () => {
    const bin = await dockerCommand()
    if (bin === null) {
      return { ok: false, version: '', error: 'docker 不可用：PATH 与常见安装路径都没找到' }
    }
    const result = await run(bin, ['--version'], 5000)
    return { ok: result.ok, version: result.ok ? result.stdout.trim() : '', error: result.ok ? undefined : result.error }
  };

  /**
   * Run one Docker CLI invocation through the resolved executable.
   * @param {string[]} args - Arguments after the executable.
   * @param {number} timeoutMs - Kill delay.
   * @param {'utf8'|'buffer'} encoding - Output encoding.
   * @returns {Promise<Record<string, any>>} Same shape as {@link run}.
   */
  const dockerRun = async (args, timeoutMs = 120000, encoding = 'utf8') => {
    const bin = await dockerCommand()
    if (bin === null) {
      return { ok: false, stdout: encoding === 'buffer' ? Buffer.alloc(0) : '', stderr: '', error: 'docker 不可用' }
    }
    return run(bin, args, timeoutMs, encoding)
  };

  /**
   * Recent recalls and write-backs, newest first, from the bridge's audit log.
   * @param {number} limit - Maximum rows.
   * @returns {Array<Record<string, unknown>>} Rows.
   */
  const recentSessions = (limit = 40) => {
    const file = join(stateDir, 'audit.jsonl');
    if (!existsSync(file)) return [];
    const rows = [];
    for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        rows.push(JSON.parse(line));
      } catch { /* torn last line */ }
    }
    const interesting = rows.filter((row) => row.event === 'gate' || row.event === 'gate-failed'
      || row.event === 'write-back' || row.event === 'write-back-digest' || row.event === 'write-back-skipped');
    return interesting.slice(-limit).reverse().map((row) => ({
      ts: row.ts,
      event: row.event,
      class: row.class ?? null,
      ok: row.ok ?? null,
      ms: row.ms ?? null,
      count: row.count ?? null,
      turn: row.turn ?? null,
      query: typeof row.query === 'string' ? row.query.slice(0, 80) : null,
      reason: row.reason ?? null,
      conversationId: row.conversation_id ?? null,
    }));
  };

  /** Backup inventory: newest run first, plus the configured policy. */
  const backupStatus = () => {
    const values = readSettings();
    const runs = [];
    try {
      if (existsSync(backupsDir)) {
        for (const name of readdirSync(backupsDir)) {
          const full = join(backupsDir, name);
          try {
            const stat = statSync(full);
            if (!stat.isDirectory()) continue;
            const files = readdirSync(full).map((file) => ({ name: file, bytes: statSync(join(full, file)).size }));
            runs.push({ name, at: stat.mtime.toISOString(), bytes: files.reduce((sum, file) => sum + file.bytes, 0), files });
          } catch { /* skip unreadable entry */ }
        }
      }
    } catch { /* no directory yet */ }
    runs.sort((a, b) => String(b.name).localeCompare(String(a.name)));
    return {
      dir: backupsDir,
      lastAt: runs[0]?.at ?? null,
      everyHours: Number(values.backupEveryHours) || 0,
      keep: Number(values.backupKeep) || 7,
      pgContainer: values.pgContainer || '',
      runs: runs.slice(0, 20),
      busy: Boolean(backupRunning),
    };
  };

  let backupRunning = false;

  /**
   * One backup run: pg_dump through Docker, plus one Qdrant snapshot per collection.
   * @returns {Promise<Record<string, unknown>>} Run report.
   */
  const runBackup = async () => {
    if (backupRunning) return { ok: false, error: 'already running' };
    backupRunning = true;
    const values = readSettings();
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const dir = join(backupsDir, `run-${stamp}`);
    const report = { ok: false, dir, startedAt: new Date().toISOString(), pg: null, qdrant: null };
    try {
      mkdirSync(dir, { recursive: true });

      if (!values.pgContainer) {
        report.pg = { ok: false, skipped: true, reason: 'pgContainer 未配置（工作台 → 配置 → pg16 容器名）' };
      } else {
        // 走流式：大库的 pg_dump 会超过任何合理的内存缓冲，必须边导边压边落盘。
        const output = join(dir, 'pg.sql.gz');
        const dockerBin = await dockerCommand();
      const child = spawn(dockerBin ?? 'docker', [
          'exec', values.pgContainer, 'pg_dump',
          '-U', values.pgUser, '--no-owner', '--no-acl', values.pgDatabase,
        ], { windowsHide: true });
        let stderr = '';
        child.stderr.on('data', (chunk) => {
          if (stderr.length < 4000) stderr += String(chunk);
        });
        const closed = new Promise((resolve) => child.on('close', (code) => resolve(code)));
        const piped = pipeline(child.stdout, createGzip(), createWriteStream(output))
          .then(() => null)
          .catch((error) => error);
        const code = await closed;
        const pipeError = await piped;
        if (code === 0 && pipeError === null) {
          report.pg = { ok: true, file: output, bytes: statSync(output).size, database: values.pgDatabase };
        } else {
          report.pg = {
            ok: false,
            error: (pipeError ? String(pipeError.message) : '').concat(stderr).slice(0, 300) || `pg_dump exit ${code}`,
          };
        }
      }

      const qdrant = await qdrantProbe(values.qdrantUrl);
      if (!qdrant.reachable) {
        report.qdrant = { ok: false, error: qdrant.error || 'unreachable' };
      } else if ((qdrant.collections || []).length === 0) {
        report.qdrant = { ok: true, snapshots: [], note: '没有集合，跳过' };
      } else {
        const snapshots = [];
        for (const collection of qdrant.collections) {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 30000);
          try {
            const response = await fetch(`${values.qdrantUrl}/collections/${encodeURIComponent(collection.name)}/snapshots`, {
              method: 'POST', signal: controller.signal,
            });
            const body = await response.json().catch(() => null);
            snapshots.push({ collection: collection.name, ok: response.ok, snapshot: body?.result?.name ?? null });
          } catch (error) {
            snapshots.push({ collection: collection.name, ok: false, error: String(error?.message || error) });
          } finally {
            clearTimeout(timer);
          }
        }
        writeFileSync(join(dir, 'qdrant.json'), `${JSON.stringify({ snapshots, collections: qdrant.collections }, null, 2)}\n`, 'utf8');
        report.qdrant = { ok: snapshots.every((item) => item.ok), snapshots };
      }

      // Retention: keep the newest N run directories.
      const keep = Math.max(1, Number(values.backupKeep) || 7);
      const dirs = [];
      if (existsSync(backupsDir)) {
        for (const name of readdirSync(backupsDir)) {
          try {
            if (statSync(join(backupsDir, name)).isDirectory()) dirs.push(name);
          } catch { /* skip */ }
        }
      }
      dirs.sort((a, b) => String(b).localeCompare(String(a)));
      const pruned = [];
      for (const name of dirs.slice(keep)) {
        try {
          rmSync(join(backupsDir, name), { recursive: true, force: true });
          pruned.push(name);
        } catch { /* keep going */ }
      }
      report.pruned = pruned;
      // 「成功」必须真的备份到了东西：什么都没配的空跑不算成功，否则第一次装上来
      // 点一下就显示绿的，实际一份数据都没落盘。
      const attempted = Boolean(values.pgContainer) || (report.qdrant?.snapshots?.length ?? 0) > 0;
      report.ok = attempted && Boolean(report.pg?.ok || report.pg?.skipped) && Boolean(report.qdrant?.ok);
      report.finishedAt = new Date().toISOString();
      return report;
    } catch (error) {
      report.error = String(error?.message || error);
      return report;
    } finally {
      writeFileSync(join(dir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
      backupRunning = false;
    }
  };

  /**
   * The exact setup commands, computed without running anything.
   * Every step is skipped unless its target is configured, so a fresh install
   * on someone else's machine does nothing surprising.
   * @returns {Promise<Record<string, unknown>>} Plan.
   */
  const setupPlan = async () => {
    const values = readSettings();
    const docker = await dockerReady();
    const steps = [];
    const add = (id, label, command, extra = {}) => steps.push({ id, label, command, ...extra });

    add('pg', 'PostgreSQL 16（记忆库，本机 41023）', docker.ok
      ? (values.pgContainer
        ? `docker start ${values.pgContainer}`
        : `docker run -d --name zhishu-mem-pg -e POSTGRES_PASSWORD=postgres -p 41023:5432 pgvector/pgvector:pg16`)
      : 'docker --version', { needs: 'docker', ready: docker.ok });

    add('pg-db', `建库与扩展（${values.pgDatabase}${values.pgContainer ? ` @ ${values.pgContainer}` : ''}）`,
      values.pgContainer
        ? `docker exec ${values.pgContainer} psql -U ${values.pgUser} -tAc "select 1 from pg_database where datname='${values.pgDatabase}'" | grep -q 1 || docker exec ${values.pgContainer} createdb -U ${values.pgUser} ${values.pgDatabase}`
        : '（没有容器名就跳过建库；用你自己的库时请手动确认库已存在）',
      { ready: Boolean(values.pgContainer), needs: 'pgContainer' });

    add('qdrant', 'Qdrant（向量库，本机 41024）', docker.ok
      ? (values.qdrantContainer
        ? `docker start ${values.qdrantContainer}`
        : 'docker run -d --name zhishu-qdrant -p 41024:6333 qdrant/qdrant')
      : 'docker --version', { needs: 'docker', ready: docker.ok });

    add('memory', '记忆服务（Python，本机 41012）',
      values.memoryMcpDir
        ? `python -m pip install -e "${values.memoryMcpDir}"`
        : values.memoryMcpRepo
          ? `git clone ${values.memoryMcpRepo} ${join(stateDir, 'service')} && python -m pip install -e ${join(stateDir, 'service')}`
          : '（在配置里填 memory-mcp 目录或 Git 地址，或直接填一个已有的服务地址）',
      { ready: Boolean(values.memoryMcpDir || values.memoryMcpRepo), needs: 'memoryMcpDir / memoryMcpRepo' });

    const dir = resolveServiceDir();
    const python = await resolvePython();
    const serviceLauncher = dir ? join(dir, 'launch-mem-service.ps1') : null;
    const hasLauncher2 = Boolean(serviceLauncher && existsSync(serviceLauncher));
    add('memory-start', '启动记忆服务并等 /health 真通',
      !dir
        ? '（没找到服务目录：先填目录或 Git 地址再装，或直接把一个已在跑的服务地址填进配置）'
        : hasLauncher2
          ? `powershell -File "${serviceLauncher}" -ServiceDir "${dir}" -Port ${cfg.servicePort}`
          : python
            ? `cd "${dir}" && PORT=${cfg.servicePort} "${python}" run.py`
            : '（找不到 python：在配置里填 Python 可执行文件）',
      { ready: Boolean(dir && (hasLauncher2 || python)), needs: 'serviceDir + python' });

    return {
      docker,
      serviceDir: dir ?? join(stateDir, 'service'),
      stateDir,
      python,
      steps,
      ready: steps.filter((step) => step.ready !== false).length,
      note: '缺 docker 的步骤不会执行；修改配置后重新点「预览」。',
    };
  };

  /**
   * Execute the setup plan. Requires `confirm: true` so a stray request cannot
   * start containers on someone's machine.
   * @param {boolean} confirm - Caller acknowledgement.
   * @returns {Promise<Record<string, unknown>>} Per-step log.
   */
  const runSetup = async (confirm) => {
    if (confirm !== true) return { ok: false, error: '需要 confirm: true 才会执行' };
    const plan = await setupPlan();
    const results = [];
    const values = readSettings();

    // 没有 Docker 不该整场放弃：记忆服务与 python 不依赖容器，
    // 只跳过容器相关的步，剩下的照做（别人可能本来就自己有库）。
    if (!plan.docker.ok) {
      results.push({ step: 'docker', ok: false, skipped: true, reason: plan.docker.error || 'docker 不可用' });
    }

    if (!plan.docker.ok) {
      results.push({ step: 'pg', ok: false, skipped: true, reason: 'docker 不可用' });
      results.push({ step: 'pg-db', ok: false, skipped: true, reason: 'docker 不可用' });
      results.push({ step: 'qdrant', ok: false, skipped: true, reason: 'docker 不可用' });
    } else {
      const pgCommand = values.pgContainer ? ['start', values.pgContainer] : ['run', '-d', '--name', 'zhishu-mem-pg', '-e', 'POSTGRES_PASSWORD=postgres', '-p', '41023:5432', 'pgvector/pgvector:pg16'];
      results.push({ step: 'pg', ...(await dockerRun(pgCommand, 300000)) });

      // 刚 run 出来的容器只有默认的 postgres 库，记忆服务要的是 mem；不建库
      // 就会「容器起来了但服务连不上」，第一步就红（这是别人装上最容易踩的点）。
      if (values.pgContainer) {
        const user = values.pgUser;
        const database = values.pgDatabase;
        const exists = await dockerRun(['exec', values.pgContainer, 'psql', '-U', user, '-tAc', `select 1 from pg_database where datname='${database}'`], 30000);
        if ((exists.stdout || '').trim() === '1') {
          results.push({ step: 'pg-db', ok: true, note: '已存在' });
        } else {
          results.push({ step: 'pg-db', ...(await dockerRun(['exec', values.pgContainer, 'createdb', '-U', user, database], 60000)) });
        }
        // pgvector 是可选的：镜像里没有扩展时只记一条说明，不让整步失败。
        const vector = await dockerRun(['exec', values.pgContainer, 'psql', '-U', user, '-d', database, '-c', 'create extension if not exists vector'], 60000);
        results.push({ step: 'pg-vector', ok: vector.ok, optional: true, note: vector.ok ? 'extension ok' : String(vector.stderr || '').slice(0, 160) });
      } else {
        results.push({ step: 'pg-db', ok: false, skipped: true, reason: 'pgContainer 未配置' });
      }

      const qdrantCommand = values.qdrantContainer ? ['start', values.qdrantContainer] : ['run', '-d', '--name', 'zhishu-qdrant', '-p', '41024:6333', 'qdrant/qdrant'];
      results.push({ step: 'qdrant', ...(await dockerRun(qdrantCommand, 600000)) });
    }

    // 统一的 python：先解析真实可执行文件，实在找不到才回落到命令名。
    const pythonExe = (await resolvePython()) ?? pythonBin ?? (process.platform === 'win32' ? 'python' : 'python3');

    if (values.memoryMcpDir) {
      results.push({ step: 'memory-install', ...(await run(pythonExe, ['-m', 'pip', 'install', '-e', values.memoryMcpDir], 600000)) });
    } else if (values.memoryMcpRepo) {
      // 没给本地目录但有 Git 地址：先克隆到服务目录，再按可编辑包安装。
      const target = join(stateDir, 'service');
      const install = existsSync(join(target, 'pyproject.toml')) || existsSync(join(target, 'setup.py')) || existsSync(join(target, 'run.py'))
        ? { ok: true }
        : await run('git', ['clone', '--depth', '1', values.memoryMcpRepo, target], 300000);
      results.push({ step: 'memory-clone', ...install });
      if (install.ok) {
        results.push({ step: 'memory-install', ...(await run(pythonExe, ['-m', 'pip', 'install', '-e', target], 600000)) });
      }
    } else {
      results.push({ step: 'memory-install', ok: false, skipped: true, reason: 'memoryMcpDir / memoryMcpRepo 都未配置' });
    }

    // 装完还要拉起来，否则「一键」只完成了一半。已经在跑的就不重复起：先探 /health。
    const alreadyUp = await probe();
    if (alreadyUp.connected) {
      results.push({ step: 'memory-start', ok: true, note: '已在运行' });
    } else {
      const started = await start();
      results.push({
        step: 'memory-start',
        ok: Boolean(started.ok),
        serviceDir: started.serviceDir,
        error: started.ok ? undefined : started.error,
      });
    }

    return {
      ok: results.every((item) => item.ok || item.skipped || item.optional),
      // 「装完了」和「没报错」不是一回事：记忆服务那步跳过时环境仍不完整。
      complete: Boolean(values.memoryMcpDir || values.memoryMcpRepo)
        && results.some((item) => item.step === 'memory-start' && item.ok)
        && results.every((item) => item.ok || item.skipped || item.optional),
      skipped: results.filter((item) => item.skipped).map((item) => item.step),
      results,
    };
  };

  /**
   * Run one interval-driven backup when its turn has come.
   * @returns {Promise<void>} Fulfilment after the check.
   */
  const maybeScheduledBackup = async () => {
    const values = readSettings();
    const every = Number(values.backupEveryHours) || 0;
    if (every <= 0 || backupRunning) return;
    const last = backupStatus().lastAt;
    const due = last === null || Date.now() - Date.parse(last) >= every * 3_600_000;
    if (!due) return;
    const report = await runBackup();
    console.log(`[mem-float] scheduled backup ${report.ok ? 'ok' : 'failed'}: ${report.dir}`);
  };

  /**
   * Run one interval-driven collect when its turn has come.
   *
   * 开关默认关闭（别人没配服务地址前不该自己跑）；间隔 0 = 只在手动点。
   * 到点才跑，单次运行加锁在 collect 模块里。任何失败只写审计并打一行日志，
   * 绝不让定时器把宿主带崩。
   * @returns {Promise<void>} Fulfilment after the check.
   */
  /**
   * 后台跑一次汇集。HTTP 请求不再等它跑完：dsh 这类大来源逐条 /ingest 要几分钟，
   * 同步返回必然被客户端超时打断（实测 300s 就断，状态也没落盘）。
   * @param {string[]} tools - 工具 id 列表。
   * @returns {{id: string, running: boolean}} 当前运行句柄。
   */
  let collectRun = null;
  const startCollectRun = (tools) => {
    if (collectRun && collectRun.running) return { id: collectRun.id, running: true };
    const run = {
      id: randomUUID(),
      running: true,
      tools,
      startedAt: new Date().toISOString(),
      finishedAt: null,
      report: null,
      error: null,
    };
    collectRun = run;
    void (async () => {
      try {
        run.report = await collectNow({ home: homedir(), serviceUrl: cfg.serviceUrl, tools });
      } catch (error) {
        run.error = String(error?.message || error);
      } finally {
        run.running = false;
        run.finishedAt = new Date().toISOString();
        console.log(`[mem-float] collect ${run.error ? 'failed' : 'done'}: ${run.report?.messages ?? 0} 条消息 / ${run.report?.docs ?? 0} 份文档`);
      }
    })();
    return { id: run.id, running: true };
  };

  const maybeScheduledCollect = async () => {
    try {
      const values = readSettings();
      if (values.collectEnabled !== true) return;
      const every = Number(values.collectEveryMinutes) || 0;
      if (every <= 0) return;
      if (collectRun && collectRun.running) return;
      const last = collectLastRun({ home: homedir(), serviceUrl: cfg.serviceUrl, tools: values.collectTools });
      const due = last === null || Date.now() - Date.parse(last) >= every * 60_000;
      if (!due) return;
      startCollectRun(values.collectTools);
    } catch (error) {
      // 兜底审计：collect 模块正常失败时自己会写，这里只接住真正意外的异常。
      try {
        appendFileSync(join(stateDir, 'audit.jsonl'), `${JSON.stringify({
          ts: new Date().toISOString(),
          event: 'collect',
          tool: '*',
          sessions: 0,
          messages: 0,
          ok: false,
          ms: 0,
          error: String(error?.message || error),
        })}\n`);
      } catch { /* 审计也写不了就只能打日志 */ }
      console.error(`[mem-float] scheduled collect error: ${error?.message || error}`);
    }
  };

  /**
   * Run one psql statement inside the pg container, with psql-side variable
   * substitution (`:'name'`) so values are never concatenated into SQL text.
   * @param {string} sql - SQL text; use :'var' placeholders.
   * @param {Record<string, string>} vars - Substitution values.
   * @param {number} timeoutMs - Give-up delay.
   * @returns {Promise<{ok: boolean, rows: string[][], stderr: string, error?: string}>} Result.
   */
  const psql = async (sql, vars = {}, timeoutMs = 20000) => {
    const values = readSettings();
    if (!values.pgContainer) {
      return { ok: false, rows: [], stderr: '', error: 'pgContainer 未配置，无法直接查询记忆库' };
    }
    const dockerBin = await dockerCommand();
    if (dockerBin === null) {
      return { ok: false, rows: [], stderr: '', error: 'docker 不可用，无法直接查询记忆库' };
    }
    const args = [
      'exec', '-i', values.pgContainer,
      // -q 必须加：不加的话 psql 会把 "UPDATE 0" 这种命令标签打到 stdout，
      // 被当成一行结果，于是「改了一个不存在的 path」也会报告成功（实测踩过）。
      'psql', '-U', values.pgUser, '-d', values.pgDatabase,
      '-q', '-At', '-F', '\t', '-v', 'ON_ERROR_STOP=1',
    ];
    for (const [key, value] of Object.entries(vars)) args.push('-v', `${key}=${value}`);
    return new Promise((resolve) => {
      const child = spawn(dockerBin, args, { windowsHide: true });
      let stdout = '';
      let stderr = '';
      child.stdout.on('data', (chunk) => { if (stdout.length < 2_000_000) stdout += String(chunk); });
      child.stderr.on('data', (chunk) => { if (stderr.length < 4000) stderr += String(chunk); });
      const timer = setTimeout(() => child.kill(), timeoutMs);
      child.on('error', (error) => {
        clearTimeout(timer);
        resolve({ ok: false, rows: [], stderr, error: String(error.message || error) });
      });
      child.on('close', (code) => {
        clearTimeout(timer);
        const rows = stdout.split(/\r?\n/).filter((line) => line.length > 0).map((line) => line.split('\t'));
        resolve({ ok: code === 0, rows, stderr, error: code === 0 ? undefined : stderr.slice(0, 300) || `psql exit ${code}` });
      });
      child.stdin.end(sql);
    });
  };

  /**
   * List or search memory documents (read-only).
   * @param {string} query - Case-insensitive substring over path and title.
   * @param {number} limit - Maximum rows.
   * @returns {Promise<Record<string, unknown>>} Rows plus the effective query.
   */
  const listEntities = async (query = '', limit = 30) => {
    const size = Math.min(100, Math.max(1, Number(limit) || 30));
    const sql = `select path, kind, coalesce(title, ''), coalesce(length(body), 0), coalesce(to_char(updated_at, 'YYYY-MM-DD HH24:MI'), '')
from mem.doc
where path ilike :'pattern' or coalesce(title, '') ilike :'pattern'
order by updated_at desc nulls last
limit ${size};`;
    const result = await psql(sql, { pattern: `%${query}%` });
    return {
      ok: result.ok,
      query,
      error: result.error,
      rows: result.rows.map((row) => ({
        path: row[0], kind: row[1], title: row[2], bodyChars: Number(row[3]) || 0, updatedAt: row[4],
      })),
    };
  };

  /**
   * Read one document body.
   * @param {string} path - Document path.
   * @returns {Promise<Record<string, unknown>>} Document, capped at 20000 characters.
   */
  const readEntity = async (path) => {
    const result = await psql(`select path, kind, coalesce(title, ''), left(coalesce(body, ''), 20000), coalesce(to_char(updated_at, 'YYYY-MM-DD HH24:MI'), '')
from mem.doc where path = :'path' limit 1;`, { path });
    const row = result.rows[0];
    if (!result.ok) return { ok: false, error: result.error };
    if (!row) return { ok: false, error: 'not found' };
    return { ok: true, entity: { path: row[0], kind: row[1], title: row[2], body: row[3], updatedAt: row[4] } };
  };

  /**
   * Conversation sources with counts, for the source picker.
   *
   * `mem.conv.source` 是导入方自己写的标签（`dsh` / `workbuddy` / `豆包` / `doubao`…），
   * 不是适配器 id，所以这里按库里真实值列，不硬编码列表。
   * @returns {Promise<Record<string, unknown>>} `{ ok, sources: [{ source, count }], error? }`.
   */
  const listConversationSources = async () => {
    const result = await psql('select source, count(*)::int from mem.conv group by 1 order by 2 desc, 1 asc;');
    return {
      ok: result.ok,
      error: result.error,
      sources: result.rows.map((row) => ({ source: row[0] || '', count: Number(row[1]) || 0 })),
    };
  };

  /**
   * Search conversations by source, time window and free text.
   *
   * `q` 同时匹配标题、snippet 和任意一条消息正文——正文那条最贵，所以先按
   * 来源/时间收窄，再 `exists(...)`，最后才取一页；一次多取一条用来判断还有没有下一页。
   * @param {{source?: string, from?: string, to?: string, q?: string, limit?: number, offset?: number}} input - Filters.
   * @returns {Promise<Record<string, unknown>>} `{ ok, rows, more, error? }`.
   */
  const searchConversations = async (input = {}) => {
    const size = Math.min(100, Math.max(1, Number(input.limit) || 30));
    const skip = Math.max(0, Number(input.offset) || 0);
    const vars = {
      source: String(input.source || '').trim(),
      from: String(input.from || '').trim(),
      to: String(input.to || '').trim(),
      needle: String(input.q || '').trim(),
      pattern: String(input.q || '').trim() ? `%${String(input.q).trim()}%` : '',
    };
    // 时间统一按 ISO 返回（UTC），客户端自己转本地时区显示；空串用 nullif 挡掉，避免 ''::timestamptz 报错。
    // 命中片段以关键词为中心取 220 字（只取前 240 字的话，关键词在长消息后半段时会看不到）。
    const sql = `select c.id,
       c.source,
       regexp_replace(coalesce(c.title, ''), E'[\\t\\r\\n]', ' ', 'g'),
       regexp_replace(coalesce(c.snippet, ''), E'[\\t\\r\\n]', ' ', 'g'),
       c.message_count + (select count(*) from mem.live_event l
                           where l.conversation_id = c.id and l.reconciled_at is null),
       to_char((coalesce(c.created_at, c.updated_at)) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
       to_char(c.updated_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
       coalesce(regexp_replace((
         select case when :'needle' = ''
                     then left(m2.text, 240)
                     else substr(m2.text, greatest(1, position(lower(:'needle') in lower(m2.text)) - 40), 220)
                end
           from mem.conv_message m2
          where m2.conversation_id = c.id
            and (:'pattern' = '' or m2.text ilike :'pattern')
          order by m2.seq asc
          limit 1), E'[\\t\\r\\n]+', ' ', 'g'), '')
from mem.conv c
where (:'source' = '' or c.source = :'source')
  and (nullif(:'from', '') is null or coalesce(c.created_at, c.updated_at) >= nullif(:'from', '')::timestamptz)
  and (nullif(:'to', '') is null or coalesce(c.created_at, c.updated_at) < nullif(:'to', '')::timestamptz)
  and (:'pattern' = ''
       or c.title ilike :'pattern'
       or coalesce(c.snippet, '') ilike :'pattern'
       or exists (select 1 from mem.conv_message m3
                   where m3.conversation_id = c.id and m3.text ilike :'pattern')
       or exists (select 1 from mem.live_event l3
                   where l3.conversation_id = c.id and l3.reconciled_at is null and l3.text ilike :'pattern'))
order by coalesce(c.created_at, c.updated_at) desc nulls last
limit ${size + 1} offset ${skip};`;
    const result = await psql(sql, vars, 30000);
    const rows = result.rows.map((row) => ({
      id: row[0],
      source: row[1] || '',
      title: row[2] || '',
      snippet: row[3] || '',
      messages: Number(row[4]) || 0,
      createdAt: row[5] || null,
      updatedAt: row[6] || null,
      hit: row[7] || '',
    }));
    return {
      ok: result.ok,
      error: result.error,
      rows: rows.slice(0, size),
      more: rows.length > size,
    };
  };

  /**
   * Read one conversation's messages.
   *
   * `conv_get` 的口径：`conv_message` 与「还没对账的 live_event」合并按 (seq, created_at) 排。
   * 正文走 base64（`encode()` 默认按 MIME 每 76 字符插换行，必须先去掉），否则
   * 换行/制表符会把 TSV 分列拆坏。
   * @param {{id?: string, limit?: number, offset?: number}} input - Conversation id and page.
   * @returns {Promise<Record<string, unknown>>} `{ ok, conversation, total, offset, messages, error? }`.
   */
  const readConversation = async (input = {}) => {
    const id = String(input.id || '').trim();
    if (!id) return { ok: false, error: 'id 必填' };
    // 上限 100 条：正文按 base64 传，psql 输出有 2 MB 硬顶，再多会被截断成半行。
    const size = Math.min(100, Math.max(1, Number(input.limit) || 40));
    const skip = Math.max(0, Number(input.offset) || 0);
    const metaResult = await psql(`select id, source,
       regexp_replace(coalesce(title, ''), E'[\\t\\r\\n]', ' ', 'g'),
       coalesce(message_count, 0),
       to_char((coalesce(created_at, updated_at)) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
  from mem.conv where id = :'id' limit 1;`, { id });
    if (!metaResult.ok) return { ok: false, error: metaResult.error };
    const meta = metaResult.rows[0];
    if (!meta) return { ok: false, error: '找不到这个会话' };
    const totalResult = await psql(`select (select count(*) from mem.conv_message where conversation_id = :'id')
       + (select count(*) from mem.live_event
           where conversation_id = :'id' and reconciled_at is null);`, { id });
    const sql = `select m.seq, m.role,
       to_char(m.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
       replace(replace(encode(convert_to(left(m.text, 4000), 'UTF8'), 'base64'), chr(10), ''), chr(13), ''),
       length(m.text) > 4000
  from (
    select seq, role, created_at, text, omni_id as ord from mem.conv_message where conversation_id = :'id'
    union all
    select seq, role, created_at, text, id as ord from mem.live_event
     where conversation_id = :'id' and reconciled_at is null
  ) m
  order by m.seq asc, m.created_at asc, m.ord asc
  limit ${size} offset ${skip};`;
    const result = await psql(sql, { id });
    if (!result.ok) return { ok: false, error: result.error };
    return {
      ok: true,
      conversation: {
        id: meta[0],
        source: meta[1] || '',
        title: meta[2] || '',
        messageCount: Number(meta[3]) || 0,
        createdAt: meta[4] || null,
      },
      total: Number(totalResult.rows[0] && totalResult.rows[0][0]) || 0,
      offset: skip,
      messages: result.rows.map((row) => ({
        seq: Number(row[0]) || 0,
        role: row[1] || '',
        at: row[2] || null,
        text: row[3] ? Buffer.from(row[3], 'base64').toString('utf8') : '',
        truncated: row[4] === 't',
      })),
    };
  };

  /**
   * Update one document's title / kind / body.
   * Values go through psql variables, except the body which uses dollar quoting
   * with a random tag, so neither path nor content can break the statement.
   * @param {{path: string, title?: string, kind?: string, body?: string}} payload - Patch.
   * @returns {Promise<Record<string, unknown>>} Result.
   */
  const updateEntity = async (payload) => {
    const path = typeof payload?.path === 'string' ? payload.path.trim() : '';
    if (!path) return { ok: false, error: 'path 必填' };
    const sets = [];
    const vars = { path };
    if (typeof payload.title === 'string') {
      sets.push("title = :'title'");
      vars.title = payload.title;
    }
    if (typeof payload.kind === 'string' && payload.kind.trim()) {
      sets.push("kind = :'kind'");
      vars.kind = payload.kind.trim();
    }
    if (typeof payload.body === 'string') {
      const tag = `z${randomUUID().replace(/-/g, '').slice(0, 12)}`;
      sets.push(`body = $${tag}$${payload.body}$${tag}$`);
    }
    if (sets.length === 0) return { ok: false, error: '没有要改的字段' };
    sets.push('updated_at = now()');
    const sql = `update mem.doc set ${sets.join(', ')} where path = :'path' returning path, to_char(updated_at, 'YYYY-MM-DD HH24:MI');`;
    const result = await psql(sql, vars);
    const row = result.rows[0];
    return {
      ok: result.ok && Boolean(row),
      changed: Boolean(row),
      updatedAt: row ? row[1] : null,
      error: result.ok ? (row ? undefined : '没有这个 path') : result.error,
    };
  };

  const server = createServer(async (req, res) => {
    try {
      if (req.method === 'OPTIONS') {
        res.writeHead(204, {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
          'Access-Control-Allow-Headers': 'content-type',
        });
        return res.end();
      }
      const url = (req.url || '/').split('?')[0];
      if (req.method === 'GET' && url === '/status') return send(res, 200, await status());
      if (req.method === 'GET' && url === '/workbench') return send(res, 200, await workbench());
      if (req.method === 'GET' && url === '/sessions') return send(res, 200, { rows: recentSessions(60) });
      if (req.method === 'GET' && url === '/backup') return send(res, 200, backupStatus());
      if (req.method === 'POST' && url === '/backup') return send(res, 200, await runBackup());
      if (req.method === 'GET' && url === '/setup') return send(res, 200, await setupPlan());
      if (req.method === 'GET' && url === '/entities') {
        const parsed = new URL(req.url || '/', 'http://127.0.0.1');
        return send(res, 200, await listEntities(parsed.searchParams.get('q') || '', parsed.searchParams.get('limit') || 30));
      }
      // MCP 服务页：状态 / Key / 别人调 MCP 的审计，全部经 41033 转发到服务本体。
      if (req.method === 'GET' && url === '/mcp/status') {
        return send(res, 200, await mcpStatus());
      }
      if (req.method === 'GET' && url === '/mcp/keys') {
        const result = await serviceCall('/keys');
        return send(res, 200, result.ok ? { ok: true, ...result.data } : { ok: false, error: result.error });
      }
      if (req.method === 'POST' && url === '/mcp/keys') {
        const body = await readBody(req);
        const result = await serviceCall('/keys', { method: 'POST', body: { name: String((body && body.name) || '') } });
        return send(res, 200, result.ok ? { ok: true, ...result.data } : { ok: false, error: result.error, status: result.status });
      }
      if (req.method === 'POST' && url === '/mcp/keys/revoke') {
        const body = await readBody(req);
        const result = await serviceCall('/keys/revoke', { method: 'POST', body: { id: String((body && body.id) || '') } });
        return send(res, 200, result.ok ? { ok: true, ...result.data } : { ok: false, error: result.error, status: result.status });
      }
      // 会话检索：来源 + 时间窗 + 关键词，全部只读。
      if (req.method === 'GET' && url === '/conversations/sources') {
        return send(res, 200, await listConversationSources());
      }
      if (req.method === 'GET' && url === '/conversations') {
        const parsed = new URL(req.url || '/', 'http://127.0.0.1');
        return send(res, 200, await searchConversations({
          source: parsed.searchParams.get('source') || '',
          from: parsed.searchParams.get('from') || '',
          to: parsed.searchParams.get('to') || '',
          q: parsed.searchParams.get('q') || '',
          limit: parsed.searchParams.get('limit') || 30,
          offset: parsed.searchParams.get('offset') || 0,
        }));
      }
      if (req.method === 'GET' && url === '/conversations/messages') {
        const parsed = new URL(req.url || '/', 'http://127.0.0.1');
        return send(res, 200, await readConversation({
          id: parsed.searchParams.get('id') || '',
          limit: parsed.searchParams.get('limit') || 40,
          offset: parsed.searchParams.get('offset') || 0,
        }));
      }
      if (req.method === 'POST' && url === '/entities/read') {
        const body = await readBody(req);
        return send(res, 200, await readEntity(body && body.path));
      }
      if (req.method === 'POST' && url === '/entities/update') {
        const body = await readBody(req);
        return send(res, 200, await updateEntity(body));
      }
      if (req.method === 'POST' && url === '/setup') {
        const body = await readBody(req);
        return send(res, 200, await runSetup(body && body.confirm === true));
      }
      if (req.method === 'POST' && url === '/start') {
        stoppedByUser = false;
        return send(res, 200, await start());
      }
      if (req.method === 'POST' && url === '/stop') return send(res, 200, await stop());
      if (req.method === 'GET' && url === '/settings') return send(res, 200, settingsPayload());
      if (req.method === 'POST' && url === '/settings') {
        const body = await readBody(req);
        const result = writeSettings(body && body.patch ? body.patch : body);
        return send(res, 200, { ok: true, ...result, ...settingsPayload() });
      }
      if (req.method === 'POST' && url === '/settings/reset') {
        const result = resetSettings();
        return send(res, 200, { ok: true, ...result, ...settingsPayload() });
      }
      // 汇集：两条都必须在服务不可达时优雅降级成 { ok:false, error }，不能抛。
      if (req.method === 'GET' && url === '/collect/status') {
        try {
          return send(res, 200, await collectStatus({ home: homedir(), serviceUrl: cfg.serviceUrl }));
        } catch (error) {
          return send(res, 200, { ok: false, error: String(error?.message || error), tools: [] });
        }
      }
      if (req.method === 'POST' && url === '/collect/run') {
        try {
          const body = await readBody(req);
          const values = readSettings();
          const tools = body && Array.isArray(body.tools) ? body.tools : values.collectTools;
          // 手动点不受 collectEnabled 限制：开关只决定定时器跑不跑。
          const started = startCollectRun(tools);
          return send(res, 200, { ok: true, running: true, runId: started.id, already: started.running && started.id !== undefined && collectRun?.id !== started.id ? false : undefined });
        } catch (error) {
          return send(res, 200, { ok: false, error: String(error?.message || error), results: [] });
        }
      }
      if (req.method === 'GET' && url === '/collect/log') {
        const parsed = new URL(req.url || '/', 'http://127.0.0.1');
        const limit = Math.min(200, Math.max(1, Number(parsed.searchParams.get('limit')) || 40));
        try {
          const rows = [];
          const file = join(stateDir, 'audit.jsonl');
          if (existsSync(file)) {
            for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
              if (!line.includes('"event":"collect"')) continue;
              try { rows.push(JSON.parse(line)); } catch { /* 半行 */ }
            }
          }
          return send(res, 200, { rows: rows.slice(-limit).reverse() });
        } catch (error) {
          return send(res, 200, { rows: [], error: String(error?.message || error) });
        }
      }
      if (req.method === 'GET' && url === '/collect/progress') {        const run = collectRun;
        if (!run) return send(res, 200, { running: false, report: null });
        return send(res, 200, {
          running: Boolean(run.running),
          runId: run.id,
          tools: run.tools,
          startedAt: run.startedAt,
          finishedAt: run.finishedAt,
          error: run.error,
          report: run.report,
        });
      }
      return send(res, 404, { ok: false, error: 'not found' });
    } catch (error) {
      return send(res, 500, { ok: false, error: String(error?.message || error) });
    }
  });

  server.on('error', (error) => {
    console.error(`[mem-float] ${cfg.host}:${cfg.port} 监听失败：${error?.message}`);
  });
  server.listen(cfg.port, cfg.host, () => {
    console.log(`[mem-float] 控制接口就绪 http://${cfg.host}:${cfg.port}`);
  });

  ctx.effect(() => () => {
    try { server.close(); } catch { /* 已关闭 */ }
  }, 'mem-float: control server');

  // 装载后 8 秒自动拉一次服务：目录在、服务没跑、用户没手动停 → 起。
  // 只做启动，不 clone、不 pip install；失败只打日志。
  setTimeout(() => { void maybeAutoStart(); }, 8 * 1000).unref?.();

  // 定时备份：每 10 分钟看一次到点没有。备份间隔为 0 时什么都不做。
  const backupTimer = setInterval(() => { void maybeScheduledBackup(); }, 10 * 60 * 1000);
  ctx.effect(() => () => clearInterval(backupTimer), 'mem-float: backup timer');
  setTimeout(() => { void maybeScheduledBackup(); }, 30 * 1000).unref?.();

  // 内置汇集：启动 60 秒后首查一次，之后每 10 分钟看一次是否到点。
  // 开关关着、间隔为 0 时什么都不做；单次运行加锁在 collect 模块里。
  const collectTimer = setInterval(() => { void maybeScheduledCollect(); }, 10 * 60 * 1000);
  ctx.effect(() => () => clearInterval(collectTimer), 'mem-float: collect timer');
  setTimeout(() => { void maybeScheduledCollect(); }, 60 * 1000).unref?.();
}
