# Zhishu memory suite for DeepSeek Harness

A DSH bundle with four rows that share one local memory service:

| Row | What it does |
|---|---|
| `zhishu-memory` | Before turn 1, asks the memory service what you decided / got burned by before and injects the matches. After the turn, writes the answer and a short digest back. |
| `ctx-guard` | Inserts one short "budget self-check" note every N turns. It never trims history or touches tool results. |
| `redline-guard` | Sends irreversible commands (`git push`, `docker push`, recursive delete, `DROP TABLE`, outward messages) through a user approval card first. |
| `mem-float` | The bottom-right capsule: memory service / pg16 / Qdrant probes, one-click environment setup, session log, backups, and live parameters. |

Everything machine-specific lives in `~/.dsh/zhishu-memory/settings.json`; nothing is hard-coded to this author's machine. Remove that file to return to shipped defaults.

## Install

From the market index (Discover → add index source `https://dshopen.org/index.json`), or by package name:

```bash
dsh plugin --profile desktop add @asywr/dsh-zhishu-memory
```

First run opens the capsule automatically, because the bundle starts unconfigured on purpose: no data is read or written until you point it at a service.

## First run

1. The capsule (bottom right) opens itself. **Workbench → Setup** shows what is missing.
2. Either point it at an existing memory service (any HTTP endpoint that answers `/gate`, `/remember`, `/ingest`, `/health`), or run the one-click setup: PostgreSQL 16 with pgvector, Qdrant, and a memory service, all as local containers on `127.0.0.1`. The memory service's Git URL is pre-filled, so that step is ready out of the box.
3. Save. Every parameter takes effect on the next turn; no DSH restart.

Once a service directory exists, the controller **starts it automatically 8 seconds after the plugin loads** (it only starts what is already on disk — it never clones or installs by itself). Stop it by hand and it stays stopped for that session; the Setup tab can always start it again.

## What leaves your machine

Nothing, unless you configure a remote endpoint. The bundle speaks only to the URLs in your settings file (`baseUrl`, `serviceUrl`, `pgHost/pgPort`, `qdrantUrl`), all `127.0.0.1` by default.

The memory bridge sends the current user prompt to `baseUrl/gate` and the assistant answer to `baseUrl/ingest` / `baseUrl/remember`. Turn off `writeBack` in the workbench to keep the store read-only, or disable the `zhishu-memory` row entirely.

## Workbench

Two entrances, one set of sections:

- **Sidebar panel** — 智枢记忆 / Zhishu memory in the left navigation (next to Plugins) opens the full workbench page in the main column.
- **Corner capsule** — the quick modal; every section is there, plus an **Open workbench** jump into the page.

Click the capsule:

- **Overview** — memory service, pg16 and Qdrant, with latency and the last search / last write-back.
- **Config** — connection targets plus the tuning sliders, each with a plain-language note.
- **Sessions** — search stored conversations by source (doubao / dsh / workbuddy …), by time (`2024`, `2024-10`, `2024-10-23`) and by keyword across titles and message bodies; click a row to read its messages. Recent recalls and write-backs stay below.
- **MCP service** (page only) — the memory MCP itself: health, MCP URL, keys for other tools (create / revoke), the last 24h of client calls (client, version, key fingerprint, latency, result), the audit file, and a jump to the service console.
- **Backup** — one-shot `pg_dump` plus Qdrant snapshots, optional interval, bounded retention.
- **Setup** — the exact commands it would run, with a dry run before anything executes.

## Settings

Everything the service needs — DSNs, file-store directory, review token, default user, require-key, instance identity — lives in **Workbench → Config** and is written to `~/.dsh/zhishu-memory/settings.json`. Precedence: process environment (what the controller passes on start) > panel file > code default. The workbench writes the file; hand-editing is fine, unknown keys are ignored and out-of-range values are clamped.

A `.env` next to the service source is now **optional** (kept only for running the service standalone, outside DSH): the service itself falls back to the panel file, so removing `.env` does not cut it off from the database.

## Uninstall

Remove the bundle (`dsh plugin remove …` or Plugin Manager). Disabling individual rows leaves the other three working. `~/.dsh/zhishu-memory/` holds the audit log, backups and settings; delete it to start over.

## License

MIT.

---

## 中文速览

四行共用一个本地记忆服务：**记忆桥**（开工前查、收工后写回）、**预算自检**（每 N 轮一句提示，不裁剪历史）、**红线守卫**（不可逆命令先批红）、**右下角胶囊**（工作台：概览 / 配置 / 会话 / 备份 / 一键装环境）。

- 所有机器相关配置都在 `~/.dsh/zhishu-memory/settings.json`，删掉即回出厂值；改完下一轮生效，不用重启 DSH。
- 默认只连本机（`127.0.0.1`），不配置就不读不写；`writeBack` 可关成只读。
- 安装后胶囊会**自动弹出**到 Setup，先把环境准备好再用。
