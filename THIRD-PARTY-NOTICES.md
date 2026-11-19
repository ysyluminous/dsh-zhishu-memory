# THIRD-PARTY-NOTICES

本仓库 `ported/` 下的代码**不是本项目原创**，来源如下。

## 来源

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/EternalNight996/memory-eternal` |
| 版本 | `0.49.11`（`ported/` 里复制的那一版，取自上游 `package.json` 的 `version`）；**已补 sha 基线**：`9e958bdbbdf381be6341a9fffbd3ebe76bd16b8e`（v0.49.12，2026-11-16T18:21:28Z），完整快照落盘 `D:\code\reference\memory-eternal-sha\` |
| 许可 | **MIT License**，Copyright (c) 2026 EternalNight996（原文见 `LICENSE-memory-eternal`） |
| 获取方式 | `ported/` 通过镜像下载 zip 解压（`ghproxy.net`），此前未取到 commit sha，故基线只能按 `package.json` 记为 0.49.11；**2026-11-17 已用 GitHub API 补 sha 并重拉**，`ported/` 内文件未改动（仅补基线，不回写已复制的内容） |
| 抄的方式 | `ported/NN-*/` 下为上游 `lib/` 的**原样复制**（不改写、不删注释）；每个目录附 `NOTES.md` 说明依赖与接入点 |

## 我们抄了什么（ROI 顺序）

见 `ported/` 下 15 个目录；每目录 `NOTES.md` 记录了「模块干什么 / 原文件 / 依赖（静态解析）/ 我们缺哪些依赖 / 将来接到哪个文件」。

## 故意不抄（写清理由，避免以后有人问「为什么不抄」）

| 上游模块/目录 | 不抄的理由 |
|---|---|
| `lib/cloud-*.js`、`lib/cloud-auth.js`、`lib/cloud-server.js` | 云同步/账号体系，与我们「本地 PG + MCP」的形态冲突；且涉及外部服务依赖 |
| `billing` / `quota` / `entitlement` / `license` / `oidc` / `webhook` / `merchant` / `subscription-*` | **商用耦合**（收费、许可、企业登录），我们自用不需要，抄进来只会增加攻击面与合规负担 |
| `news-*.js` | 上游用它做资讯聚合；我们已有 X 探针采集链路，两套并存只会分叉 |
| `web.js`、`client.js`、`src/client/*`（约 1.6MB 前端） | 成本太高：上游是 Node 单页前端，我们是 Python + 现有 web 站，整套 UI 移植 ROI 最低 |

## 我们对上游的处理原则

1. **先抄明白，再改造**：本轮只做原样复制 + NOTES，不做 Python 化改写；
2. 上游是 CommonJS（`require('./x')`）+ Node 运行时；我们是 Python，**真正接入时必须重写或起 Node 边车**，这一点在每个 `NOTES.md` 的「额外需要移植的东西」里都写了「未核实」；
3. 上游 `NOTES.md` 里凡是读不懂的依赖，一律写「未核实」，**没有猜**。

## 总纲：这批代码不是「直接能接」（必须在每个 NOTES 里保持一致口径）

> **上游是 CommonJS + Node 运行时，我们是 Python/FastAPI。这 15 份代码不是「直接能接」，是「可读的实现参考」。**
>
> 真接入有两条路，**待评审，不许写成既成事实**：
> ① **Python 重写**——慢，但同栈、同运维（我们的 gate/search/remember 都在 `app/service.py`）；
> ② **起 Node 边车进程**——快，但多一个运行时要保活（与「开机自启 + 关掉 DSH 不受影响」那套要一起设计）。
>
> 选哪条路，按模块分别定也可以（例如 embeddings 这类适合边车，recall-track 这种几十行适合重写）。