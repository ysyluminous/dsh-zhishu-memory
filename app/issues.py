"""事项（Issue）实体：事后聚类 + 可复现的新旧判定 + 多对多关联 + 退化检查。

2026-11-18 账号主要的模型（老大转述并加了六条约束），这里逐条落地：

1. **事后聚类，不是事前必填**：`auto_assign()` 由系统在写入后自己跑，挂不上就记 orphan，
   等工作台人工归并。任何路径都不许反过来要求「先建事项再干活」，也不许打断对话去问。
2. **新旧判定可复现**（三级，顺序固定）：① 路径/关键词硬信号 → ② 相似度 → ③（默认关闭的）模型。
   输出只有 `reuse` / `create` / `ambiguous`，挂不上的另记 `orphan`。阈值写在 `RULES` 里，能调。
3. **层级别做死**：树只是展示/聚合，真源是 `links.jsonl` 里的「事项 ↔ 实体」多对多。
   同一件事可以挂两个父（项目 + 模块），不靠 parent 字段表达。
4. **只删派生数据**：本模块从不删除原始事实；`archive` 只是状态，默认检索不含归档，
   但 `list_issues(include_archived=True)` 能翻出来。
5. **检索分工**：字段筛选 = 本模块的 `list_issues(...)` 过滤；向量/相似度 = `similarity`；
   图谱 = links（4 类节点 事项/决策/文档/事实，3 种边 belongs/evidence/supersedes）。
6. **退化检查**：`degrade_report()` 五条，输出人话，不是丢数字。

存储全部 append-only JSONL（零 DDL，与 core / supersede / observation 同一套）。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from datetime import datetime, timezone
from typing import Any

from .config import DEFAULT_USER, DATA_DIR
from .quality import jaccard

_ROOT = pathlib.Path(__file__).resolve().parents[2]
# 目录可用环境变量覆盖：验收/回归要在隔离目录里造矛盾样本，不能往线上库灌假结论。
_DIR = pathlib.Path(os.environ.get("MEM_ISSUES_DIR", str(DATA_DIR / "issues")))
_ISSUES = _DIR / "issues.jsonl"     # create / update / archive
_LINKS = _DIR / "links.jsonl"       # issue ↔ entity（belongs / evidence）
_ASSIGN = _DIR / "assign.jsonl"     # 每次判定的留痕（reuse/create/ambiguous/orphan + 信号）

# 阈值写死在这里，能复现、能调（2026-11-18 首版；没有数据支撑前不改）
REUSE_SIM = 0.55        # 相似度 ≥ 这个 → 直接 reuse
AMBIGUOUS_SIM = 0.30    # 落在 [AMBIGUOUS, REUSE) → ambiguous，交人工/模型
VAGUE_CHARS = 12        # 短于这个且没有任何硬信号 → orphan（「这个再改改」这类）
MAX_ISSUES = 500

# 硬信号：路径前缀 / 关键词 → 事项种子。命中一条即 reuse，命中多条即 ambiguous。
# 这是第一级判据，最可靠；新增项目在这里加一行，不用改代码逻辑。
RULES: list[dict[str, Any]] = [
    {
        "issue": "issue-cms-book-page",
        "title": "CMS 图书页改造（详情页/分类/相关资源）",
        "project": "zhishu-web",
        "match": ["/cms/book", "cms/book", "书页", "图书页", "图书详情", "分类徽标", "相关资源"],
    },
    {
        "issue": "issue-memory-context",
        "title": "记忆与上下文编排（Observation / 压缩 / 插件）",
        "project": "memory-mcp",
        "match": ["memory-mcp", "记忆插件", "Observation", "supersede", "ctx_trace", "上下文压缩"],
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: pathlib.Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ---------------- 事项状态 ----------------

def load_issues(include_archived: bool = True) -> dict[str, dict[str, Any]]:
    """事件流折叠成当前事项；archive 只改状态，不删行。"""
    state: dict[str, dict[str, Any]] = {}
    for rec in _read(_ISSUES):
        iid = (rec.get("issue_id") or "").strip()
        if not iid:
            continue
        action = rec.get("action")
        if action == "create":
            state[iid] = {
                "issue_id": iid,
                "title": rec.get("title") or "",
                "project": rec.get("project") or "",
                "status": "active",
                "priority": rec.get("priority") or 2,
                "summary": rec.get("summary") or "",
                "created_at": rec.get("ts"),
                "updated_at": rec.get("ts"),
                "method": rec.get("method") or "seed",
            }
        elif action == "update" and iid in state:
            for k in ("title", "summary", "priority", "project"):
                if rec.get(k) not in (None, ""):
                    state[iid][k] = rec[k]
            state[iid]["updated_at"] = rec.get("ts") or state[iid]["updated_at"]
        elif action == "archive" and iid in state:
            state[iid]["status"] = "archived"
            state[iid]["updated_at"] = rec.get("ts") or state[iid]["updated_at"]
        elif action == "restore" and iid in state:
            state[iid]["status"] = "active"
    if not include_archived:
        state = {k: v for k, v in state.items() if v["status"] == "active"}
    return state


def links() -> list[dict[str, Any]]:
    return _read(_LINKS)


def folded_links() -> list[dict[str, Any]]:
    """按 (issue, entity) 折叠：后写的状态覆盖先写的（被拒 = status=rejected）。

    只追加不改行，所以「挂了但被拒」是链接的一个状态，不是删掉这条关联——
    工作台要能看见「这个东西本来挂在这，但结论被 review 拒了」。
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in links():
        key = (rec.get("issue_id") or "", rec.get("entity_id") or "")
        latest[key] = {**latest.get(key, {}), **rec}
    return list(latest.values())


