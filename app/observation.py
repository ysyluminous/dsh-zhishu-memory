"""Observation 写入模型（零 DDL，append-only JSONL）。

背景（2026-11-18 账号主点名，参照 hindsight 的 Observation 语义）：
旧模型只会「追加」——同一条事实写十次就是十行，改一个结论只能再写一行新的，
旧结论还留在检索面里。写入侧没有去重、没有淘汰、没有取代关系，全靠事后人工收拾。
账号主原话：「迁移过程中的遗忘，非要我主动让你想你才想得起来」。

四条写入语义（照抄，不自己发明）：
  1. 新的事项          → 新建 Observation（action=create）
  2. 重复的信息        → 给已有 Observation 补证据（action=evidence），不新增行
  3. 补充条件 / 更正 / 状态变化 → 更新已有 Observation（action=update）：
                          旧行置 status=superseded，链到新行，历史不删
  4. 没有值得补充的内容 → 保持不变（action=noop）

判定发生在**写入前**（decide()），不是先写进去再合并——这是这次改动的核心。

每条 Observation 的字段：
  obs_id       与 mem.doc.path 同一个值（一条 Observation 对应一份文档，检索面靠它对齐）
  statement    一句话事实（从正文首行取，≤200 字）
  status       active | superseded
  evidence[]   [{source, anchor, note, ts}]  来源 + 锚点 + 片段 + 时间
  valid_from   这条事实从什么时候开始成立（首次写入时间；补证据不改它）
  updated_at   最后一次被补充/更新的时间
  supersedes   本行取代了谁（旧 obs_id），没有则 null
  superseded_by 本行被谁取代，没有则 null

存储：data/observations/observations.jsonl，一行一个事件，只追加不修改，历史可复盘。
"""

from __future__ import annotations

from .config import DATA_DIR

import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Any

from .quality import jaccard

_PATH = pathlib.Path(
    os.environ.get(
        "MEM_OBS_PATH",
        str(DATA_DIR / "observations" / "observations.jsonl"),
    )
)

# 同一条判定的两条线（写入前用，不写进去）：
#   >= SAME_LINE 视为「同一条事实」→ 只补证据，不新建
#   >= RELATED_LINE 且正文带「改/更正/推翻」这类动词 → 视为更正 → supersede
SAME_LINE = 0.90
RELATED_LINE = 0.62

