# 03-junk

> 来源：`EternalNight996/memory-eternal` 的 `lib/`（zip 解压，未取到 commit sha，版本未核实）。
> 纪律：**原样复制，不改造**；本文件只记录事实，读不懂的写「未核实」。

## 这个模块干什么

- 检索时降权/过滤垃圾卡（app/service.py search_personal）

## 原样复制的文件

- `junk.js`（10576 字节）
- `auto-junk.js`（10153 字节）

## 内部依赖（由 require/import 静态解析，未核实运行时行为）

```
依赖图（同组内）：
```
- `junk.js` → `./db.js`
- `auto-junk.js` → `./db.js`, `./junk.js`, `./vault.js`

## 我们仓库里**暂时没有**的依赖（需要额外移植或桩）

- `./db.js`
- `./junk.js`
- `./vault.js`

## 额外需要移植的东西（未核实，抄的时候才确认）

- 这些 JS 用的是 `require`（CommonJS）+ 本地 `./xxx` 相对导入；
  我们的服务是 Python，移植时要重写成 Python，或用 Node 边车（未定，等评审）。

## 将来接到我们哪个文件

- 检索时降权/过滤垃圾卡（app/service.py search_personal）