def mark_rejected(entity_id: str, reason: str = "", new_entity_id: str = "") -> int:
    """review 拒绝一条结论时调用：把它名下的关联标成 rejected（不删关联）。

    文档会被 review 从 pending/ 挪到 rejected/，所以关联里的 entity_id 要跟着换成新路径，
    否则又制造一个「链接指向空路径」——和 Observation 幽灵记录同一类错。
    """
    n = 0
    for rec in folded_links():
        if rec.get("entity_id") != entity_id:
            continue
        if rec.get("status") == "rejected":
            continue
        moved = {**rec, "ts": _now(), "status": "rejected",
                 "reason": (reason or "")[:160], "moved_from": entity_id}
        if new_entity_id:
            # 旧键标 moved，避免「旧路径 + active」这条影子关联继续存在
            _append(_LINKS, {**rec, "ts": _now(), "status": "moved",
                             "moved_to": new_entity_id})
            moved["entity_id"] = new_entity_id
        _append(_LINKS, moved)
        n += 1
    return n


def mark_moved(old_entity: str, new_entity: str) -> int:
    """文档通过 review 从 pending/ 搬到正式区：关联跟着走（不新增关联，只是换 id）。

    注意：折叠的键是 (issue, entity)，换了 entity_id 就变成另一条，所以必须把旧键标成
    moved，否则旧关联会以「active」身份留在列表里，指向一个已经不存在的路径。
    """
    n = 0
    for rec in folded_links():
        if rec.get("entity_id") != old_entity:
            continue
        if rec.get("status") == "moved":
            continue
        prev_status = rec.get("status") or "active"
        _append(_LINKS, {**rec, "ts": _now(), "status": "moved",
                         "moved_to": new_entity})
        _append(_LINKS, {**rec, "ts": _now(), "entity_id": new_entity,
                         "status": prev_status, "moved_from": old_entity})
        n += 1
    return n


def ensure_issue(issue_id: str, title: str, project: str = "", summary: str = "",
                 method: str = "rule") -> dict[str, Any]:
    state = load_issues()
    if issue_id in state:
        return state[issue_id]
    _append(_ISSUES, {"ts": _now(), "action": "create", "issue_id": issue_id,
                      "title": title, "project": project, "summary": summary,
                      "priority": 2, "method": method})
    return load_issues()[issue_id]


