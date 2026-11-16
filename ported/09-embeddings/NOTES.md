# 09-embeddings

> 来源：`EternalNight996/memory-eternal` 的 `lib/`（zip 解压，未取到 commit sha，版本未核实）。
> 纪律：**原样复制，不改造**；本文件只记录事实，读不懂的写「未核实」。

## 这个模块干什么

- 新增 app/embedding.py（我们 embedding 三路都不通，先只存代码）

## 原样复制的文件

- `builtin-embed.js`（7568 字节）
- `embeddings.js`（15248 字节）
- `embedding-config.js`（5897 字节）
- `embedding-catalog.js`（4921 字节）
- `ollama-install.js`（10280 字节）
- `ollama-service.js`（7592 字节）

## 内部依赖（由 require/import 静态解析，未核实运行时行为）

```
依赖图（同组内）：
```
- `builtin-embed.js` → `./minhash.js`
- `embeddings.js` → `./builtin-embed.js`, `./db.js`, `./embedding-config.js`
- `embedding-config.js` → `./capture-run.js`
- `embedding-catalog.js` → （无相对依赖）
- `ollama-install.js` → `./ollama-service.js`
- `ollama-service.js` → （无相对依赖）

## 我们仓库里**暂时没有**的依赖（需要额外移植或桩）

- `./builtin-embed.js`
- `./capture-run.js`
- `./db.js`
- `./embedding-config.js`
- `./minhash.js`
- `./ollama-service.js`

## 额外需要移植的东西（未核实，抄的时候才确认）

- 这些 JS 用的是 `require`（CommonJS）+ 本地 `./xxx` 相对导入；
  我们的服务是 Python，移植时要重写成 Python，或用 Node 边车（未定，等评审）。

## 将来接到我们哪个文件

- 新增 app/embedding.py（我们 embedding 三路都不通，先只存代码）
