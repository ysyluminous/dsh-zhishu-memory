# 市场收录提交包（开放插件市场 dshopen.org）

市场是社区项目 [polang233/dsh-plugin-index](https://github.com/polang233/dsh-plugin-index)，索引地址 `https://dshopen.org/index.json`。
收录门槛：**公开源码 + 可安装的 npm 版本 + 明确许可证 + 自己的 Logo + 完善描述**。

## 一、提交前必须成立的三件事

| # | 条件 | 现在 | 谁做 |
|---|---|---|---|
| 1 | 仓库对匿名访问可见 | ❌ `GET api.github.com/repos/ysyluminous/dsh-zhishu-memory` 返回 404（私有） | 你：把仓库转 public，或新建公开仓库 |
| 2 | 仓库里能看到这份源码 | ❌ `git ls-files dsh-plugin` = 0（上游仓库没跟踪这个目录） | 你：`git add dsh-plugin && git commit && git push` |
| 3 | npm 上有可安装版本 | ⏸ 未发布（打包已验证：24 文件 / 69 KB） | 你：确认 scope 后 `npm publish --access public` |

包名与 scope 二选一：

- 保留 `@asywr/dsh-zhishu-memory` → 需要 npm 上有 `@asywr` 组织；
- 改成 `@ysyluminous/dsh-zhishu-memory` 或无 scope 的 `dsh-zhishu-memory` → 我可以改，改完重跑 `npm run verify` 即可。

## 二、发布命令（第 3 条就位后）

```bash
cd dsh-plugin
npm run verify            # 三道闸门：pack-check / 全新 profile 组合 / 全新 profile 安装
npm publish --access public
```

`prepublishOnly` 已经绑到 `verify`，闸门不过发不出去。

## 三、提交表单内容（可直接粘贴）

新建 issue：<https://github.com/polang233/dsh-plugin-index/issues/new?template=plugin-add.yml>

- **插件名 / npm 包名**：`@asywr/dsh-zhishu-memory`（或改后的名字）
- **仓库地址**：`https://github.com/ysyluminous/dsh-zhishu-memory`（目录 `dsh-plugin`）
- **许可证**：MIT
- **Logo**：仓库内 `dsh-plugin/icon.svg`（594 B，无脚本）
- **分类建议**：Memory（记忆）/ Tools（工具）
- **一句话描述**：开工前从本地记忆库取回相关决定与踩过的坑并注入上下文；收工后写回答案与摘要；附胶囊工作台（pg16 / Qdrant / 记忆服务三路探活、一键装环境、会话记录、实体改查、定时备份）。
- **详细描述（英文，市场主语言）**：

  > A four-row bundle over one local memory service. Before turn one it asks the service what you decided or got burned by before and injects the matches; after the turn it writes the answer and a 300-character digest back. A context-budget guard inserts one short self-check note every N turns without ever trimming history. A redline guard sends irreversible commands (`git push`, `docker push`, recursive delete, `DROP TABLE`, outward messages) through an approval card first. The corner capsule opens a workbench: memory service / pg16 / Qdrant probes with latency, a one-click local setup (pgvector Postgres 16, Qdrant, optional Python memory service), recent recalls and write-backs, `mem.doc` browsing and editing, and scheduled `pg_dump` + Qdrant snapshots with bounded retention.
  >
  > Everything machine-specific lives in `~/.dsh/zhishu-memory/settings.json`, defaults are loopback-only, and nothing is read or written until you point it at a service. Needs Docker only for the one-click environment step; the bundle itself needs no dependencies.

- **需要说明的限制**：

  > 实体编辑是行级直改（不写服务端审计、不重建向量）；一键安装只覆盖 Docker 路线；四行的激活效果需要在装好 DSH 的机器上看。

## 四、收录后的徽章（放进 README，把包名换成实际名字）

```md
[![开放插件市场](https://img.shields.io/endpoint?url=https%3A%2F%2Fdshopen.org%2Fbadge%2F%40asywr%2Fdsh-zhishu-memory.json)](https://dshopen.org/zh/plugins/%40asywr/dsh-zhishu-memory/)
```

## 五、本地自己验证（不用等收录）

```bash
node tools/verify.mjs                                   # 3 秒，三道闸门
dsh plugin --profile <干净profile> add @asywr/dsh-zhishu-memory
```

装好之后胶囊会自己弹到「配置」页：先看三张卡是不是绿的，再决定要不要点「执行安装」。