def link(issue_id: str, entity_id: str, relation: str = "belongs",
         method: str = "rule", confidence: float = 1.0) -> dict[str, Any]:
    """事项 ↔ 实体 多对多（真源）。同一对只登记一次，重复调用幂等。"""
    for rec in links():
        if rec.get("issue_id") == issue_id and rec.get("entity_id") == entity_id \
                and rec.get("relation") == relation:
            return {"ok": True, "skipped": True, **rec}
    rec = {"ts": _now(), "issue_id": issue_id, "entity_id": entity_id,
           "relation": relation, "method": method, "confidence": round(float(confidence), 4)}
    _append(_LINKS, rec)
    # 把实体的标题并进事项摘要，供二级相似度复用（不建向量库，先用 3-gram Jaccard 顶着）
    issue = load_issues().get(issue_id)
    if issue is not None:
        merged = (issue.get("summary") or "") + " " + entity_id
        _append(_ISSUES, {"ts": _now(), "action": "update", "issue_id": issue_id,
                          "summary": merged[-2000:]})
    return {"ok": True, "skipped": False, **rec}


# ---------------- 三级判定 ----------------

def _rule_hits(text: str) -> list[dict[str, Any]]:
    low = (text or "").lower()
    hits = []
    for rule in RULES:
        for kw in rule.get("match") or []:
            if kw.lower() in low:
                hits.append({"issue_id": rule["issue"], "title": rule["title"],
                             "project": rule.get("project") or "", "signal": kw})
                break
    return hits


def _has_hard_signal(text: str) -> bool:
    """路径 / 文件名 / 网址这类硬信号——有它才允许 create，没有就别瞎猜。"""
    return bool(re.search(r"(/[\w\-./]{3,}|\.(?:md|go|tsx?|py|json|yml)\b|https?://|pid=\d+)", text or ""))


def similarity(text: str) -> list[tuple[str, float]]:
    """二级判据：跟每个 active 事项的摘要比 3-gram Jaccard，降序返回。

    为什么不是真向量：现在没有可用的 embedding 通道（MEM 向量库未启）。
    这一级的口径是「可复现的字面相似」，将来换成向量时只替换本函数，调用方不动。
    """
    base = " ".join((text or "").split())
    scored = []
    for iid, issue in load_issues(include_archived=False).items():
        seed = f"{issue.get('title') or ''} {issue.get('summary') or ''}"
        scored.append((iid, jaccard(base, seed)))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored


def decide_issue(text: str, kind: str = "event") -> dict[str, Any]:
    """判定一条新内容属于哪个事项。输出只有 reuse / create / ambiguous（外加 orphan）。

    决不做的事：不调模型去猜、不打断对话、不要求先建事项。默认路径全确定性。
    """
    raw = (text or "").strip()
    hits = _rule_hits(raw)

    # ① 硬信号
    if len(hits) == 1:
        return {"decision": "reuse", "issue_id": hits[0]["issue_id"],
                "confidence": 0.95, "method": "rule", "signal": hits[0]["signal"],
                "candidate": hits[0]}

    # 多条规则同时命中 → 不猜，交人工
    if len(hits) > 1:
        return {"decision": "ambiguous", "confidence": 0.5, "method": "rule",
                "candidates": [{"issue_id": h["issue_id"], "title": h["title"],
                                "signal": h["signal"], "confidence": 0.5} for h in hits]}

    # ② 相似度
    scored = similarity(raw)
    if scored:
        best_id, best = scored[0]
        if best >= REUSE_SIM:
            return {"decision": "reuse", "issue_id": best_id, "confidence": round(best, 4),
                    "method": "similarity"}
        if best >= AMBIGUOUS_SIM:
            cands = [{"issue_id": i, "title": load_issues()[i]["title"],
                      "confidence": round(s, 4)} for i, s in scored[:3] if s >= AMBIGUOUS_SIM]
            return {"decision": "ambiguous", "confidence": round(best, 4),
                    "method": "similarity", "candidates": cands}

    # ③ 模型级留给将来；现在不启用，落到下面的规则
    # 会话流水（kind=event 且没有任何硬信号命中）不建事项：
    # 每轮对话都新建一个事项，工作台当天就会被几百个「session-xxx」淹掉——那正是
    # 账号主说的「什么都很乱」。它们落 orphan 池，等人工归并（约束①：挂不上记 orphan）。
    if kind == "event":
        return {"decision": "orphan", "confidence": 0.2, "method": "rule",
                "reason": "会话流水没有命中任何项目信号，进孤儿池等人工归并"}

    # 没有任何硬信号、又太短 → orphan（「这个再改改」这种，别猜）
    if not _has_hard_signal(raw) and len(raw) < VAGUE_CHARS:
        return {"decision": "orphan", "confidence": 0.2, "method": "rule",
                "reason": "没有可识别的实体信号，且文本过短"}
    if not _has_hard_signal(raw) and kind not in ("decision", "artifact"):
        return {"decision": "orphan", "confidence": 0.3, "method": "rule",
                "reason": "没有路径/文件名/项目等硬信号"}

    return {"decision": "create", "confidence": 0.4, "method": "rule"}


