/**
 * ctx-guard · 上下文预算守卫
 *
 * 目标：不让会话在无人察觉的情况下越长越贵。三种模式：
 *   off   关闭
 *   smart 每 noteEveryTurns 轮，在回合开始时给 agent 追加一条极短的自省提示（默认）
 *   hard  每一轮都提示（只在调试或止血时用）
 *
 * 提示不改变既有的消息、不裁剪历史、不碰工具结果——它只是把「要不要压缩、
 * 要不要落 mem.doc、要不要开新会话」这个判断交回给 AI 自己。真正的裁剪交给
 * DSH 自带的 compaction-basic 与 tool-result-pruner（参数在 profile 的
 * cordis.patch.yml 里收紧）。
 *
 * 每次注入都写一行 JSONL，方便做开启前后对照。
 *
 * 2026-11-18 修复（这是「开了插件就发不出消息」的真因）：
 * `agent.inject()` 的签名是 `inject(message: UserMessage)`，要的是**消息对象**，
 * 不是一个字符串（dsh-agent/src/runtime-types.ts:271，messages 的形状由
 * dsh-llm/src/message.ts:246 的 createUserMessage 给出：{ id, role, content, source }）。
 * 以前这里传了裸字符串，字符串被原样塞进 next-step 收件箱（会话日志里能直接看到
 * `agent/inbox/spliced ... "inserted":["[预算自检] ..."]`）；下一个 pre-step 里
 * 任何一处 `message.source.kind` 都会在 undefined 上读属性
 * （dsh-repeat-tool-reminder/src/index.ts:231、dsh-agent-loop/src/runtime-context.ts:24），
 * 于是每一轮都在第 2 步开头抛
 * "Cannot read properties of undefined (reading 'kind')"，turn 以 error 收尾。
 *
 * 参数（noteEveryTurns / noteMaxChars / 开关 / 模式）由 lib/settings.js 提供：
 * 悬浮面板改了之后下一轮就生效，不用重启。
 */
import { appendFileSync, mkdirSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

import { readSettings } from '../lib/settings.js';

export const name = '@asywr/dsh-ctx-guard';

const DEFAULTS = {
  enabled: true,
  mode: 'smart',
  noteEveryTurns: 8,
  noteMaxChars: 220,
  // 留空 = `~/.dsh/zhishu-memory/ctx-guard.jsonl`。以前这里写死了一台机器的
  // 绝对路径，别人装上直接写不进去（公开发布的阻塞项之一）。
  logPath: '',
};

const NOTE = (turn, every) =>
  `[预算自检] 本会话已 ${turn} 轮（每 ${every} 轮提示一次）：先判断这件事能否 3 个动作内解决；长内容写 mem.doc，回执只带 doc id；同一内容读超过两轮，就先说一句「建议压缩或开新会话」，不要闷头继续。`;

function writeLog(path, row) {
  try {
    mkdirSync(dirname(path), { recursive: true });
    appendFileSync(path, `${JSON.stringify(row)}\n`, 'utf8');
  } catch { /* 日志失败不影响主流程 */ }
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config || {}) };
  const logPath = cfg.logPath || join(homedir(), '.dsh', 'zhishu-memory', 'ctx-guard.jsonl');

  ctx.effect(() => ctx.on('agent/pre-step', async (event, next) => {
    const decision = await next();
    try {
      // 每轮重新读：面板上的滑杆改完，下一轮就是新值。
      const live = readSettings({
        noteEveryTurns: cfg.noteEveryTurns,
        noteMaxChars: cfg.noteMaxChars,
        ctxGuardEnabled: cfg.enabled,
        ctxGuardMode: cfg.mode,
      });
      const mode = String(live.ctxGuardMode || 'smart');
      if (!live.ctxGuardEnabled || mode === 'off') return decision;
      if (decision?.kind !== 'enter') return decision;
      const { agent, step, turn } = event || {};
      if (step !== 1) return decision;
      const currentTurn = Number(turn) || 0;
      const every = Math.max(1, Number(live.noteEveryTurns) || 8);
      const maxChars = Math.max(60, Number(live.noteMaxChars) || 220);
      if (mode !== 'hard' && currentTurn % every !== 0) return decision;

      let text = NOTE(currentTurn, every);
      if (text.length > maxChars) text = `${text.slice(0, maxChars - 1)}…`;

      if (typeof agent?.inject === 'function') {
        // 必须是合法 UserMessage：字符串会让下一轮 pre-step 在 message.source 上崩。
        agent.inject({
          id: `ctx-guard-${randomUUID()}`,
          role: 'user',
          content: [{ type: 'text', text }],
          source: { kind: 'ctx-guard' },
        });
      }
      writeLog(logPath, {
        t: new Date().toISOString(),
        agent: agent?.id ?? null,
        session: agent?.session?.id ?? null,
        turn: currentTurn,
        step,
        chars: text.length,
        mode,
        every,
      });
    } catch (error) {
      writeLog(logPath, { t: new Date().toISOString(), error: String(error?.message || error) });
    }
    return decision;
  }, { prepend: true }), 'ctx-guard: pre-step budget note');
}
