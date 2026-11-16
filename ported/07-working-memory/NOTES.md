# 07-working-memory

> 来源：`EternalNight996/memory-eternal` 的 `lib/`（zip 解压，未取到 commit sha，版本未核实）。
> 纪律：**原样复制，不改造**；本文件只记录事实，读不懂的写「未核实」。

## 这个模块干什么

- app/service.py gate（工作记忆注入，与已有 core 常驻相近）

## 原样复制的文件

- `working-memory.js`（8574 字节）
- `resident-state.js`（4741 字节）

## 内部依赖（由 require/import 静态解析，未核实运行时行为）

```
依赖图（同组内）：
```
- `working-memory.js` → `./support.js`
- `resident-state.js` → `./watchdog.js`

## 我们仓库里**暂时没有**的依赖（需要额外移植或桩）

- `./support.js`
- `./watchdog.js`

## 额外需要移植的东西（未核实，抄的时候才确认）

- 这些 JS 用的是 `require`（CommonJS）+ 本地 `./xxx` 相对导入；
  我们的服务是 Python，移植时要重写成 Python，或用 Node 边车（未定，等评审）。

## 将来接到我们哪个文件

- app/service.py gate（工作记忆注入，与已有 core 常驻相近）