def auto_assign(entity_id: str, title: str, body: str, kind: str = "event",
                project: str = "") -> dict[str, Any]:
    """写入后自动挂靠（事后聚类）。永不抛异常、永不阻塞写入。"""
    text = f"{title}\n{body}".strip()
    try:
        verdict = decide_issue(text, kind)
    except Exception as exc:                      # 判定失败不能连累 remember
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    action = verdict["decision"]
    issue_id = verdict.get("issue_id")

    if action == "reuse":
        cand = verdict.get("candidate") or {}
        ensure_issue(issue_id, cand.get("title") or issue_id, cand.get("project") or "")
        link(issue_id, entity_id, "belongs", verdict.get("method", "rule"),
             verdict.get("confidence", 0.9))
    elif action == "create":
        issue_id = f"issue-{entity_id.rsplit('-', 1)[-1].replace('.md', '')}"
        ensure_issue(issue_id, title[:80] or entity_id, project, summary=body[:400],
                     method="auto-create")
        link(issue_id, entity_id, "belongs", "auto-create", verdict.get("confidence", 0.4))
    # ambiguous / orphan：不建事项、不挂，等工作台人工归并（这就是 orphan 池）

    _append(_ASSIGN, {"ts": _now(), "entity_id": entity_id, "kind": kind,
                      "decision": action, "issue_id": issue_id,
                      "confidence": verdict.get("confidence"),
                      "method": verdict.get("method"),
                      "signal": verdict.get("signal"),
                      "candidates": verdict.get("candidates"),
                      "reason": verdict.get("reason")})
    return {"ok": True, "decision": action, "issue_id": issue_id, **verdict}


# ---------------- 工作台四格 ----------------

def workbench() -> dict[str, Any]:
    """首页四格：汇集 / 抽取 / 关联 / 维护。每格只给一个数和一句人话。"""
    assigns = _read(_ASSIGN)
    # reuse/create 按全部算（明确命中项目信号的会话流水也算真关联）；
    # orphan/ambiguous 只按可抽取实体算——会话流水在第①步「汇集」里数过了，
    # 拿它算待归并会让孤儿数天天爆炸（实测一天 10 条以上）。
    linkable = [a for a in assigns if a.get("kind") not in ("event",)]
    reuse = sum(1 for a in assigns if a.get("decision") == "reuse")
    created = sum(1 for a in assigns if a.get("decision") == "create")
    amb = [a for a in linkable if a.get("decision") == "ambiguous"]
    orph = [a for a in linkable if a.get("decision") == "orphan"]
    issues = load_issues(include_archived=True)
    active = [i for i in issues.values() if i["status"] == "active"]

    return {
        "collect": {"entities": len({a.get("entity_id") for a in assigns}),
                    "note": "已进入聚类流程的实体数（对话/文档写回都算）"},
        "extract": {"entities": len({a.get("entity_id") for a in assigns}),
                    "kinds": _kind_counts()},
        "link": {"reuse": reuse, "create": created,
                 "orphan": len(orph), "ambiguous": len(amb),
                 "issues_active": len(active), "issues_total": len(issues)},
        "maintain": {"pending": _pending_count(),
                     "archived_issues": len(issues) - len(active),
                     "rejected_links": _rejected_link_count(),
                     "degrade": degrade_report()["counts"]},
    }


def _rejected_link_count() -> int:
    return sum(1 for r in folded_links() if r.get("status") == "rejected")


def _pending_count() -> int:
    try:
        from . import service as _service
        return int(_service.pending_summary(DEFAULT_USER).get("count") or 0)
    except Exception:
        return 0


