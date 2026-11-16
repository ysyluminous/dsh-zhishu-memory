# THIRD-PARTY-NOTICES

本仓库 `ported/` 下的代码**不是本项目原创**，来源如下。

## 来源

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/EternalNight996/memory-eternal` |
| 版本 | `0.49.11`（取自上游 `package.json` 的 `version`） |
| 许可 | **MIT License**，Copyright (c) 2026 EternalNight996（原文见 `LICENSE-memory-eternal`） |
| 获取方式 | 通过镜像下载 zip 解压（`ghproxy.net`），**未取到 commit sha**，故版本只能按 `package.json` 记为 0.49.11，具体 commit 未核实 |
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