# 更正信号（判定用，不是给人看的）：出现这些词且高相关，就当作「这条旧结论被改了」
_CHANGE_WORDS = (
    "改成", "改为", "调整为", "修订", "更正", "推翻", "取代", "作废", "不再",
    "统一为", "定为", "改掉", "撤掉", "修正为",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _append(rec: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _events() -> list[dict[str, Any]]:
    if not _PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in _PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load(user_id: str = "") -> dict[str, dict[str, Any]]:
    """把事件流折叠成当前状态：{obs_id: Observation}。

    只折叠 create / evidence / update 三类；noop 也留着（用来回答「这条我写过了」）。
    """
    state: dict[str, dict[str, Any]] = {}
    relocations: dict[str, str] = {}
    for rec in _events():
        if user_id and rec.get("user_id") and rec.get("user_id") != user_id:
            continue
        action = rec.get("action")
        oid = (rec.get("obs_id") or "").strip()
        if action == "create":
            state[oid] = {
                "obs_id": oid,
                "statement": rec.get("statement") or "",
                "kind": rec.get("kind") or "event",
                "user_id": rec.get("user_id") or "",
                "status": "active",
                "evidence": list(rec.get("evidence") or []),
                "valid_from": rec.get("valid_from") or rec.get("ts"),
                "updated_at": rec.get("updated_at") or rec.get("ts"),
                "supersedes": rec.get("supersedes"),
                "superseded_by": None,
            }
        elif action == "evidence" and oid in state:
            ev = list(rec.get("evidence") or [])
            state[oid]["evidence"].extend(ev)
            state[oid]["updated_at"] = rec.get("ts") or state[oid]["updated_at"]
        elif action == "relocate":
            # 文档搬家：把记录整体挪到新 id，状态按调用方给的来（active / archived）。
            old_id = rec.get("from_id")
            if old_id in state:
                moved = state.pop(old_id)
                moved["obs_id"] = oid
                moved["status"] = rec.get("status") or "active"
                moved["updated_at"] = rec.get("ts") or moved["updated_at"]
                moved["relocated_from"] = old_id
                state[oid] = moved
                relocations[old_id] = oid
        elif action == "update":
            # update 事件自带新行的全部字段（新行没有单独的 create 事件），
            # 所以这里要先「建行」再「把旧行置 superseded」——漏了建行，链会断在新行上。
            if oid not in state:
                state[oid] = {
                    "obs_id": oid,
                    "statement": rec.get("statement") or "",
                    "kind": rec.get("kind") or "event",
                    "user_id": rec.get("user_id") or "",
                    "status": "active",
                    "evidence": [],
                    "valid_from": rec.get("valid_from") or rec.get("ts"),
                    "updated_at": rec.get("updated_at") or rec.get("ts"),
                    "supersedes": rec.get("supersedes"),
                    "superseded_by": None,
                }
            state[oid]["evidence"].extend(list(rec.get("evidence") or []))
            state[oid]["updated_at"] = rec.get("ts") or state[oid]["updated_at"]
            old_id = rec.get("supersedes")
            if old_id and old_id in state:
                state[old_id]["status"] = "superseded"
                state[old_id]["superseded_by"] = oid
    # 指针跟随搬家：superseded_by 里存的是写入当时的路径，文档后来从 pending 挪到正式区
    # 或 rejected/ 时，指针要跟着解析到最新 id，否则又制造一个「指向空路径」的引用。
    if relocations:
        def _final(doc_id: str) -> str:
            seen: set[str] = set()
            while doc_id in relocations and doc_id not in seen:
                seen.add(doc_id)
                doc_id = relocations[doc_id]
            return doc_id

        for obs in state.values():
            if obs.get("superseded_by"):
                obs["superseded_by"] = _final(obs["superseded_by"])
    return state


def active(user_id: str = "") -> list[dict[str, Any]]:
    return [o for o in load(user_id).values() if o["status"] == "active"]


def get(obs_id: str, user_id: str = "") -> dict[str, Any] | None:
    return load(user_id).get(obs_id)


def history(obs_id: str) -> list[dict[str, Any]]:
    """一条 Observation 的全部事件（复盘用：什么时候建、补过几次证据、被谁取代）。"""
    return [r for r in _events() if (r.get("obs_id") or "") == obs_id
            or (r.get("supersedes") or "") == obs_id]


def _clean_evidence(evidence: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for ev in evidence or []:
        if not isinstance(ev, dict):
            continue
        item = {
            "source": str(ev.get("source") or "")[:200],
            "anchor": str(ev.get("anchor") or "")[:200],
            "note": str(ev.get("note") or "")[:300],
            "ts": str(ev.get("ts") or _now()),
        }
        if item["source"] or item["anchor"] or item["note"]:
            out.append(item)
    return out


def _evidence_key(ev: dict) -> str:
    return "|".join((ev.get("source") or "", ev.get("anchor") or "", ev.get("note") or ""))


def _has_new(state: dict, fresh: list[dict]) -> list[dict]:
    """只留已有证据里没有的那些（同一来源同一锚点算重复）。"""
    seen = {_evidence_key(e) for e in state.get("evidence") or []}
    return [e for e in fresh if _evidence_key(e) not in seen]


def _looks_like_change(statement: str) -> bool:
    text = statement or ""
    return any(w in text for w in _CHANGE_WORDS)


def _best_match(statement: str, state: dict[str, dict[str, Any]]) -> tuple[str, float]:
    best_id, best = "", 0.0
    for oid, obs in state.items():
        if obs.get("status") != "active":
            continue
        s = jaccard(statement, obs.get("statement") or "")
        if s > best:
            best_id, best = oid, s
    return best_id, best


def decide(statement: str, kind: str, user_id: str, obs_id: str,
           evidence: list[dict] | None = None, supersedes: str = "",
           force_new: bool = False, note: str = "") -> dict[str, Any]:
    """写入前判定四条语义，返回这次的处置结果（调用方据此决定要不要写 mem.doc）。

    返回值里的 action 就是四条语义：
      create   新建
      evidence 已有同一条 → 只补证据，不新增行
      update   更正/状态变化 → 新建一行并把旧行置 superseded
      noop     没有值得补充的内容 → 保持不变
    """
    st = _norm(statement)
    if not st:
        raise ValueError("statement 不能为空")
    if not obs_id:
        raise ValueError("obs_id 不能为空")
    ev = _clean_evidence(evidence)
    state = load(user_id)

    # ① 调用方明确说「这条取代旧的」→ update（最可靠，不靠猜）
    if supersedes:
        old = state.get(supersedes)
        if old is None:
            raise ValueError(f"supersedes 指向不存在的 Observation: {supersedes}")
        _append({"ts": _now(), "action": "update", "obs_id": obs_id, "user_id": user_id,
                 "statement": st, "kind": kind, "evidence": ev, "valid_from": _now(),
                 "updated_at": _now(), "supersedes": supersedes, "note": note})
        # 关键：接替必须登记进 supersede.jsonl——检索面的隐藏读的是那个文件。
        # 2026-11-18 就是漏了这一行，才有「status 说 superseded、检索还排第 3」。登记与观测两条线都要走。
        try:
            from . import supersede as _supersede
            _supersede.add(supersedes, obs_id, reason=note or "observation: 显式接替", actor="auto")
        except Exception:
            pass
        return {"ok": True, "action": "update", "obs_id": obs_id, "status": "active",
                "statement": st, "kind": kind, "evidence_count": len(ev),
                "supersedes": supersedes, "superseded_status": "superseded",
                "doc_should_write": True}

    match_id, score = ("", 0.0) if force_new else _best_match(st, state)

    # ② 同一条事实 → 补证据（没有新证据就 noop）
    if match_id and score >= SAME_LINE:
        fresh = _has_new(state[match_id], ev)
        if not fresh:
            _append({"ts": _now(), "action": "noop", "obs_id": match_id, "user_id": user_id,
                     "statement": st, "reason": "same-statement & no-new-evidence"})
            return {"ok": True, "action": "noop", "obs_id": match_id, "status": "active",
                    "statement": state[match_id]["statement"], "kind": state[match_id]["kind"],
                    "evidence_count": len(state[match_id]["evidence"]),
                    "similarity": round(score, 4), "doc_should_write": False,
                    "reason": "同一条事实，没有值得补充的内容"}
        _append({"ts": _now(), "action": "evidence", "obs_id": match_id, "user_id": user_id,
                 "evidence": fresh, "statement": st})
        return {"ok": True, "action": "evidence", "obs_id": match_id, "status": "active",
                "statement": state[match_id]["statement"], "kind": state[match_id]["kind"],
                "evidence_count": len(state[match_id]["evidence"]) + len(fresh),
                "similarity": round(score, 4), "doc_should_write": False,
                "reason": f"同一条事实，补 {len(fresh)} 条证据"}

    # ③ 相关 + 正文带「改成/更正/推翻」→ 视为纠正旧结论 → supersede 链
    if match_id and score >= RELATED_LINE and _looks_like_change(st):
        _append({"ts": _now(), "action": "update", "obs_id": obs_id, "user_id": user_id,
                 "statement": st, "kind": kind, "evidence": ev, "valid_from": _now(),
                 "updated_at": _now(), "supersedes": match_id,
                 "note": note or f"auto: {score:.2f} 相关 + 更正词"})
        try:
            from . import supersede as _supersede
            _supersede.add(match_id, obs_id, reason=f"observation 自动判定更正（相似度 {score:.2f}）",
                           actor="auto")
        except Exception:
            pass   # 检索隐藏失败不影响本次写入
        return {"ok": True, "action": "update", "obs_id": obs_id, "status": "active",
                "statement": st, "kind": kind, "evidence_count": len(ev),
                "supersedes": match_id, "superseded_status": "superseded",
                "similarity": round(score, 4), "doc_should_write": True,
                "reason": "判定为更正/状态变化，旧条转 superseded"}

    # ④ 相关但不是同一条、也没说改 → 补证据（避免新建重复行，也不擅自改结论）
    if match_id and score >= RELATED_LINE and ev:
        fresh = _has_new(state[match_id], ev)
        if fresh:
            _append({"ts": _now(), "action": "evidence", "obs_id": match_id, "user_id": user_id,
                     "evidence": fresh, "statement": st})
            return {"ok": True, "action": "evidence", "obs_id": match_id, "status": "active",
                    "statement": state[match_id]["statement"], "kind": state[match_id]["kind"],
                    "evidence_count": len(state[match_id]["evidence"]) + len(fresh),
                    "similarity": round(score, 4), "doc_should_write": False,
                    "reason": "相关条目，补充证据而非新建"}

    # ⑤ 新的事项 → 新建
    _append({"ts": _now(), "action": "create", "obs_id": obs_id, "user_id": user_id,
             "statement": st, "kind": kind, "evidence": ev, "valid_from": _now(),
             "updated_at": _now(), "supersedes": None})
    return {"ok": True, "action": "create", "obs_id": obs_id, "status": "active",
            "statement": st, "kind": kind, "evidence_count": len(ev),
            "supersedes": None, "doc_should_write": True}


def backfill(obs_id: str, statement: str, kind: str, user_id: str,
             ts: str = "", note: str = "legacy-doc backfill") -> bool:
    """给「Observation 模型之前写入的老文档」补一条 create 事件。

    为什么需要：线上 3065 条历史文档都没有观测记录，直接传 supersedes 指过去会
    「指向不存在的 Observation」。懒回填——只在第一次被接替/补充时才补一行，
    不批量搬历史（搬历史要动 3000 行，收益为零）。
    """
    if not obs_id:
        return False
    if obs_id in load(user_id):
        return False
    stamp = ts or _now()
    _append({"ts": stamp, "action": "create", "obs_id": obs_id, "user_id": user_id,
             "statement": (statement or "")[:200], "kind": kind or "event",
             "evidence": [], "valid_from": stamp, "updated_at": stamp,
             "supersedes": None, "backfilled": True, "note": note})
    return True


def relocate(old_id: str, new_id: str, user_id: str = "", status: str = "active",
             reason: str = "") -> bool:
    """文档搬家时让 Observation 跟着走（review 通过 → 去 pending；拒绝 → 去 rejected/）。

    2026-11-18 幽灵记录的根因：review 只改 mem.doc.path，Observation 里的 obs_id 还是老路径，
    于是出现「指向空路径的 active」。这里登记一条 relocate 事件，状态由调用方给：
      通过 → status=active（继续是活的结论）
      拒绝 → status=archived（归档：不是活结论，也不算被接替）
    """
    if not old_id or not new_id or old_id == new_id:
        return False
    _append({"ts": _now(), "action": "relocate", "obs_id": new_id, "from_id": old_id,
             "user_id": user_id, "status": status or "active", "reason": reason or ""})
    return True


def stats(user_id: str = "") -> dict[str, Any]:
    state = load(user_id)
    rows = list(state.values())
    return {
        "total": len(rows),
        "active": sum(1 for r in rows if r["status"] == "active"),
        "superseded": sum(1 for r in rows if r["status"] == "superseded"),
        "events": len(_events()),
    }