def _kind_counts() -> dict[str, int]:
    observations = 0
    try:
        from . import observation as _obs
        observations = len(_obs.load(""))
    except Exception:
        pass
    return {"issues": len(load_issues(include_archived=True)),
            "links": len(links()),
            "observations": observations}


# ---------------- 退化检查（六条里的第 5 条） ----------------

_CLAIM_RE = re.compile(r"(阈值|上限|下限|比例|数量|金额|天数|超时|价格|容量|并发|阈值比)\D{0,8}([0-9]+(?:\.[0-9]+)?)")


def _numeric_claims(text: str) -> dict[str, float]:
    """从一句话里抽「关键词 + 数值」，只用来发现互相矛盾的 active 结论。

    例：「压缩阈值仍为 0.2（DSH 口径）」→ {"阈值": 0.2}
    同一事项下两条 active 对同一个关键词给出不同数值 → 判矛盾（可复现，不猜语义）。
    """
    out: dict[str, float] = {}
    for key, val in _CLAIM_RE.findall(text or ""):
        try:
            out.setdefault(key, float(val))
        except ValueError:
            continue
    return out


def degrade_report(stale_days: int = 90, pending_days: int = 7) -> dict[str, Any]:
    """五条退化检查，输出人话 + 条数。原始事实不在这里被删，只报告。"""
    from . import observation as _obs
    from . import supersede as _sup

    state = _obs.load("")
    superseded = {k for k, v in state.items() if v.get("status") == "superseded"}

    # ① 被 supersede 的旧结论还在被引用：links 里还挂着、或别的事项摘要里提到
    still_linked = []
    for rec in links():
        if rec.get("entity_id") in superseded:
            still_linked.append({"entity": rec.get("entity_id"),
                                 "issue": rec.get("issue_id")})

    # ② 同一事实存在多版本：active 记录里相似度 > 0.9 的成对
    actives = [(k, v.get("statement") or "") for k, v in state.items()
               if v.get("status") == "active"]
    duplicates = []
    for i in range(len(actives)):
        for j in range(i + 1, len(actives)):
            s = jaccard(actives[i][1], actives[j][1])
            if s >= 0.90:
                duplicates.append({"a": actives[i][0], "b": actives[j][0],
                                   "similarity": round(s, 3)})

    # ③ 僵尸事项：active 且超过 stale_days 没更新
    now = datetime.now(timezone.utc)
    zombies = []
    for iid, issue in load_issues(include_archived=False).items():
        ts = issue.get("updated_at") or issue.get("created_at") or ""
        try:
            age = (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
        except Exception:
            continue
        if age >= stale_days:
            zombies.append({"issue_id": iid, "title": issue["title"], "idle_days": age})

    # ④ 孤儿实体：只在「抽取层」的实体里算（decision/artifact/…）。
    # 会话流水（event）属于第①步「汇集」，不是待归并的候选——把它算进孤儿池，
    # 每天几百条写回摘要会把孤儿数顶到几十上百，退化报告直接失去意义。
    merged = {(r.get("entity_id")) for r in folded_links()}
    orphans = [a for a in _read(_ASSIGN)
               if a.get("decision") in ("orphan", "ambiguous")
               and a.get("kind") not in ("event",)
               and a.get("entity_id") not in merged]

    # ⑤ 超过 N 天还在 pending 的待审
    old_pending = []
    try:
        from . import service as _service          # 延迟导入避免循环
        info = _service.pending_summary(DEFAULT_USER)
        age_days = (info.get("oldest_age_hours") or 0) / 24
        if int(info.get("count") or 0) > 0 and age_days >= pending_days:
            old_pending.append({"count": info["count"],
                                "oldest_age_days": round(age_days, 1),
                                "oldest_at": info.get("oldest_at")})
    except Exception:
        pass

    # ⑥ 同一事项下互相矛盾的 active 结论（2026-11-18 新增，老大点名）
    # 判据是「同一关键词 + 不同数值」，比如「压缩阈值 0.2」和「压缩阈值 0.3」都 active。
    # 只认关键词+数字这一种形态（可复现、不猜语义）；语义级矛盾留给将来的模型级判定。
    conflicts = []
    by_issue: dict[str, list[str]] = {}
    for rec in folded_links():
        if rec.get("status") == "rejected":
            continue
        by_issue.setdefault(rec.get("issue_id") or "", []).append(rec.get("entity_id") or "")
    for issue_id, entity_ids in by_issue.items():
        claims: list[tuple[str, str, dict[str, float]]] = []
        for eid in entity_ids:
            obs = state.get(eid) or {}
            if obs.get("status") != "active":
                continue
            got = _numeric_claims(obs.get("statement") or "")
            if got:
                claims.append((eid, obs.get("statement") or "", got))
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                shared = set(claims[i][2]) & set(claims[j][2])
                differ = [k for k in shared if claims[i][2][k] != claims[j][2][k]]
                if differ:
                    conflicts.append({
                        "issue_id": issue_id,
                        "a": {"entity": claims[i][0], "statement": claims[i][1][:80]},
                        "b": {"entity": claims[j][0], "statement": claims[j][1][:80]},
                        "keys": differ,
                        "values": {k: [claims[i][2][k], claims[j][2][k]] for k in differ},
                    })

    counts = {
        "superseded_still_linked": len(still_linked),
        "duplicate_facts": len(duplicates),
        "zombie_issues": len(zombies),
        "orphan_entities": len(orphans),
        "stale_pending": len(old_pending),
        "conflicting_active": len(conflicts),
    }
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "details": {
            "superseded_still_linked": still_linked[:5],
            "duplicate_facts": duplicates[:5],
            "zombie_issues": zombies[:5],
            "orphan_entities": [{"entity": o.get("entity_id"), "decision": o.get("decision"),
                                 "reason": o.get("reason")} for o in orphans[:5]],
            "stale_pending": old_pending[:5],
            "conflicting_active": conflicts[:5],
        },
        "tips": _tips(counts),
    }


def _tips(counts: dict[str, int]) -> list[str]:
    out: list[str] = []
    if counts["superseded_still_linked"]:
        out.append("有旧结论已作废却还挂在事项上：检索会推旧口径，先摘掉再补新条。")
    if counts["duplicate_facts"]:
        out.append("同一事实存在多个版本：下次写入会判 evidence，建议人工指定保留哪条。")
    if counts["zombie_issues"]:
        out.append("有事项长期没动：要么归档，要么补一条进度，别让 active 变摆设。")
    if counts["orphan_entities"]:
        out.append("有实体没挂上任何事项：等你在工作台归并，系统不会替你猜。")
    if counts["stale_pending"]:
        out.append("有待审条目压太久：审核不通过就归档，别让 pending 变成垃圾场。")
    if counts["conflicting_active"]:
        out.append("同一事项下有互相矛盾的 active 结论：先确定哪个是对的，把错的那条接替掉，"
                   "否则检索会一会给这个值、一会给那个值。")
    return out or ["六项都没命中：当前没有退化迹象。"]


def archive_issue(issue_id: str, reason: str = "") -> dict[str, Any]:
    """归档一个事项（只改状态，不删数据）。被它挂着的关联一并标 archived。"""
    if issue_id not in load_issues():
        raise ValueError(f"事项不存在: {issue_id}")
    _append(_ISSUES, {"ts": _now(), "action": "archive", "issue_id": issue_id,
                      "reason": (reason or "")[:160]})
    for rec in folded_links():
        if rec.get("issue_id") == issue_id and (rec.get("status") or "active") == "active":
            _append(_LINKS, {**rec, "ts": _now(), "status": "archived",
                             "reason": "issue archived"})
    return {"ok": True, "issue_id": issue_id, "status": "archived"}


def list_issues(status: str = "active", project: str = "", kind: str = "",
                limit: int = 50) -> dict[str, Any]:
    """字段筛选（精确检索那一档）。status=all 时含归档。"""
    issues = load_issues(include_archived=True)
    rows = []
    for iid, issue in issues.items():
        if status != "all" and issue["status"] != status:
            continue
        if project and issue.get("project") != project:
            continue
        rows.append(issue)
    rows.sort(key=lambda r: (r.get("updated_at") or ""), reverse=True)
    return {"count": len(rows), "items": rows[:limit], "status": status}
