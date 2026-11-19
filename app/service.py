from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

from . import history as _history
from . import quality as _quality
from . import core as _core
from . import supersede as _supersede
# 2026-11-18：Observation 写入模型 + 项目开工必读清单（账号主点名，参照 hindsight Observation）
from . import observation as _observation
from . import projects as _projects
from . import rules as _rules
# 事项实体（2026-11-18）：事后聚类，挂在 remember 之后自己跑
from . import issues as _issues
# 统一可见性出口：谁被 supersede 只在这里算一次，查文档的地方统一带 hidden_param()
from . import visibility as _visibility
from .config import DEFAULT_USER, MEM_DSN, REQUIRE_KEY, ZHISHU_DSN


def _conn(dsn: str):
    # 连接超时 8s→3s（2026-11-16 P0 复盘）：库不可达时 8 秒会把秒级预算吃穿，
    # 三层写回叠加就是 20 秒。只限「建连」，查询超时另设，不受影响。
    return psycopg.connect(dsn, connect_timeout=3)


def resolve_user(api_key: str | None) -> dict[str, str] | None:
    """Return {id,name} for a key, or the default local user when keys are off."""
    key = (api_key or "").strip()
    if key:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with _conn(MEM_DSN) as c:
            with c.cursor() as cur:
                cur.execute(
                    """select u.id, u.name from mem.api_key k
                       join mem.app_user u on u.id = k.user_id
                       where k.key_hash = %s""",
                    (digest,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cur.execute("update mem.api_key set last_used_at = now() where key_hash = %s", (digest,))
            c.commit()
        return {"id": row[0], "name": row[1] or row[0]}
    if REQUIRE_KEY:
        return None
    return {"id": DEFAULT_USER, "name": DEFAULT_USER}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(text: str, size: int = 320) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:size]


# 元笔记降权（2026-11-19 WorkBuddy 首次接入复盘）：
# 查「伤寒论三家合参笔记.pdf」时，命中最多的是「关于 omnihist/记忆库本身」的技术笔记，
# 正文里恰好引用了这个词；真正的业务会话反被压下去。
# 只对 mem.doc 标题做降权，不动 mem.conv；MEM_META_PENALTY=1 可一键关掉。
_META_TITLE_RE = re.compile(
    r"(omnihist|记忆库|记忆服务|检索底座|检索层|mcp|workbuddy|会话采集|上下文编排|工作台)",
    re.I,
)
_META_PENALTY = 0.55
try:
    _META_PENALTY = max(0.05, min(1.0, float(os.getenv("MEM_META_PENALTY", "0.55") or 0.55)))
except (TypeError, ValueError):
    pass


def _is_meta_note(title: str, path: str = "") -> bool:
    return bool(_META_TITLE_RE.search(f"{title or ''} {path or ''}"))


def _match_info(terms: list[str], title: str, body: str, snippet: str) -> dict[str, Any]:
    """回答「为什么这条被召回」：命中了哪些词、落在标题还是正文。"""
    low_t = (title or "").lower()
    low_b = (body or "").lower()
    low_s = (snippet or "").lower()
    title_hits = [t for t in terms if t and t.lower() in low_t]
    body_hits = [t for t in terms if t and (t.lower() in low_b or t.lower() in low_s)]
    return {
        "matched_terms": list(dict.fromkeys(title_hits + body_hits))[:12],
        "title_hits": list(dict.fromkeys(title_hits))[:8],
        "body_hits": list(dict.fromkeys(body_hits))[:8],
    }


def _snippet_around(text: str, keyword: str, width: int = 160) -> str:
    """关键词附近的片段，而不是从头截 320 字（会话消息常把关键词埋在中间）。"""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return ""
    low = clean.lower()
    key = (keyword or "").split()[0].lower() if (keyword or "").split() else ""
    pos = low.find(key) if key else -1
    if pos < 0:
        return clean[:width * 2]
    start = max(0, pos - width)
    end = min(len(clean), pos + len(key) + width)
    return ("…" if start else "") + clean[start:end] + ("…" if end < len(clean) else "")


def _anchor(kind: str, ident: str, seq: int | None = None, at: str | None = None) -> str:
    """可回溯锚点：doc 用路径；会话用 session + seq + 时间。"""
    base = f"{kind}:{ident}"
    if seq is not None:
        base += f"#seq={seq}"
    if at:
        base += f"@{at}"
    return base


# 来源权重（2026-11-16 内容副总/幕后大佬定，只上这一条）：
# mem.doc 得分 ×1.2 —— 不改数据、不删内容、不引入时间假设，只在两张表的分数合并处生效。
# 明确不上：兜底制、过滤 doubao:*、时间衰减（各有副作用，仅留条目备查）。
_DOC_SCORE_WEIGHT = 1.2
_TERM_SPLIT = re.compile(r"[\s,，。;；、/|]+")
_MAX_TERMS = 8
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_GRAM_LIMIT = 12
# 虚词 2-gram 先滤掉：它们几乎每篇都有，留着只会把候选池冲淡。
_GRAM_STOP = frozenset(
    """这个 那个 我们 你们 他们 什么 怎么 为什 是不 不是 可以 需要 应该 一个 一下
    现在 然后 因为 所以 但是 如果 或者 以及 这样 那样 就是 还是 已经 没有 多少
    时候 如何 哪些 是否 有没 有什""".split()
)

# 引用惩罚：正文含「整条 query 原文」的文档打 0.3 折。
# 为什么：记录「某条查询查不到」时把 query 原句抄进正文，trigram 相似度天然最高，
# 真答案反被压下去（检索自污染）。引用不等于回答，故降权。只罚 mem.doc，不罚 mem.conv。
_QUOTE_PENALTY = 0.3


def _legacy_terms(q: str) -> list[str]:
    return [t for t in _TERM_SPLIT.split(q.strip()) if t.strip()][:_MAX_TERMS]


def _gram_terms(q: str) -> list[str]:
    """中文按 2-gram 切（2026-11-16 P1）：旧行为把整句当一个 term 做 ilike，必 0 条。"""
    terms: list[str] = []
    seen: set[str] = set()
    for run in _CJK_RUN.findall(q or ""):
        grams = [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
        for g in grams:
            if g in _GRAM_STOP or g in seen:
                continue
            seen.add(g)
            terms.append(g)
    for word in _TERM_SPLIT.split(q or ""):
        w = word.strip()
        if not w or _CJK_RUN.fullmatch(w):
            continue
        if w.lower() in seen:
            continue
        seen.add(w.lower())
        terms.append(w)
    return terms[:_GRAM_LIMIT]


def _terms(q: str) -> list[str]:
    """检索词。MEM_RETRIEVAL=legacy 一键回退到整句/分词旧行为。"""
    if os.environ.get("MEM_RETRIEVAL", "gram").strip().lower() == "legacy":
        return _legacy_terms(q)
    return _gram_terms(q) or _legacy_terms(q)


_DF_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "term_df.json")
_DF_TTL = 600.0  # 词频变化慢；过期后按需重算，不建表、不进库
_DF_LOADED = False
_DF: dict[str, dict[str, float]] = {}


def _df_cache_load() -> None:
    global _DF_LOADED
    if _DF_LOADED:
        return
    _DF_LOADED = True
    try:
        with open(_DF_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _DF.update({str(k): v for k, v in data.items() if isinstance(v, dict)})
    except Exception:
        pass


def _df_cache_save() -> None:
    try:
        os.makedirs(os.path.dirname(_DF_CACHE_PATH), exist_ok=True)
        tmp = _DF_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_DF, f, ensure_ascii=False)
        os.replace(tmp, _DF_CACHE_PATH)
    except Exception:
        pass  # 缓存写不进去不影响检索，只是下次仍慢


def _safe_df(value: Any) -> float:
    """把文档频率折成一个安全值。

    2026-11-16 事故：英文/编号类查询（如 zz-probe-20261116-2227）的 gram 在 mem.doc 里
    df=0 → 权重全是 1e-6 → 权重和 3e-6 → 拼进 SQL 时被格式化成 0.0000 → PG 抛
    `division by zero`，整条 search 直接返错。所以 0 / NaN / inf / 负数 / 非数字 一律当 1。
    """
    try:
        df = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(df) or df < 1.0:
        return 1.0
    return df


def _weight_sum(weights: list[float]) -> float:
    """权重和 → SQL 里的除数：非有限/非正的值一律剔除，结果下限 1e-6（绝不能是 0）。"""
    total = 0.0
    for w in weights:
        try:
            v = float(w)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            total += v
    return max(total, 1e-6)


def _idf_weights(
    cur, user_id: str, n_terms: int, params: dict[str, Any]
) -> list[float]:
    """按 mem.doc 文档频率算 IDF 权重（落盘缓存，不建表）。

    为什么需要它：覆盖度门槛若按 gram「个数」算，多出来的虚词 gram 会把覆盖率拉低，
    只命中真词的正确答案反而被门槛挡在外面——漏召回比排错更严重。
    为什么落盘：2 字 gram 用不上 trgm 索引，全表统计一次约 1.5s；词频变化很慢，
    结果写到 app/.cache/term_df.json，冷启动慢一次，之后毫秒级。
    """
    _df_cache_load()
    now = time.time()
    weights: list[float] = [0.0] * n_terms
    missing: list[int] = []
    for i in range(n_terms):
        key = f"{user_id}:{params[f't{i}']}"
        hit = _DF.get(key)
        if hit and now - float(hit.get("ts", 0)) <= _DF_TTL:
            df = _safe_df(hit.get("df", 1))
            weights[i] = _df_weight(df)
        else:
            missing.append(i)
    if missing:
        exprs = ", ".join(
            f"count(*) filter (where (title ilike %(p{i})s or body ilike %(p{i})s))"
            for i in missing
        )
        cur.execute(f"select {exprs} from mem.doc where user_id = %(uid)s", params)
        for i, df in zip(missing, cur.fetchone()):
            df = _safe_df(df)
            weights[i] = _df_weight(df)
            _DF[f"{user_id}:{params[f't{i}']}"] = {"df": df, "ts": now}
        _df_cache_save()
    return weights


def _personal_score_and_match(
    body_expr: str,
    n_terms: int,
    op: str,
    quote_penalty: bool = False,
    weights: list[float] | None = None,
) -> tuple[str, str]:
    """生成 mem.doc / mem.conv 共用的排序表达式与过滤条件。

    过滤：op='cover' 按 IDF 加权覆盖度（>= 总权重的 30%）进候选；op='and' 全词命中；
    op='or' 任一命中（兜底放宽）。
    排序：标题命中（乘性加权） > 词覆盖度 > 命中密度 > 整串相似度，再乘按表降权与引用惩罚。
    """
    w = weights if weights and len(weights) == n_terms else [1.0] * n_terms
    # 除数绝不为 0：全部权重都是 1e-6 时（英文/编号查询常见），旧写法会格式化成 0.0000 → PG 除零。
    wsum = _weight_sum(w)
    in_title = [f"title ilike %(p{i})s" for i in range(n_terms)]
    hit = [f"(title ilike %(p{i})s or {body_expr} ilike %(p{i})s)" for i in range(n_terms)]
    cover = " + ".join(f"(case when {h} then {x:.6f} else 0 end)" for h, x in zip(hit, w))
    if op == "cover":
        match = f"({cover}) >= {0.30 * wsum:.6f}"
    elif op == "and":
        match = " and ".join(hit)
    else:
        match = " or ".join(hit)
    w_title = [
        f"word_similarity(lower(%(t{i})s), lower(title)) * {x:.4f}" for i, x in enumerate(w)
    ]
    w_body = [
        f"word_similarity(lower(%(t{i})s), lower({body_expr})) * {x:.4f}"
        for i, x in enumerate(w)
    ]
    density = [
        f"((length(lower({body_expr}))"
        f" - length(replace(lower({body_expr}), lower(%(t{i})s), '')))::float"
        f" / greatest(length(%(t{i})s), 1) / greatest(length({body_expr}), 1)) * {x:.4f}"
        for i, x in enumerate(w)
    ]
    title_hits = (
        f"({' + '.join(f'(case when title ilike %(p{i})s then 1 else 0 end)' for i in range(n_terms))})"
    )
    base = (
        f"(case when {' and '.join(in_title)} then 0.6 else 0 end)"
        f" + ({' + '.join(w_title)}) / {wsum:.6f}"
        f" + ({' + '.join(w_body)}) / {wsum:.6f} * 0.5"
        f" + ({' + '.join(density)}) / {wsum:.6f} * 100.0"
        " + similarity(lower(title), lower(%(q)s)) * 0.3"
    )
    # 标题命中每词 +50%（乘性）：人工结论标题短、词少，命中一个就该重排上来。
    base = f"({base}) * (1.0 + 0.5 * ({title_hits}))"
    # 按表降权 0.5：插件每轮自动生成的记录（title 形如 session-xxxxxxxx-…）不是人工结论。
    penalty = "case when title ~ '^session-[0-9a-f]{{8}}-' then 0.5 else 1.0 end"
    score = f"({base}) * ({penalty})"
    # 引用惩罚：正文含整条 query 原文的文档打 0.3 折（引用不等于回答）。
    if quote_penalty:
        score = (
            f"({score}) * (case when {body_expr} like %(qpat)s"
            f" then {_QUOTE_PENALTY} else 1.0 end)"
        )
    return score, match


def _personal_rows(
    cur,
    params: dict[str, Any],
    n_terms: int,
    limit: int,
    op: str,
    weights: list[float] | None = None,
    include_pending: bool = False,
) -> list[dict[str, Any]]:
    score_doc, match_doc = _personal_score_and_match(
        "body", n_terms, op, quote_penalty=True, weights=weights
    )
    score_conv, match_conv = _personal_score_and_match(
        "coalesce(snippet, '')", n_terms, op, weights=weights
    )
    params = _visibility.with_hidden({
        **params,
        "incl_pending": bool(include_pending),
        "meta_re": _META_TITLE_RE.pattern,
        "meta_pen": _META_PENALTY,
    })
    # 标题是元笔记（讲记忆库/检索/MCP 本身）→ 排序时降权，但不删除结果。
    score_doc = (
        f"({score_doc}) * (case when title ~* %(meta_re)s then %(meta_pen)s else 1.0 end)"
    )
    terms = [str(params.get(f"t{i}", "")) for i in range(n_terms)]
    rows: list[dict[str, Any]] = []
    cur.execute(
        f"""select path, kind, title, body, updated_at, {score_doc} as score
            from mem.doc
            where user_id = %(uid)s and ({match_doc})
              and (%(incl_pending)s or path not like '%%/pending/%%')
              and path not like '%%/rejected/%%'
              {_visibility.hidden_param()}
            order by score desc, updated_at desc limit %(lim)s""",
        params,
    )
    for row in cur.fetchall():
        at = row[4].isoformat() if row[4] else None
        info = _match_info(terms, row[2] or "", row[3] or "", _snippet(row[3]))
        rows.append({
            "scope": "personal",
            "type": "doc",
            "source": "mem.doc",
            "kind": row[1],
            "id": row[0],
            "title": row[2] or "",
            "snippet": _snippet(row[3]),
            "date": at,
            "at": at,
            "last_synced_at": at,
            "anchor": _anchor("mem.doc", row[0], at=at),
            "meta": _is_meta_note(row[2] or "", row[0] or ""),
            "match": info,
            "score": round(float(row[5]) * _DOC_SCORE_WEIGHT, 4),
        })
    cur.execute(
        f"""select id, source, title, snippet, updated_at, message_count, {score_conv} as score
            from mem.conv
            where user_id = %(uid)s and ({match_conv})
              and id <> all(%(sup)s)
            order by score desc, updated_at desc limit %(lim)s""",
        params,
    )
    for row in cur.fetchall():
        at = row[4].isoformat() if row[4] else None
        info = _match_info(terms, row[2] or "", row[3] or "", _snippet(row[3]))
        rows.append({
            "scope": "personal",
            "type": "conversation",
            "source": "mem.conv",
            "kind": "conversation",
            "id": row[0],
            "session": row[0],
            "title": row[2] or row[1],
            "snippet": _snippet(row[3] or ""),
            "date": at,
            "at": at,
            "last_synced_at": at,
            "message_count": row[5],
            "anchor": _anchor("mem.conv", row[0], at=at),
            "match": info,
            "score": round(float(row[6]), 4),
        })
    rows.sort(key=lambda x: (x.get("score") or 0.0, x.get("date") or ""), reverse=True)
    return rows[:limit]


def search_personal(query: str, user_id: str, limit: int = 10, include_pending: bool = False) -> list[dict[str, Any]]:
    """个人检索：gram 模式按 IDF 加权覆盖度召回 + 加权排序；legacy 模式保持原行为。

    user_id 过滤是 SQL 硬条件，检索结果不会跨用户（隔离用例见 tests/test_search_isolation.py）。
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(limit, 50))
    terms = _digit_variants(_expand_synonyms(_terms(q) or [q]), q)
    params: dict[str, Any] = {
        "uid": user_id,
        "lim": limit,
        "q": q,
        "qpat": _like(q),  # 整条 query 的转义 like 模式，供引用惩罚使用
    }
    for i, term in enumerate(terms):
        params[f"p{i}"] = _like(term)
        params[f"t{i}"] = term
    gram_mode = os.environ.get("MEM_RETRIEVAL", "gram").strip().lower() != "legacy"
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            weights = _idf_weights(cur, user_id, len(terms), params) if gram_mode else None
            first_op = "cover" if gram_mode else "and"
            out = _personal_rows(cur, params, len(terms), limit, first_op, weights, include_pending)
            if gram_mode and len(out) < 3 and len(terms) > 1:
                # 召回不足时补一轮 OR 并合并（漏召回比排错更严重，宁可多给候选再精排）
                extra = _personal_rows(cur, params, len(terms), limit, "or", weights, include_pending)
                seen = {r.get("id") for r in out}
                out += [r for r in extra if r.get("id") not in seen]
                out.sort(key=lambda x: (x.get("score") or 0.0, x.get("date") or ""), reverse=True)
                out = out[:limit]
            if not out and len(terms) > 1:
                out = _personal_rows(cur, params, len(terms), limit, "or", weights, include_pending)
    return out


def search_public(query: str, limit: int = 10) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(limit, 50))
    pat = _like(q)
    with _conn(ZHISHU_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """select id, source, title, body, updated_at
                   from cms.search_docs
                   where title ilike %s or body ilike %s
                   order by updated_at desc nulls last
                   limit %s""",
                (pat, pat, limit),
            )
            rows = cur.fetchall()
    return [
        {
            "scope": "public",
            "type": "public_doc",
            "source": row[1],
            "id": row[0],
            "title": row[2] or "",
            "snippet": _snippet(row[3]),
            "date": row[4].isoformat() if row[4] else None,
        }
        for row in rows
    ]


def _search_rule_results(user_id: str, query: str,
                         results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把已批准规则插到 search 结果最前面（pinned 无条件，其他按 query 命中）。"""
    recs = [r for r in _rules.active(user_id)
            if r.get("kind") == "rule" or r.get("priority", 50) > 50]
    if not recs:
        return results, []
    by_id = {str(r.get("id")): r for r in results}
    items: list[dict[str, Any]] = []
    for rec in recs[:8]:
        path = rec.get("path") or ""
        if not path:
            continue
        matched = _rule_hit(rec, query)
        # gate 无条件注入；search 只在 query 命中规则标题/摘要/项目/主题时置顶。
        if not matched:
            continue
        kind = rec.get("kind") or "rule"
        item = {
            "scope": "personal", "type": "rule" if kind == "rule" else "doc",
            "source": "mem.doc", "kind": kind,
            "id": path, "title": rec.get("title") or "",
            "snippet": _snippet(rec.get("summary") or "", 320),
            "score": round(10.0 + (int(rec.get("priority") or 0) / 100.0), 4),
            "priority": int(rec.get("priority") or 0),
            "pinned": bool(rec.get("pinned")), "project": rec.get("project") or "",
            "topics": rec.get("topics") or [], "rule": kind == "rule", "match": matched,
        }
        old = by_id.get(path)
        if old is not None:
            old.update(item)
            old["score"] = max(float(old.get("score") or 0), item["score"])
            item = old
        else:
            try:
                with _conn(MEM_DSN) as c:
                    with c.cursor() as cur:
                        cur.execute(
                            "select body, updated_at from mem.doc where user_id=%s and path=%s",
                            (user_id, path),
                        )
                        row = cur.fetchone()
                if row:
                    at = row[1].isoformat() if row[1] else None
                    item["snippet"] = _snippet(row[0] or "", 320)
                    item["date"] = at
                    item["at"] = at
                    item["anchor"] = _anchor("mem.doc", path, at=at)
                    results.append(item)
            except Exception:
                continue
        items.append(item)
    results.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    return results, items


def search(query: str, user_id: str, limit: int = 10, scope: str = "auto", include_pending: bool = False,
           include_conversations: bool = True) -> dict[str, Any]:
    scope = (scope or "auto").lower()
    limit = max(1, min(limit, 50))
    if scope == "personal":
        results = search_personal(query, user_id, limit, include_pending)
    elif scope == "public":
        results = search_public(query, limit)
    else:
        personal = search_personal(query, user_id, limit, include_pending)
        public = search_public(query, limit)
        results = (personal + public)[:limit]

    # 规则层：已批准规则优先于普通文档；query 命中时置顶。
    rule_items: list[dict[str, Any]] = []
    if scope != "public":
        try:
            results, rule_items = _search_rule_results(user_id, query, results)
        except Exception:
            rule_items = []

    # P0（2026-11-19）：文档/会话元数据常常只沾到 query 里的一两个词，
    # 真正「聊过这件事」的会话在 13 万条消息里。单独给一栏会话命中，
    # 不挤占 results 的排序，客户端也不会再被迫跳出 MCP 去 grep。
    conversation_hits: list[dict[str, Any]] = []
    if include_conversations and scope != "public" and len((query or "").strip()) >= 2:
        try:
            conversation_hits = conv_search(
                query, user_id=user_id, limit=5, context=0
            ).get("matches", [])
        except Exception:
            conversation_hits = []
    return {
        "query": query,
        "scope": scope,
        "results": results,
        "count": len(results),
        "rules": rule_items,
        "conversation_hits": conversation_hits,
        "hints": [
            "results 里 type=doc 的用 id（path）按需取正文；type=conversation 的用 conv_search 在会话内定位。",
            "conversation_hits 是消息级命中，带 session/seq/at/anchor，可直接用 conv_get 取上下文。",
            "只想要某一条会话的全文时用 conv_get(compact=True)；不要整段导出再本地 grep。",
        ],
    }


def doc_get(user_id: str, doc_id: str, max_chars: int = 20000) -> dict[str, Any]:
    """按 path 读一条记忆正文（search 只回 snippet；这里给全文/截断全文）。"""
    path = (doc_id or "").strip()
    if not path:
        raise ValueError("doc_id 不能为空")
    max_chars = max(200, min(int(max_chars), 100000))
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                "select path, kind, title, body, updated_at from mem.doc "
                "where user_id=%s and path=%s",
                (user_id, path),
            )
            row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "not found", "id": path}
    body = row[3] or ""
    at = row[4].isoformat() if row[4] else None
    return {
        "ok": True, "id": row[0], "kind": row[1], "title": row[2] or "",
        "updated_at": at, "anchor": _anchor("mem.doc", row[0], at=at),
        "chars": len(body), "body": body[:max_chars],
        "truncated": len(body) > max_chars, "max_chars": max_chars,
    }


def rule_set(user_id: str, doc_id: str, priority: int | None = None,
             pinned: bool | None = None, project: str | None = None,
             topics: str | None = None, note: str = "") -> dict[str, Any]:
    """AI 把一条已存在的记忆提权为规则/调整权重（自审，写 rules.jsonl）。"""
    doc_id = (doc_id or "").strip()
    if not doc_id:
        raise ValueError("doc_id 不能为空")
    if "/pending/" in doc_id:
        return {"ok": False, "error": "pending 文档先 review 通过，再 rule_set"}
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                "select path, kind, title, body from mem.doc where user_id=%s and path=%s",
                (user_id, doc_id),
            )
            row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "找不到这条记忆"}
    rec = _rules.get(doc_id) or {}
    summary = rec.get("summary") or ""
    if len(summary) < 400:
        summary = _snippet(row[3] or "", 1200)
    updated = _rules.set_rule(
        doc_id, uid=user_id, kind="rule", title=row[2] or rec.get("title") or "",
        summary=summary,
        priority=(priority if priority is not None else rec.get("priority", 100)),
        pinned=(pinned if pinned is not None else rec.get("pinned", True)),
        project=(project if project is not None else rec.get("project", "")),
        topics=(topics if topics is not None else rec.get("topics", [])),
        approved=True, source=rec.get("source", "rule_set"),
        anchor=rec.get("anchor", f"mem.doc:{doc_id}"), by="ai",
    )
    return {"ok": True, "rule": updated, "note": note or "规则已提权，gate 下一次请求即生效"}


def rule_list(user_id: str, include_all: bool = False) -> dict[str, Any]:
    rows = _rules.active(user_id)
    if include_all:
        rows = [r for r in _rules._fold().values() if r.get("uid") == user_id]
        rows.sort(key=lambda r: (bool(r.get("pinned")), int(r.get("priority") or 0)),
                  reverse=True)
    return {"ok": True, "count": len(rows), "rules": rows, "stats": _rules.stats(user_id)}


_HELP_GUIDE: dict[str, Any] = {
    "purpose": "跨 AI 协作的记忆检索底座：先选对工具，再取最小够用的上下文。",
    "tools": {
        "search": {
            "when": "不知道在哪，先做全局检索；返回文档/会话元数据 + conversation_hits（消息级命中）。",
            "example": 'search(query="伤寒论三家合参笔记.pdf", limit=10)',
            "notes": "results[].type=doc|conversation；conversation_hits[] 带 session/seq/at/anchor。",
        },
        "conv_search": {
            "when": "已知大概在某条会话，或 search 的 conversation_hits 命中了，做会话内关键词定位。",
            "example": 'conv_search(keyword="三家合参", conv_id="dsh:session-...", limit=20, context=1)',
            "notes": "返回 seq/role/at/snippet/context/anchor；不跨 user_id。",
        },
        "conv_list": {
            "when": "先按时间窗/source 找会话，不知道关键词时用。",
            "example": 'conv_list(since="7d", source="workbuddy", limit=20)',
        },
        "conv_get": {
            "when": "已知 conv_id，要读原文；先 compact=True 看轮廓，再按 seq 取局部。",
            "example": 'conv_get(conv_id="...", compact=True, limit=100)',
            "notes": "keyword 可在会话内过滤；返回 last_synced_at / sync_status / anchor。",
        },
        "doc_get": {
            "when": "search 只回了 snippet，需要读某条记忆/规则的完整正文时。",
            "example": 'doc_get(doc_id="cloud/owner/20261119-xxx.md", max_chars=20000)',
        },
        "gate": {
            "when": "回答前强制取回相关上下文；DSH 插件每轮自动调用。",
            "example": 'gate(query="...", source="dsh", conversation_id="...")',
        },
        "remember": {
            "when": "写入结论/决策/规则；带 anchor（文件:行号 / session / 命令原文）便于以后溯源。",
            "example": 'remember(text="建表前先盘点老库", kind="rule", priority=100, pin=True, anchor="docs/xxx.md:12")',
        },
        "rule_set": {
            "when": "AI 检索/编码后认为某条记忆应成为铁律：提权、pin、补 project/topics。",
            "example": 'rule_set(doc_id="cloud/owner/xxx.md", priority=100, pinned=True, project="cms")',
        },
        "rule_list": {
            "when": "看当前生效规则和权重，确认下一次 gate 会注入什么。",
        },
        "review": {
            "when": "AI 自审：把 pending 的规则/结论转正或打回（actor=ai，写 review_log）。",
            "example": 'review(doc_id="cloud/owner/pending/xxx.md", decision="pass", reason="有出处且无冲突")',
        },
        "pending": {
            "when": "看待审积压有多少条、最老一条多久。",
        },
        "doc_history": {
            "when": "查某条记忆的变更时间线（append-only + hash chain）。",
        },
        "feedback": {
            "when": "记录这条记忆是否有用；可选 0-5 分项评分。",
        },
    },
    "order": ["规则层（gate 无条件注入）", "search → conversation_hits",
              "conv_search (定位)", "conv_get (compact → 局部)"],
}


def help_text(topic: str = "") -> dict[str, Any]:
    """工具选型引导；topic 可传 search / conversation / write / review，留空给全量。"""
    key = (topic or "").strip().lower()
    if not key:
        return {"ok": True, **_HELP_GUIDE}
    aliases = {
        "search": ["search", "conv_search", "conv_list", "conv_get", "doc_get"],
        "conversation": ["search", "conv_search", "conv_list", "conv_get", "doc_get"],
        "write": ["remember", "pending"],
        "review": ["pending", "doc_history", "feedback", "review"],
        "rule": ["remember", "rule_set", "rule_list", "review"],
        "rules": ["remember", "rule_set", "rule_list", "review"],
    }
    names = aliases.get(key)
    if not names:
        return {"ok": False, "error": f"未知 topic：{topic}", "topics": sorted(aliases)}
    return {
        "ok": True,
        "topic": key,
        "tools": {name: _HELP_GUIDE["tools"].get(name, {}) for name in names},
        "order": _HELP_GUIDE["order"],
    }


def capabilities() -> dict[str, Any]:
    """给客户端做工具选型用的能力清单（不依赖库，永远可用）。"""
    return {
        "ok": True,
        "server": "zhishu-memory",
        "transport": "MCP streamable HTTP + REST mirror",
        "tools": sorted(_HELP_GUIDE["tools"]),
        "retrieval": {
            "search": "docs + conversation metadata + conversation_hits; approved rules rank first",
            "conv_search": "keyword search inside one/all conversations, returns anchors",
            "conv_get": "transcript with compact/keyword/last_synced_at/reconcile",
            "doc_get": "read a memory/rule body by path after search",
            "rules": "remember(kind=rule, priority, pin, project) → auto-review → gate injects unprompted",
        },
        "identity": "x-api-key；未开 REQUIRE_KEY 时落到 MEM_DEFAULT_USER",
        "help": "help(topic='search'|'conversation'|'write'|'review'|'rule')",
    }

# ---------------- 写入幂等键（2026-11-18 老大定） ----------------
# 为什么必须有：客户端超时会重试（今早实测一次 30s 超时重试，`书页优点对表` 落了两条 event），
# 重复 event 会直接污染退化检查（duplicate_facts 误报）、让同一事项挂出一堆重复 links——
# 正是我们在防的「越用越烂」。所以：同一个键 30 分钟内视为重复，直接返回已存 id，不再写行。
_IDEM_PATH = pathlib.Path(__file__).resolve().parents[2] / "data" / "audit" / "idem.jsonl"
IDEM_TTL_SECONDS = 30 * 60


def _idem_key(idem_key: str, source: str, conversation_id: str, body: str) -> str:
    """显式键优先；没给就按 (source, conversation_id, 文本 hash) 算——客户端不用改也能幂等。"""
    explicit = (idem_key or "").strip()
    if explicit:
        return explicit[:200]
    raw = f"{source or 'session'}|{conversation_id or ''}|{body or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _idem_lookup(key: str) -> dict[str, Any] | None:
    """查 30 分钟内的同键命中；返回 {doc_id, age_seconds} 或 None。event 流水也走这条。"""
    if not _IDEM_PATH.exists() or not key:
        return None
    now = time.time()
    hit = None
    for line in _IDEM_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("key") != key:
            continue
        try:
            age = now - datetime.fromisoformat(str(rec.get("ts")).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if 0 <= age <= IDEM_TTL_SECONDS:
            hit = {"doc_id": rec.get("doc_id"), "age_seconds": round(age, 1),
                   "first_ts": rec.get("ts")}
    return hit


def _idem_record(key: str, doc_id: str) -> None:
    try:
        _IDEM_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _IDEM_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                 "key": key, "doc_id": doc_id}, ensure_ascii=False) + "\n")
    except Exception:
        pass          # 幂等登记失败不能连累写入本身


def remember(user_id: str, text: str, kind: str = "event", title: str = "", topics: str = "",
             source: str = "", anchor: str = "", supersedes: str = "",
             force_new: bool = False, idem_key: str = "",
             conversation_id: str = "", priority: int | None = None,
             pin: bool | None = None, project: str = "") -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("内容不能为空")
    # 幂等：同键 30 分钟内重复请求 → 直接返回已存 id，不写文档、不写 Observation、不挂事项。
    idem = _idem_key(idem_key, source, conversation_id, body)
    hit = _idem_lookup(idem)
    if hit:
        return {"ok": True, "id": hit["doc_id"], "idem": "hit", "idem_key": idem,
                "age_seconds": hit["age_seconds"], "deduped": True,
                "observation": {}, "issue": {},
                "note": f"同键 {IDEM_TTL_SECONDS // 60} 分钟内已写过，直接命中已存记录"}
    kind = (kind or "event").strip().lower()
    # v1 review-gated：不再静默把未知 kind 降级成 event（今天踩过「新造类型被悄悄改掉」），
    # 原样保留，交给 review 的白名单去拦。
    first = body.splitlines()[0].strip().lstrip("#").strip()
    title = (title or first)[:120]
    topic_list = [t.strip() for t in (topics or "").split(",") if t.strip()]
    rule_mode = kind == "rule"
    # 规则/经验默认最高权重并常驻 gate；普通文档显式传 priority/pin 才进入权重层。
    boost_requested = rule_mode or priority is not None or pin is not None or bool(project)
    try:
        priority = 100 if rule_mode and priority is None else max(
            1, min(100, int(priority if priority is not None else 50)))
    except (TypeError, ValueError):
        priority = 100 if rule_mode else 50
    pinned = True if rule_mode and pin is None else bool(pin)
    project = (project or "").strip()
    body_core = body
    if rule_mode:
        bits = [f"priority={priority}", f"pin={'true' if pinned else 'false'}"]
        if project:
            bits.append(f"project={project}")
        if topic_list:
            bits.append("topics=" + ",".join(topic_list))
        body = body_core + "\n\n[rule] " + " ".join(bits)
    digest = hashlib.sha256(body.encode()).hexdigest()
    day = datetime.now().strftime("%Y%m%d")
    # v1 review-gated：path 就是状态（零 DDL）。
    #
    # v1.6 豁免（2026-11-17 幕后大佬定）：**所有 kind=event 一律直接入库，不进 pending**。
    # 2026-11-19：kind=rule 走 AI 自审快车道（默认开，MEM_RULE_AUTOREVIEW=0 可关）：
    #   规则是「该怎么做」，必须让下一次 CREATE TABLE 前看得到；埋在 90 条 pending 里等于没有。
    #   入口处仍写 review_log(auto_pass) + rules.jsonl，保留可追溯/可撤销。
    rule_auto = bool(
        rule_mode
        and os.getenv("MEM_RULE_AUTOREVIEW", "1").strip().lower() not in ("0", "off", "false", "no")
        and len(body_core) >= 40
    )
    auto_writeback = kind == "event" or rule_auto
    path_dir = f"cloud/{user_id}" if auto_writeback else f"cloud/{user_id}/pending"
    path = f"{path_dir}/{day}-{digest[:8]}.md"

    # Observation 写入前判定（2026-11-18，账号主指定语义）：
    # 先决定「新建 / 补证据 / 更正 / 不动」，再决定要不要落 mem.doc。
    # 判定在写入前做，不是写进去再合并——同一条事实写第二次不应该多出一行。
    statement = (title or first)[:200]
    # Observation 只接「结论类」写入（decision/artifact/belief/metric/entity）。
    # 为什么 event 不走：event 是「发生了什么」的流水，标题常是同一句开场（「收到」「补写」），
    # 按相似度判重会把两条不同的事静默折成一条——那是比遗忘更严重的丢历史。
    # 结论类才会出现「同一条事实反复写」和「旧结论被推翻」，正是这次要治的病。
    obs_kinds = {"decision", "artifact", "belief", "metric", "entity"}
    decision = None
    if kind in obs_kinds:
        # 接替一条「Observation 模型之前」写的老文档：先按 mem.doc 现状懒回填一行，
        # 否则 supersedes 会指向不存在的 Observation（线上 3065 条历史文档都是这种）。
        if supersedes and supersedes not in _observation.load(user_id):
            legacy = _doc_meta(supersedes, user_id)
            if legacy:
                _observation.backfill(supersedes, legacy["title"], legacy["kind"], user_id,
                                      ts=legacy["updated_at"])
        decision = _observation.decide(
            statement=statement, kind=kind, user_id=user_id, obs_id=path,
            evidence=[{
                "source": source or "session",
                "anchor": anchor,
                "note": body[:160],
                "ts": datetime.now(timezone.utc).isoformat(),
            }],
            supersedes=supersedes, force_new=force_new,
        )
    obs_out = {k: decision.get(k) for k in
               ("action", "status", "statement", "evidence_count", "similarity",
                "supersedes", "reason") if decision and decision.get(k) is not None}
    if decision is not None and not decision.get("doc_should_write"):
        # evidence / noop：已有 Observation 吸收这次信息，不新增文档行（行数不变）。
        _idem_record(idem, decision["obs_id"])
        return {"ok": True, "id": decision["obs_id"], "kind": decision["kind"],
                "title": title, "topics": topic_list, "observation": obs_out,
                "idem": "miss", "idem_key": idem,
                "deduped": True, "auto_approved": False}

    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            # P4 词法去重（零 DDL）：先跟正式区最近 200 条比 3-gram Jaccard，>=0.62 判重不写。
            # 默认关闭（MEM_DEDUPE=on 才启用）：今晚只交付 P3，P4 等明早评审后再开闸。
            dedupe_on = os.environ.get("MEM_DEDUPE", "off").strip().lower() in ("1", "on", "true", "yes")
            cur.execute(
                f"select path, body from mem.doc where user_id=%(uid)s "
                f"and path not like '%%/pending/%%' and path not like '%%/rejected/%%' "
                f"{_visibility.hidden_param()} order by updated_at desc limit 200",
                _visibility.with_hidden({"uid": user_id}),
            )
            best_id, best_score = None, 0.0
            for cand_path, cand_body in (cur.fetchall() if dedupe_on else []):
                s = _quality.jaccard(body, cand_body or "")
                if s > best_score:
                    best_id, best_score = cand_path, s
            if best_id and best_score >= 0.62:
                return {"ok": True, "deduped": True, "similar_to": best_id,
                        "score": round(best_score, 4), "id": best_id}
            cur.execute("select hash, length(body) from mem.doc where path=%s", (path,))
            prev_row = cur.fetchone()
            cur.execute(
                """insert into mem.doc (path, kind, title, body, hash, updated_at, user_id)
                   values (%s,%s,%s,%s,%s,now(),%s)
                   on conflict (path) do update
                     set title=excluded.title, body=excluded.body, hash=excluded.hash,
                         kind=excluded.kind, updated_at=now()""",
                (path, kind, title, body, digest, user_id),
            )
            for topic in topic_list:
                tid = f"{user_id}:topic:{topic}"
                cur.execute(
                    """insert into mem.entity (id, kind, name, updated_at, user_id)
                       values (%s,'topic',%s,now(),%s)
                       on conflict (id) do update set updated_at=now()""",
                    (tid, topic, user_id),
                )
                cur.execute(
                    """insert into mem.edge (src,dst,relation,weight,updated_at,user_id)
                       values (%s,%s,'topic',1,now(),%s)
                       on conflict (src,dst,relation) do update set updated_at=now()""",
                    (path, tid, user_id),
                )
        c.commit()
    _history.add(path, "update" if prev_row else "create",
                 "auto" if auto_writeback else "user",
                 old_hash=(prev_row[0] if prev_row else ""), new_hash=digest,
                 old_len=(prev_row[1] if prev_row else None), new_len=len(body),
                 reason=(("rule auto-review" if rule_auto else
                          ("write-back 摘要豁免" if auto_writeback else ""))))
    rule_rec = None
    if boost_requested:
        rule_rec = _rules.set_rule(
            path, uid=user_id, kind=kind, title=title, summary=_snippet(body_core, 1200),
            priority=priority, pinned=pinned, project=project, topics=topic_list,
            approved=auto_writeback, source=source, anchor=anchor,
            by="auto" if auto_writeback else "ai",
        )
    if auto_writeback:
        # 豁免必须留痕：每天早上要能数出「昨天自动通过了几条」，否则这条豁免就是法外之地。
        _review_log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "id": path,
            "kind": kind,
            "title": title,
            "decision": "auto_pass",
            "auto": True,
            "reason": ("规则类 AI 自审快车道（MEM_RULE_AUTOREVIEW）；入口即写 rules.jsonl"
                       if rule_auto else
                       "write-back 摘要豁免（kind=event）：v1.5 临时方案，v2 改显式 auto_approved 参数"),
        })
    # 事后聚类（2026-11-18）：写完就自己找事项，挂不上记 orphan，绝不打断写入、绝不反问。
    assign: dict[str, Any] = {}
    try:
        assign = _issues.auto_assign(path, title, body, kind)
    except Exception as exc:
        assign = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _idem_record(idem, path)
    return {"ok": True, "id": path, "kind": kind, "title": title, "topics": topic_list,
            "observation": obs_out, "issue": assign, "idem": "miss", "idem_key": idem,
            "auto_approved": auto_writeback,
            "priority": priority, "pinned": bool(pinned), "project": project,
            "rule": rule_rec}


def _doc_meta(path: str, user_id: str) -> dict[str, str] | None:
    """取一条老文档的标题/类型/更新时间，用于 Observation 懒回填。"""
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                "select title, kind, updated_at from mem.doc where user_id=%s and path=%s",
                (user_id, path),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {"title": row[0] or "", "kind": row[1] or "event",
            "updated_at": row[2].isoformat() if row[2] else ""}


def whoami(user_id: str) -> dict[str, Any]:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select id, name, created_at from mem.app_user where id = %s", (user_id,))
            row = cur.fetchone()
    return {"user_id": user_id, "name": row[1] if row else user_id, "created_at": row[2].isoformat() if row else None}


def stats(user_id: str) -> dict[str, Any]:
    out = _stats_core(user_id)
    out["feedback_7d"] = _quality.feedback_stats(7)
    return out


def _stats_core(user_id: str) -> dict[str, Any]:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select count(*) from mem.doc where user_id = %s", (user_id,))
            docs = cur.fetchone()[0]
            cur.execute("select count(*) from mem.conv where user_id = %s", (user_id,))
            convs = cur.fetchone()[0]
            cur.execute("select kind, count(*) from mem.doc where user_id = %s group by 1 order by 2 desc", (user_id,))
            kinds = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(
                """select source, count(*), max(updated_at) from mem.conv
                   where user_id = %s group by 1 order by 2 desc""",
                (user_id,),
            )
            tools = [
                {"tool": r[0], "sessions": r[1], "last": r[2].isoformat() if r[2] else None}
                for r in cur.fetchall()
            ]
    return {
        "user_id": user_id,
        "documents": docs,
        "conversations": convs,
        "tools_used": len(tools),
        "tools": tools,
        "by_kind": kinds,
        "images_generated": None,
        "note": "images_generated 暂无采集表；先返回 null，不编数字。",
    }


# ---------------- ideas / todos / ledger ----------------

def _new_id() -> str:
    return uuid.uuid4().hex[:20]


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def idea_add(user_id: str, title: str, body: str = "", priority: int = 2, parent_id: str = "") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("思路标题不能为空")
    iid = _new_id()
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """insert into mem.idea (id,user_id,title,body,status,priority,parent_id)
                   values (%s,%s,%s,%s,'open',%s,nullif(%s,''))""",
                (iid, user_id, title, body or "", max(1, min(int(priority), 5)), parent_id or ""),
            )
        c.commit()
    return {"ok": True, "id": iid, "title": title, "status": "open"}


def idea_list(user_id: str, status: str = "", limit: int = 50) -> dict:
    limit = max(1, min(limit, 100))
    sql = """select id,title,status,priority,decision,created_at,updated_at
             from mem.idea where user_id = %s"""
    args = [user_id]
    if status:
        sql += " and status = %s"
        args.append(status)
    sql += " order by updated_at desc limit %s"
    args.append(limit)
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return {"count": len(rows), "ideas": [
        {"id": r[0], "title": r[1], "status": r[2], "priority": r[3],
         "decision": r[4], "created_at": r[5].isoformat() if r[5] else None,
         "updated_at": r[6].isoformat() if r[6] else None}
        for r in rows
    ]}


def idea_breakdown(user_id: str, idea_id: str, steps: list[str]) -> dict:
    clean = [s.strip() for s in steps if str(s).strip()]
    if not clean:
        raise ValueError("至少给一个拆分步骤")
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select coalesce(max(seq),0) from mem.idea_step where idea_id = %s and user_id = %s", (idea_id, user_id))
            seq = cur.fetchone()[0]
            created = []
            for i, title in enumerate(clean, start=1):
                sid = _new_id()
                cur.execute(
                    """insert into mem.idea_step (id,idea_id,user_id,seq,title,status)
                       values (%s,%s,%s,%s,%s,'pending')""",
                    (sid, idea_id, user_id, seq + i, title),
                )
                created.append({"id": sid, "seq": seq + i, "title": title, "status": "pending"})
            cur.execute("update mem.idea set status = case when status='open' then 'planned' else status end, updated_at = now() where id=%s and user_id=%s", (idea_id, user_id))
        c.commit()
    return {"ok": True, "idea_id": idea_id, "steps": created}


def idea_decide(user_id: str, idea_id: str, decision: str, status: str = "planned") -> dict:
    if status not in {"planned", "executing", "review", "done"}:
        status = "planned"
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """update mem.idea set decision=%s, decision_at=now(), status=%s, updated_at=now()
                   where id=%s and user_id=%s""",
                (decision or "", status, idea_id, user_id),
            )
            if cur.rowcount == 0:
                raise ValueError("思路不存在")
        c.commit()
    return {"ok": True, "idea_id": idea_id, "decision": decision, "status": status}


def idea_update(user_id: str, idea_id: str, status: str) -> dict:
    if status not in {"open", "planning", "planned", "executing", "review", "done"}:
        raise ValueError("非法状态")
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("update mem.idea set status=%s, updated_at=now() where id=%s and user_id=%s", (status, idea_id, user_id))
            if cur.rowcount == 0:
                raise ValueError("思路不存在")
        c.commit()
    return {"ok": True, "idea_id": idea_id, "status": status}


def idea_steps(user_id: str, idea_id: str) -> dict:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """select id,seq,title,status from mem.idea_step
                   where idea_id=%s and user_id=%s order by seq""",
                (idea_id, user_id),
            )
            rows = cur.fetchall()
    return {"idea_id": idea_id, "steps": [
        {"id": r[0], "seq": r[1], "title": r[2], "status": r[3]} for r in rows
    ]}


def idea_to_todos(user_id: str, idea_id: str) -> dict:
    """思路步骤转待办：**统一写 growth.time_entry（经 todo_store）**，不再写 mem.todo。

    2026-11-18 老大定：mem.todo 是遗留表，第三个"待办"会造成数据分裂——
    MCP todo 工具早就改到 growth.time_entry 了，这里单独写 mem.todo 等于再开一个源。
    """
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """select id,title from mem.idea_step
                   where idea_id=%s and user_id=%s and status='pending' order by seq""",
                (idea_id, user_id),
            )
            rows = cur.fetchall()
    created = []
    for step_id, title in rows:
        res = _todo_store.todo_add(
            user_id,
            title=title,
            detail="来自思路 " + str(idea_id) + " 的步骤 " + str(step_id),
            priority=2,
            source="idea:" + str(idea_id) + ":" + str(step_id),
            owner="ai",
        )
        created.append(res.get("id"))
    return {"ok": True, "created": len(created), "todo_ids": created}


from . import todo_store as _todo_store


def todo_add(user_id: str, title: str, detail: str = "", due_at: str = "",
             priority: int = 2, project: str = "", source: str = "",
             owner: str = "human") -> dict:
    return _todo_store.todo_add(user_id, title, detail, due_at, priority, project, source, owner)


def todo_list(user_id: str, status: str = "", limit: int = 50) -> dict:
    return _todo_store.todo_list(user_id, status, limit)


def todo_query(user_id: str, status: str = "", owner: str = "", priority: int | None = None,
               project: str = "", limit: int = 100) -> dict:
    return _todo_store.todo_query(user_id, status, owner, priority, project, limit)


def todo_update(user_id: str, todo_id: str, status: str = "", title: str = "",
                detail: str = "", due_at: str = "", priority: int | None = None,
                project: str = "") -> dict:
    return _todo_store.todo_update(user_id, todo_id, status, title, detail, due_at, priority, project)


def ledger_add(user_id: str, amount: float, note: str = "", category: str = "",
               occurred_at: str = "", merchant: str = "", currency: str = "CNY", source: str = "") -> dict:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ValueError("金额必须是数字")
    if value == 0:
        raise ValueError("金额不能为 0")
    lid = _new_id()
    when = _parse_dt(occurred_at) or datetime.now(timezone.utc)
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """insert into mem.ledger (id,user_id,occurred_at,amount,currency,category,merchant,note,source)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lid, user_id, when, value, currency or "CNY", category or "", merchant or "", note or "", source or ""),
            )
        c.commit()
    return {"ok": True, "id": lid, "amount": value, "currency": currency or "CNY",
            "occurred_at": when.isoformat(), "note": note}


def ledger_list(user_id: str, since: str = "", until: str = "", limit: int = 50) -> dict:
    limit = max(1, min(limit, 200))
    sql = """select id,occurred_at,amount,currency,category,merchant,note
             from mem.ledger where user_id = %s"""
    args = [user_id]
    if since:
        sql += " and occurred_at >= %s"; args.append(_parse_dt(since))
    if until:
        sql += " and occurred_at <= %s"; args.append(_parse_dt(until))
    sql += " order by occurred_at desc limit %s"; args.append(limit)
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return {"count": len(rows), "entries": [
        {"id": r[0], "occurred_at": r[1].isoformat() if r[1] else None,
         "amount": float(r[2]), "currency": r[3], "category": r[4],
         "merchant": r[5], "note": r[6]}
        for r in rows
    ]}


def ledger_summary(user_id: str, period: str = "today") -> dict:
    period = (period or "today").lower()
    if period == "month":
        start = "date_trunc('month', now())"
    elif period == "all":
        start = "'1970-01-01'::timestamptz"
    else:
        start = "date_trunc('day', now())"
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                f"""select coalesce(sum(amount),0), count(*) from mem.ledger
                    where user_id=%s and occurred_at >= {start}""",
                (user_id,),
            )
            total, count = cur.fetchone()
            cur.execute(
                f"""select coalesce(nullif(category,''),'(未分类)'), sum(amount), count(*)
                    from mem.ledger where user_id=%s and occurred_at >= {start}
                    group by 1 order by 2 desc""",
                (user_id,),
            )
            by_cat = [{"category": r[0], "amount": float(r[1]), "count": r[2]} for r in cur.fetchall()]
    return {"period": period, "total": float(total), "count": count, "by_category": by_cat}


def related(user_id: str, node_id: str, depth: int = 1, limit: int = 20) -> dict:
    """Permission-aware neighbours in the public graph and the user's personal graph."""
    node_id = (node_id or "").strip()
    if not node_id:
        raise ValueError("node_id 不能为空")
    depth = max(1, min(int(depth), 2))
    limit = max(1, min(int(limit), 50))
    seen = {node_id}
    frontier = [node_id]
    found: list[dict] = []

    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            for level in range(1, depth + 1):
                if not frontier or len(found) >= limit:
                    break
                cur.execute(
                    """select src,dst,relation from mem.graph_edge
                       where visibility='public' and (src = any(%s) or dst = any(%s))""",
                    (frontier, frontier),
                )
                edges = cur.fetchall()
                cur.execute(
                    """select src,dst,relation from mem.edge
                       where user_id=%s and (src = any(%s) or dst = any(%s))""",
                    (user_id, frontier, frontier),
                )
                edges += cur.fetchall()
                nxt = []
                for src, dst, rel in edges:
                    other = dst if src in frontier else src
                    if not other or other in seen:
                        continue
                    seen.add(other)
                    nxt.append(other)
                    found.append({"id": other, "relation": rel, "depth": level,
                                  "scope": "personal" if other.startswith(("inbox/", "decisions/", "cloud/", "state/", "artifacts/")) else "public"})
                    if len(found) >= limit:
                        break
                frontier = nxt

            ids = [x["id"] for x in found]
            titles: dict[str, str] = {}
            kinds: dict[str, str] = {}
            if ids:
                cur.execute("select id,title,kind from mem.graph_node where id = any(%s)", (ids,))
                for nid, title, kind in cur.fetchall():
                    titles[nid] = title or ""
                    kinds[nid] = kind or ""
                cur.execute("select path,title from mem.doc where user_id=%s and path = any(%s)", (user_id, ids))
                for path, title in cur.fetchall():
                    titles[path] = title or ""
                    kinds[path] = "doc"
            for item in found:
                item["title"] = titles.get(item["id"], "")
                item["kind"] = kinds.get(item["id"], "")
    # 统一可见性：被 supersede 的旧条不许以「相关记忆」的身份回来（改口径后还在推旧口径）。
    found = [f for f in found if _visibility.is_visible(str(f.get("id") or ""))]
    return {"node_id": node_id, "depth": depth, "count": len(found), "related": found}


# ---------------- entitlements / access / quotas ----------------

ADMIN_USERS = {u.strip() for u in (os.getenv("MEM_ADMIN_USERS", "owner") or "").split(",") if u.strip()}


def _period_key(period: str) -> str:
    now = datetime.now()
    if period == "month":
        return now.strftime("%Y-%m")
    if period == "total":
        return "all"
    return now.strftime("%Y-%m-%d")


def _plan_of(cur, user_id: str) -> tuple[str, str | None]:
    cur.execute(
        """select plan, period_end from mem.subscription
           where user_id=%s and status='active'
             and (period_end is null or period_end > now())
           order by period_end desc nulls first limit 1""",
        (user_id,),
    )
    row = cur.fetchone()
    if row:
        return row[0], row[1].isoformat() if row[1] else None
    return "free", None


def entitlements(user_id: str) -> dict:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            plan, expires = _plan_of(cur, user_id)
            cur.execute("select capability, limit_value, period from mem.plan_entitlement where plan=%s", (plan,))
            caps: dict[str, object] = {}
            for capability, limit, period in cur.fetchall():
                if limit is None:
                    caps[capability] = {"allowed": True, "limit": None, "used": None, "period": period}
                    continue
                pk = _period_key(period)
                cur.execute(
                    "select used from mem.usage_counter where user_id=%s and metric=%s and period=%s",
                    (user_id, capability, pk),
                )
                used = cur.fetchone()
                caps[capability] = {"allowed": (used[0] if used else 0) < limit,
                                    "limit": limit, "used": used[0] if used else 0, "period": period}
    return {"user_id": user_id, "plan": plan, "expires_at": expires, "capabilities": caps}


def check_access(user_id: str, content_type: str, content_id: str, region: str = "") -> dict:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            plan, expires = _plan_of(cur, user_id)
            cur.execute(
                """select visibility, region, paywall, teaser, owner_user_id
                   from mem.content_policy where content_type=%s and content_id=%s""",
                (content_type, content_id),
            )
            row = cur.fetchone()
            policy = {
                "visibility": row[0] if row else "public",
                "region": (region or (row[1] if row else "cn")),
                "paywall": row[2] if row else "free",
                "teaser": row[3] if row else "title",
                "owner_user_id": row[4] if row else None,
            }
            cur.execute(
                """select grant_type from mem.content_grant
                   where user_id=%s and content_type=%s and content_id=%s
                     and (expires_at is null or expires_at > now())""",
                (user_id, content_type, content_id),
            )
            grant = cur.fetchone()
            cur.execute("select capability, limit_value from mem.plan_entitlement where plan=%s", (plan,))
            caps = {r[0]: r[1] for r in cur.fetchall()}

    if user_id in ADMIN_USERS:
        return {"allowed": True, "reason": "admin", "plan": plan, "policy": policy}
    if grant:
        if grant[0] == "deny":
            return {"allowed": False, "reason": "denied", "plan": plan, "policy": policy}
        return {"allowed": True, "reason": "grant", "plan": plan, "policy": policy}
    if policy["owner_user_id"] and policy["owner_user_id"] == user_id:
        return {"allowed": True, "reason": "owner", "plan": plan, "policy": policy}
    if policy["visibility"] == "denied":
        return {"allowed": False, "reason": "denied", "plan": plan, "policy": policy}
    if policy["visibility"] == "login" and user_id:
        return {"allowed": True, "reason": "login", "plan": plan, "policy": policy}
    if policy["visibility"] == "vip" and plan not in {"vip", "team"}:
        return {"allowed": False, "reason": "vip_required", "plan": plan, "policy": policy,
                "upgrade": {"plan": "vip"}}
    if policy["region"] == "overseas" and not caps.get("read_overseas"):
        return {"allowed": False, "reason": "vip_required", "plan": plan, "policy": policy,
                "upgrade": {"plan": "vip"}}
    if policy["paywall"] == "vip" and plan not in {"vip", "team"}:
        return {"allowed": False, "reason": "vip_required", "plan": plan, "policy": policy,
                "upgrade": {"plan": "vip"}}
    needs_vip = policy["visibility"] == "vip" or policy["paywall"] == "vip" or policy["region"] == "overseas"
    return {"allowed": True, "reason": "vip" if needs_vip else "public", "plan": plan, "policy": policy}


def consume(user_id: str, metric: str, amount: int = 1) -> dict:
    amount = max(1, min(int(amount), 100))
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            plan, _ = _plan_of(cur, user_id)
            cur.execute(
                "select limit_value, period from mem.plan_entitlement where plan=%s and capability=%s",
                (plan, metric),
            )
            row = cur.fetchone()
            if not row:
                return {"allowed": True, "metric": metric, "plan": plan, "limit": None, "used": None}
            limit, period = row
            pk = _period_key(period)
            if limit is None:
                cur.execute(
                    """insert into mem.usage_counter (user_id,metric,period,used) values (%s,%s,%s,%s)
                       on conflict (user_id,metric,period) do update set used=mem.usage_counter.used+%s
                       returning used""",
                    (user_id, metric, pk, amount, amount),
                )
                used = cur.fetchone()[0]
                c.commit()
                return {"allowed": True, "metric": metric, "plan": plan, "limit": None, "used": used}
            cur.execute(
                """insert into mem.usage_counter (user_id,metric,period,used) values (%s,%s,%s,%s)
                   on conflict (user_id,metric,period) do update set used=mem.usage_counter.used+%s
                   returning used""",
                (user_id, metric, pk, amount, amount),
            )
            used = cur.fetchone()[0]
            if used > limit:
                cur.execute(
                    "update mem.usage_counter set used=used-%s where user_id=%s and metric=%s and period=%s",
                    (amount, user_id, metric, pk),
                )
                c.commit()
                return {"allowed": False, "reason": "quota_exceeded", "metric": metric,
                        "plan": plan, "limit": limit, "used": used - amount, "period": pk,
                        "upgrade": {"plan": "vip"}}
            c.commit()
            return {"allowed": True, "metric": metric, "plan": plan, "limit": limit,
                    "used": used, "remaining": limit - used, "period": pk}


# ---------------- unified queue ----------------

QUEUE_STATUS = {"pending", "processing", "ready", "failed", "skipped", "canceled"}


def queue_add(user_id: str, source_type: str, source_id: str, title: str,
              summary: str = "", priority: int = 2, tags: list[str] | None = None) -> dict:
    source_type = (source_type or "").strip()
    source_id = (source_id or "").strip()
    if not source_type or not source_id:
        raise ValueError("source_type 和 source_id 不能为空")
    iid = _new_id()
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """insert into mem.queue_item
                   (id,user_id,source_type,source_id,title,summary,status,priority,score,tags,meta)
                   values (%s,%s,%s,%s,%s,%s,'pending',%s,50,%s,'{}')
                   on conflict (user_id,source_type,source_id) do update
                     set title=excluded.title, summary=excluded.summary,
                         priority=excluded.priority, tags=excluded.tags, updated_at=now()
                   returning id""",
                (iid, user_id, source_type, source_id, title or "", summary or "",
                 max(1, min(int(priority), 5)), tags or []),
            )
            rid = cur.fetchone()[0]
        c.commit()
    return {"ok": True, "id": rid}


def queue_list(user_id: str, status: str = "", source_type: str = "", limit: int = 50) -> dict:
    limit = max(1, min(limit, 200))
    sql = """select id,source_type,source_id,title,status,priority,score,summary,created_at
             from mem.queue_item where user_id=%s"""
    args = [user_id]
    if status:
        sql += " and status=%s"; args.append(status)
    if source_type:
        sql += " and source_type=%s"; args.append(source_type)
    sql += " order by priority asc, score desc nulls last, created_at asc limit %s"
    args.append(limit)
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return {"count": len(rows), "items": [
        {"id": r[0], "source_type": r[1], "source_id": r[2], "title": r[3],
         "status": r[4], "priority": r[5], "score": float(r[6]) if r[6] is not None else None,
         "summary": r[7], "created_at": r[8].isoformat() if r[8] else None}
        for r in rows
    ]}


def queue_update(user_id: str, item_id: str, status: str = "", priority: int | None = None) -> dict:
    fields, args = [], []
    if status:
        if status not in QUEUE_STATUS:
            raise ValueError("非法状态")
        fields.append("status=%s"); args.append(status)
        if status in {"ready", "failed", "skipped", "canceled"}:
            fields.append("processed_at=now()")
    if priority is not None:
        fields.append("priority=%s"); args.append(max(1, min(int(priority), 5)))
    if not fields:
        raise ValueError("没有要更新的字段")
    fields.append("updated_at=now()")
    args += [item_id, user_id]
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(f"update mem.queue_item set {', '.join(fields)} where id=%s and user_id=%s", args)
            if cur.rowcount == 0:
                raise ValueError("队列项不存在")
        c.commit()
    return {"ok": True, "id": item_id}


def queue_stats(user_id: str) -> dict:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select status,count(*) from mem.queue_item where user_id=%s group by 1", (user_id,))
            by_status = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("select source_type,count(*) from mem.queue_item where user_id=%s group by 1", (user_id,))
            by_source = {r[0]: r[1] for r in cur.fetchall()}
    return {"user_id": user_id, "total": sum(by_status.values()),
            "by_status": by_status, "by_source": by_source}


# ---------------- prompt templates ----------------

def prompt_list(source_type: str = "") -> dict:
    sql = "select key,version,source_type,title,description,enabled from mem.prompt_template"
    args = []
    if source_type:
        sql += " where source_type=%s"; args.append(source_type)
    sql += " order by source_type,key,version desc"
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return {"count": len(rows), "prompts": [
        {"key": r[0], "version": r[1], "source_type": r[2], "title": r[3],
         "description": r[4], "enabled": r[5]} for r in rows
    ]}


def prompt_get(key: str, version: int | None = None) -> dict:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            if version:
                cur.execute("select key,version,source_type,title,description,template,output_schema,model,temperature from mem.prompt_template where key=%s and version=%s", (key, version))
            else:
                cur.execute("select key,version,source_type,title,description,template,output_schema,model,temperature from mem.prompt_template where key=%s order by version desc limit 1", (key,))
            row = cur.fetchone()
    if not row:
        raise ValueError("内容不能为空")
    return {"key": row[0], "version": row[1], "source_type": row[2], "title": row[3],
            "description": row[4], "template": row[5], "output_schema": row[6],
            "model": row[7], "temperature": float(row[8]) if row[8] is not None else None}


def prompt_upsert(key: str, source_type: str, title: str, description: str, template: str,
                  output_schema: dict | None = None, model: str = "", temperature: float = 0.2,
                  enabled: bool = True) -> dict:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select coalesce(max(version),0) from mem.prompt_template where key=%s", (key,))
            version = int(cur.fetchone()[0]) + 1
            if enabled:
                cur.execute("update mem.prompt_template set enabled=false where key=%s", (key,))
            cur.execute(
                """insert into mem.prompt_template
                   (key,version,source_type,title,description,template,output_schema,model,temperature,enabled)
                   values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                (key, version, source_type, title, description, template,
                 json.dumps(output_schema or {}, ensure_ascii=False), model or "",
                 temperature, enabled),
            )
        c.commit()
    return prompt_get(key, version)


# ---------------- conversation transcript ----------------

def _ms(value: str, default: int | None = None) -> int | None:
    text = (value or "").strip().lower()
    if not text:
        return default
    if text.isdigit():
        return int(text)
    now = datetime.now().astimezone()
    if text in {"today", "??"}:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)
    if text in {"yesterday", "??"}:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int((start.timestamp() - 86400) * 1000)
    m = re.fullmatch(r"(\d+)\s*([hdm])", text)
    if m:
        n = int(m.group(1))
        unit = {"h": 3600, "d": 86400, "m": 60}[m.group(2)]
        return int((now.timestamp() - n * unit) * 1000)
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return int(dt.timestamp() * 1000)
    except ValueError:
        return default


def conv_list(since: str = "24h", until: str = "", source: str = "", limit: int = 50) -> dict:
    start = _ms(since, 0) or 0
    end = _ms(until, None)
    limit = max(1, min(int(limit), 200))
    sql = """select c.id,c.source,c.title,c.updated_at,
                    c.message_count + (select count(*) from mem.live_event l
                       where l.conversation_id=c.id and l.reconciled_at is null) mc,
                    (select count(*) from mem.live_event l
                       where l.conversation_id=c.id and l.reconciled_at is null) unrec,
                    coalesce((select l.text from mem.live_event l
                       where l.conversation_id=c.id and l.reconciled_at is null and l.role='user'
                       order by l.created_at desc limit 1), c.snippet) last_user
             from mem.conv c where c.updated_at >= to_timestamp(%s)"""
    args: list = [start / 1000.0]
    if end:
        sql += " and c.updated_at <= to_timestamp(%s)"
        args.append(end / 1000.0)
    if source:
        sql += " and c.source=%s"
        args.append(source)
    sql += " order by c.updated_at desc limit %s"
    args.append(limit)
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    out = []
    for r in rows:
        updated = r[3].astimezone().isoformat() if r[3] else None
        out.append({"id": r[0], "source": r[1], "title": r[2] or "",
                    "updated_at": updated,
                    "last_synced_at": updated,
                    "message_count": r[4],
                    "unreconciled": r[5],
                    "sync_status": "live" if r[5] else "reconciled",
                    "last_user": (r[6] or "")[:300]})
    return {"count": len(out), "conversations": out,
            "reconcile_note": "unreconciled 是 live_event 里尚未回填到 conv_message 的增量消息；"
                              "message_count 只统计已对账部分，可能为 0 或偏小，读取/检索不丢。"}


def conv_get(conv_id: str, since: str = "", until: str = "", limit: int = 100,
             offset: int = 0, compact: bool = False, keyword: str = "",
             max_chars: int = 20000) -> dict:
    start = _ms(since, None)
    end = _ms(until, None)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    max_chars = max(200, min(int(max_chars), 100000))
    kw = (keyword or "").strip()
    conds = []
    args: list = [conv_id]
    if start:
        conds.append("created_at >= to_timestamp(%s)")
        args.append(start / 1000.0)
    if end:
        conds.append("created_at <= to_timestamp(%s)")
        args.append(end / 1000.0)
    if kw:
        conds.append("text ilike %s")
        args.append(_like(kw))
    extra = (" and " + " and ".join(conds)) if conds else ""
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select id,source,title,updated_at,message_count from mem.conv where id=%s", (conv_id,))
            meta = cur.fetchone()
            if not meta:
                raise ValueError("参数不合法")
            cur.execute(
                f"""select
                      (select count(*) from mem.conv_message where conversation_id=%s{extra})
                    + (select count(*) from mem.live_event
                       where conversation_id=%s and reconciled_at is null{extra})""",
                [conv_id, *args[1:], conv_id, *args[1:]],
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                f"""select seq,role,created_at,text,synced_at from (
                      select seq,role,created_at,text,synced_at,omni_id ord
                      from mem.conv_message where conversation_id=%s{extra}
                      union all
                      select seq,role,created_at,text,received_at as synced_at,id ord
                      from mem.live_event
                      where conversation_id=%s and reconciled_at is null{extra}
                    ) u order by seq asc, created_at asc, ord asc limit %s offset %s""",
                [conv_id, *args[1:], conv_id, *args[1:], limit, offset],
            )
            rows = cur.fetchall()
            cur.execute(
                """select
                     (select max(synced_at) from mem.conv_message where conversation_id=%s),
                     (select max(received_at) from mem.live_event
                        where conversation_id=%s and reconciled_at is null),
                     (select count(*) from mem.live_event
                        where conversation_id=%s and reconciled_at is null)""",
                (conv_id, conv_id, conv_id),
            )
            synced_seen, live_seen, unreconciled = cur.fetchone()
    stamps = [s for s in (synced_seen, live_seen, meta[3]) if s]
    last_synced = max(stamps).astimezone().isoformat() if stamps else None
    messages = []
    for r in rows:
        text = r[3] or ""
        at = r[2].astimezone().isoformat() if r[2] else None
        chars = len(text)
        shown = text
        if compact:
            shown = _snippet_around(text, kw, 140) if kw else _snippet(text, 320)
        else:
            shown = text[:max_chars]
        messages.append({
            "seq": r[0],
            "role": r[1],
            "at": at,
            "text": shown,
            "chars": chars,
            "truncated": chars > len(shown),
            "synced_at": r[4].astimezone().isoformat() if r[4] else None,
            "anchor": _anchor("mem.conv", conv_id, r[0], at),
        })
    return {"id": meta[0], "source": meta[1], "title": meta[2] or "",
            "updated_at": meta[3].astimezone().isoformat() if meta[3] else None,
            "last_synced_at": last_synced,
            "sync_status": "live" if unreconciled else "reconciled",
            "unreconciled": unreconciled,
            "message_count": meta[4] or 0,
            "reconcile": {
                "status": "live" if unreconciled else "reconciled",
                "unreconciled": unreconciled,
                "reconciled_message_count": meta[4] or 0,
                "note": ("unreconciled=%d 是 live_event 里尚未回填到 conv_message 的增量消息；"
                         "它们已计入 total/messages，读取和检索不丢。mem.conv.message_count "
                         "只统计已对账部分，所以可能为 0 或偏小，不代表会话是空的。"
                         % unreconciled),
            },
            "keyword": kw, "compact": bool(compact), "max_chars": max_chars,
            "total": total, "offset": offset, "returned": len(messages),
            "backend": "pg", "messages": messages}


def _conv_terms(keyword: str, cap: int = 12) -> list[str]:
    """会话内检索词：整串优先，再补 4-gram。

    为什么不是 2-gram：pg_trgm 的 GIN 索引至少要 3 个字符才能抽 trigram；
    2 字词只能全表扫（实测「三家」7s、「合参」11s）。4-gram 既能吃索引，
    又能覆盖「伤寒论三家合参笔记」里真正的「三家合参」。
    """
    raw = (keyword or "").strip()
    if not raw:
        return []
    out: list[str] = []
    for candidate in (raw, re.sub(r"\.(?:pdf|md|docx?|xlsx?|pptx?|txt)$", "", raw, flags=re.I)):
        if candidate and len(candidate) >= 2 and candidate not in out:
            out.append(candidate)
    for part in _TERM_SPLIT.split(raw):
        if len(part) >= 3 and part not in out:
            out.append(part)
    for run in _CJK_RUN.findall(raw):
        if len(run) <= 3:
            if len(run) == 3 and run not in out:
                out.append(run)
            continue
        for i in range(len(run) - 3):
            gram = run[i:i + 4]
            if gram not in out:
                out.append(gram)
    return out[:cap]


_SEMANTIC_GROUPS = (
    ("改成", "改为", "换成", "切换", "调整为", "改到", "改成"),
    ("决定", "拍板", "定了", "定稿", "结论", "采用", "选择", "最终"),
    ("停用", "废弃", "撤掉", "不要", "下线", "删除"),
    ("上线", "发布", "部署", "落地", "生效"),
    ("问题", "报错", "失败", "超时", "修复", "修好"),
)
_DECISION_RE = re.compile(r"(决定|拍板|定了|定稿|最终|结论|采用|就按|改成|改为|切换|调整为)")
_CODE_MARKERS = ("```", "select ", "insert ", "update ", "create table", "| ---",
                 "cms_", "curl ", "python ", "git ")


def _looks_like_code(low: str) -> bool:
    return sum(1 for marker in _CODE_MARKERS if marker in low) >= 2


def _char_cosine(a: str, b: str) -> float:
    """字符 2-gram 余弦：中文短查询的模糊相似，比纯词命中多一点「像不像」。"""
    def grams(text: str) -> dict[str, int]:
        t = re.sub(r"\s+", "", (text or "").lower())
        if len(t) < 2:
            return {t: 1} if t else {}
        out: dict[str, int] = {}
        for i in range(len(t) - 1):
            g = t[i:i + 2]
            out[g] = out.get(g, 0) + 1
        return out
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    dot = sum(v * gb.get(k, 0) for k, v in ga.items())
    na = math.sqrt(sum(v * v for v in ga.values()))
    nb = math.sqrt(sum(v * v for v in gb.values()))
    return dot / (na * nb) if na and nb else 0.0


def _semantic_profile(query: str) -> dict[str, Any]:
    q = (query or "").lower()
    groups = [g for g in _SEMANTIC_GROUPS if any(w.lower() in q for w in g)]
    entities = [e for e in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{2,}", query or "")]
    expanded = _expand_synonyms([query] if query else [])
    return {"groups": groups, "entities": list(dict.fromkeys(entities))[:8],
            "expanded": [t for t in expanded if t != query][:12],
            "decision_intent": any(g in groups for g in (_SEMANTIC_GROUPS[0], _SEMANTIC_GROUPS[1], _SEMANTIC_GROUPS[2])),
            "query_text": query}


def _semantic_score(text: str, profile: dict[str, Any], role: str = "") -> float:
    if os.getenv("MEM_SEMANTIC", "1").strip().lower() in ("0", "off", "false", "no"):
        return 0.0
    low = (text or "").lower()
    if not low:
        return 0.0
    score = 0.0
    for group in profile.get("groups") or []:
        if any(w.lower() in low for w in group):
            score += 0.30
    hits = sum(1 for t in profile.get("expanded") or [] if t.lower() in low)
    score += min(0.25, 0.06 * hits)
    score += 0.20 * _char_cosine(profile.get("query_text") or "", low)
    if profile.get("entities") and any(e.lower() in low for e in profile["entities"]):
        score += 0.25
    # 决策/变更意图：有「决定/改成/拍板」等结论句的优先；代码块降权。
    if profile.get("decision_intent"):
        if _DECISION_RE.search(low):
            score += 0.30
        # 关键：决策词要离实体近（「改成 cms_book」），别让一篇长表结构里
        # 恰好同时出现「改成」和 cms_book 就抢到语义分。
        for entity in profile.get("entities") or []:
            pos = low.find(entity.lower())
            if pos >= 0 and _DECISION_RE.search(
                    low[max(0, pos - 120):pos + len(entity) + 120]):
                score += 0.25
                break
        if _looks_like_code(low):
            score -= 0.25
        if role == "user" and any(w in low for w in ("改成", "改为", "切换", "决定")):
            score += 0.08
    return max(0.0, min(1.0, score))


def conv_search(keyword: str, conv_id: str = "", since: str = "", until: str = "",
                limit: int = 20, context: int = 1, user_id: str = "owner",
                max_chars: int = 400, max_total_chars: int = 24000) -> dict:
    """会话内关键词检索：返回 seq/role/at/上下文/片段，可直接定位到某条消息。

    P0（2026-11-19 WorkBuddy 首次接入）：之前只能 conv_get 整段翻页，5157 条消息
    找关键决策要导出 3MB+ 再本地 grep。这里按 user_id 隔离，不跨用户。
    """
    kw = (keyword or "").strip()
    if not kw:
        raise ValueError("keyword 不能为空")
    limit = max(1, min(int(limit), 50))
    context = max(0, min(int(context), 3))
    max_chars = max(80, min(int(max_chars), 2000))
    max_total_chars = max(4000, min(int(max_total_chars), 60000))
    start = _ms(since, None)
    end = _ms(until, None)
    terms = _conv_terms(kw)
    if not terms:
        return {"keyword": kw, "count": 0, "matches": [], "conversations": [], "hint": "keyword 不能为空"}

    base_where = ["c.user_id = %s"]
    base_args: list[Any] = [user_id]
    if conv_id:
        base_where.append("m.conversation_id = %s")
        base_args.append(conv_id)
    if start:
        base_where.append("m.created_at >= to_timestamp(%s)")
        base_args.append(start / 1000.0)
    if end:
        base_where.append("m.created_at <= to_timestamp(%s)")
        base_args.append(end / 1000.0)

    # 每个词单独查（单 pattern 才能吃到 trgm GIN 索引；OR 多词会让 PG 退化成 seq scan，
    # 实测 26s → 单 pattern 0.1s）。合并去重后在 Python 里按命中词数精排。
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            for term in terms:
                where = [*base_where, "m.text ilike %s"]
                args = [*base_args, _like(term), 200]
                cur.execute(
                    f"""
                    select m.conversation_id, m.seq, m.role, m.created_at, m.text,
                           m.synced_at, c.source, c.title, c.updated_at, c.message_count
                      from (
                            select conversation_id, seq, role, created_at, text, synced_at
                              from mem.conv_message
                            union all
                            select conversation_id, seq, role, created_at, text, received_at as synced_at
                              from mem.live_event where reconciled_at is null
                           ) m
                      join mem.conv c on c.id = m.conversation_id
                     where {' and '.join(where)}
                     order by m.created_at desc
                     limit %s
                    """,
                    args,
                )
                for row in cur.fetchall():
                    key = (row[0], row[1], row[3], (row[4] or "")[:80])
                    seen.setdefault(key, {"row": row, "terms": set()})["terms"].add(term)
                # 够用了就停：长 query 会生成多个 4-gram，没必要每个都远程扫一遍。
                if len(seen) >= max(limit * 4, 24):
                    break

    part_terms = {p for p in _TERM_SPLIT.split(kw) if p}
    profile = _semantic_profile(kw)
    matches: list[dict[str, Any]] = []
    for hit in seen.values():
        row, matched = hit["row"], sorted(hit["terms"])
        text = row[4] or ""
        low = text.lower()
        matched = [t for t in matched if t.lower() in low]
        if not matched:
            continue
        at = row[3].astimezone().isoformat() if row[3] else None
        conv = row[0]
        weight = sum(3 if t == kw else (2 if t in part_terms else 1) for t in matched)
        literal = min(1.0, weight / max(1.0, 3.0 * len(matched)))
        semantic = _semantic_score(text, profile, role=row[2])
        score = 0.6 * literal + 0.4 * semantic
        matches.append({
            "scope": "personal",
            "type": "message",
            "conversation_id": conv,
            "source": row[6],
            "title": row[7] or "",
            "updated_at": row[8].astimezone().isoformat() if row[8] else None,
            "last_synced_at": row[8].astimezone().isoformat() if row[8] else None,
            "message_count": row[9],
            "seq": row[1],
            "role": row[2],
            "at": at,
            "anchor": _anchor("mem.conv", conv, row[1], at),
            "snippet": _snippet_around(text, kw if kw.lower() in low else matched[0],
                                       max(40, max_chars // 2))[:max_chars],
            "matched_terms": matched[:8],
            "literal_score": round(literal, 4),
            "semantic_score": round(semantic, 4),
            "score": round(score, 4),
            "synced_at": row[5].astimezone().isoformat() if row[5] else None,
        })
    matches.sort(key=lambda m: (m["score"], m["updated_at"] or "", m["seq"]), reverse=True)

    # 两段式：先取 2 倍候选，补上下文后用「邻近消息」再算一次语义分，再定最终 limit。
    candidates = matches[:max(limit * 2, limit)]
    if context > 0:
        _attach_contexts(candidates, context, max_chars=max_chars)
        for match in candidates:
            ctx_text = " ".join(c.get("text", "") for c in match.get("context") or [])
            if ctx_text:
                ctx_sem = _semantic_score(ctx_text, profile)
                match["semantic_score"] = round(
                    min(1.0, match["semantic_score"] * 0.8 + ctx_sem * 0.2), 4
                )
                match["score"] = round(
                    0.6 * match["literal_score"] + 0.4 * match["semantic_score"], 4
                )
        candidates.sort(key=lambda m: (m["score"], m["updated_at"] or "", m["seq"]), reverse=True)
    matches = candidates[:limit]

    # 输出预算：context[] 和 snippet 都按 max_chars 截，总字符超 max_total_chars 时
    # 从后往前丢上下文（保留命中本身和 anchor），并显式标记 truncated。
    used = 0
    output_truncated = False
    for match in matches:
        keep = []
        for item in match.get("context") or []:
            if used + len(item.get("text", "")) > max_total_chars:
                output_truncated = True
                break
            keep.append(item)
            used += len(item.get("text", ""))
        if len(keep) < len(match.get("context") or []):
            output_truncated = True
        match["context"] = keep
        used += len(match.get("snippet", ""))
    output_chars = used

    conversations: dict[str, dict[str, Any]] = {}
    for match in matches:
        item = conversations.setdefault(match["conversation_id"], {
            "id": match["conversation_id"],
            "source": match["source"],
            "title": match["title"],
            "updated_at": match["updated_at"],
            "last_synced_at": match["last_synced_at"],
            "message_count": match["message_count"],
            "hits": 0,
            "best_score": 0.0,
        })
        item["hits"] += 1
        item["best_score"] = max(item["best_score"], match["score"])
    return {
        "keyword": kw,
        "count": len(matches),
        "matches": matches,
        "conversations": list(conversations.values()),
        "max_chars": max_chars,
        "max_total_chars": max_total_chars,
        "output_chars": output_chars,
        "truncated": output_truncated,
        "scoring": {
            "mode": "literal+semantic",
            "literal_weight": 0.6,
            "semantic_weight": 0.4,
            "semantic_mode": "intent + synonym + char-ngram + neighbor-context (no embedding channel)",
            "switch": "MEM_SEMANTIC=0 回退纯字面",
        },
        "hint": "拿 match.anchor / conversation_id+seq 就能回溯；要更多上下文用 conv_get(conv_id, compact=True)。",
    }


def _attach_contexts(matches: list[dict[str, Any]], radius: int = 1,
                     max_chars: int = 400) -> None:
    """一次把同一会话的多个命中窗口查完，避免 5 条命中发 5 次远程查询。"""
    groups: dict[str, set[int]] = {}
    for match in matches:
        seq = int(match.get("seq") or 0)
        if seq <= 0:
            continue
        groups.setdefault(match["conversation_id"], set()).update(
            range(seq - radius, seq + radius + 1)
        )
    buckets: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for conv_id, seqs in groups.items():
        wanted = sorted(s for s in seqs if s > 0)
        if not wanted:
            continue
        with _conn(MEM_DSN) as c:
            with c.cursor() as cur:
                cur.execute(
                    """select seq, role, created_at, text, synced_at from (
                          select seq, role, created_at, text, synced_at from mem.conv_message
                           where conversation_id=%s and seq = any(%s)
                          union all
                          select seq, role, created_at, text, received_at as synced_at
                            from mem.live_event
                           where conversation_id=%s and reconciled_at is null and seq = any(%s)
                        ) u order by seq asc, created_at asc limit 60""",
                    (conv_id, wanted, conv_id, wanted),
                )
                rows = cur.fetchall()
        bucket: dict[int, list[dict[str, Any]]] = {}
        seen: set[tuple[int, str, str]] = set()
        for row in rows:
            # live_event 与 conv_message 可能有同一条消息的两份副本；按 seq+role+正文去重。
            key = (row[0], row[1], (row[3] or "")[:120])
            if key in seen:
                continue
            seen.add(key)
            at = row[2].astimezone().isoformat() if row[2] else None
            bucket.setdefault(row[0], []).append({
                "seq": row[0], "role": row[1], "at": at,
                "text": _snippet(row[3], max_chars),
                "anchor": _anchor("mem.conv", conv_id, row[0], at),
                "synced_at": row[4].astimezone().isoformat() if row[4] else None,
            })
        buckets[conv_id] = bucket
    for match in matches:
        seq = int(match.get("seq") or 0)
        bucket = buckets.get(match["conversation_id"], {})
        context = [
            item
            for s in range(seq - radius, seq + radius + 1)
            for item in bucket.get(s, [])
        ]
        # 同一 seq 可能有多角色副本；只保留前后 2*radius+1 条，避免输出膨胀。
        match["context"] = context[: 2 * radius + 1]



# ---------------- live turn capture ----------------

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def ingest_event(source: str, conversation_id: str, role: str, text: str,
                 title: str = "", seq: int | None = None, created_at: str = "",
                 event_id: str = "", meta: dict | None = None) -> dict:
    """Insert one turn boundary event immediately; safe to call on every prompt/answer."""
    source = (source or "unknown").strip()[:64]
    conversation_id = (conversation_id or "").strip()
    if not conversation_id:
        raise ValueError("conversation_id 不能为空")
    role = (role or "user").strip().lower()
    if role not in {"user", "assistant", "system", "tool"}:
        raise ValueError("role ??? user/assistant/system/tool")
    text = (text or "").replace("\x00", "")
    if not text.strip():
        raise ValueError("text 不能为空")
    stamp = _ms(created_at, None)
    created = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc) if stamp else datetime.now(timezone.utc)
    event_id = (event_id or "").strip() or _digest(
        f"{source}|{conversation_id}|{role}|{created.isoformat()}|{text}"
    )
    content_hash = _digest(text)
    snippet = text.strip().replace("\n", " ")[:300]
    conv_hash = _digest(f"{title}|{role}|{snippet}")

    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select id from mem.live_event where event_id=%s", (event_id,))
            if cur.fetchone():
                cur.execute(
                    """select coalesce(c.message_count,0) +
                              (select count(*) from mem.live_event l
                               where l.conversation_id=c.id and l.reconciled_at is null)
                       from mem.conv c where c.id=%s""",
                    (conversation_id,),
                )
                row = cur.fetchone()
                return {"ok": True, "duplicate": True, "event_id": event_id,
                        "conversation_id": conversation_id,
                        "message_count": row[0] if row else 0}

            if seq is None:
                cur.execute(
                    """select greatest(
                         coalesce((select max(seq) from mem.conv_message where conversation_id=%s),0),
                         coalesce((select max(seq) from mem.live_event where conversation_id=%s),0)
                       ) + 1""",
                    (conversation_id, conversation_id),
                )
                seq = int(cur.fetchone()[0])
            else:
                seq = int(seq)

            cur.execute(
                """insert into mem.live_event
                   (event_id,source,conversation_id,seq,role,text,content_hash,created_at,meta)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (event_id, source, conversation_id, seq, role, text, content_hash,
                 created, json.dumps(meta or {}, ensure_ascii=False)),
            )
            cur.execute(
                """insert into mem.conv
                   (id,source,external_id,title,project,cwd,model,created_at,updated_at,
                    message_count,snippet,hash)
                   values (%s,%s,%s,%s,'','','',%s,%s,0,%s,%s)
                   on conflict (id) do update
                     set source=excluded.source,
                         title=coalesce(nullif(excluded.title,''), mem.conv.title),
                         updated_at=greatest(coalesce(mem.conv.updated_at, excluded.updated_at), excluded.updated_at),
                         snippet=case when %s='user' then excluded.snippet else mem.conv.snippet end,
                         hash=excluded.hash""",
                (conversation_id, source, conversation_id, title, created, created,
                 snippet, conv_hash, role),
            )
            cur.execute(
                """select coalesce(c.message_count,0) +
                          (select count(*) from mem.live_event l
                           where l.conversation_id=c.id and l.reconciled_at is null)
                   from mem.conv c where c.id=%s""",
                (conversation_id,),
            )
            count = int(cur.fetchone()[0])
        c.commit()
    return {"ok": True, "duplicate": False, "event_id": event_id,
            "conversation_id": conversation_id, "seq": seq,
            "source": source, "role": role, "message_count": count,
            "received_at": datetime.now(timezone.utc).astimezone().isoformat()}


def _gate_elapsed_ms(t0: float) -> int:
    return int(round((time.monotonic() - t0) * 1000))


# ---- gate 召回：分层 / 配额 / 权重（2026-11-15） ----
# 问题：state/* 全量状态文件与两个会话摘要吃掉 52% 的注入位，
#       新写的 cloud/owner 摘要进不了 top3。
# 做法：① 分层——state/* 默认不进 gate 召回，query 明确命中其标题词才放 1 条；
#       ② 配额——每轮注入 doc ≤2、conv ≤1；
#       ③ 权重——排序 = trigram 分 × kind 权重（decision/artifact > event）× 时间衰减。

_STATE_PATH_PREFIXES = ("state/", "INDEX.md")
_KIND_WEIGHT = {
    "decision": 1.40,
    "artifact": 1.25,
    "metric": 1.15,
    "entity": 1.05,
    "event": 1.00,
    "conversation": 0.90,
}
_RECALL_DECAY_HALF_LIFE_DAYS = 14.0
_RECALL_DECAY_FLOOR = 0.6
_GATE_DOC_QUOTA = 2
_GATE_CONV_QUOTA = 1
_GATE_CANDIDATES_PER_TERM = 12


def _is_state_doc(r: dict) -> bool:
    return r.get("source") == "mem.doc" and str(r.get("id") or "").startswith(_STATE_PATH_PREFIXES)


def _title_hit(r: dict, query: str) -> bool:
    """标题的 3 字窗口出现在 query 里，才算「query 明确命中标题词」。"""
    title = (r.get("title") or "").strip().lower()
    ql = (query or "").lower()
    if not title or not ql:
        return False
    if title in ql:
        return True
    return any(title[i:i + 3] in ql for i in range(max(0, len(title) - 2)))


def _recall_decay(r: dict, now: datetime) -> float:
    raw = r.get("date") or ""
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    except Exception:
        return _RECALL_DECAY_FLOOR
    return _RECALL_DECAY_FLOOR + (1.0 - _RECALL_DECAY_FLOOR) * (
        0.5 ** (age_days / _RECALL_DECAY_HALF_LIFE_DAYS)
    )


def _df_weight(df: float) -> float:
    """IDF 权重：df 越小越重，但**不再把稀有词清零**。

    旧写法对 df<3 给 1e-6：本意是丢掉「么解/决的」这类切词噪声，实际却把
    **最有区分度的词**（路径、标识符、插件名、错误串）一起清零了——2026-11-19
    排查「自动注入完全没关系」时实测：查询里 `df=1` 的词权重 1e-6，只剩
    「内容/自动/注入」这类高频词在决定召回，于是召回的全是无关会话/文档。
    现在按标准 IDF 平滑：df=1 → 1.0，df=2 → 0.59，df=100 → 0.18。
    """
    return 1.0 / (1.0 + math.log(max(1.0, float(df))))


def _recall_score(r: dict, now: datetime) -> float:
    base = float(r.get("score") or 0.0)
    return base * _KIND_WEIGHT.get(str(r.get("kind") or ""), 1.0) * _recall_decay(r, now)


def select_recall(query: str, user_id: str, limit: int = 5,
                  conversation_id: str = "", include_pending: bool = False) -> list[dict]:
    """gate 的召回选择：宽召回（每个检索词各查一次）→ 分层 → 加权 → 配额。"""
    q = (query or "").strip()
    if not q:
        return []
    # 太短/太泛的输入（「继续」「好的」）没有检索价值：以前它会命中一条高分长文，
    # 然后被塞进注入 → 用户看到的就是一条毫不相干的记录。宁可这轮不注入。
    if len(re.sub(r"\s+", "", q)) < 6:
        return []
    limit = max(1, min(int(limit), 10))
    terms = [t for t in re.split(r"[\s,，。;；、/|]+", q) if len(t) >= 2][:4]
    seen: dict[tuple, dict] = {}
    for term in [q, *terms]:
        for r in search_personal(term, user_id, _GATE_CANDIDATES_PER_TERM, include_pending):
            key = (r.get("source"), r.get("id"))
            if conversation_id and r.get("id") == conversation_id:
                continue
            snippet = (r.get("snippet") or "").lstrip()
            if snippet.startswith("You are the ") or snippet.startswith("You are a "):
                continue
            prev = seen.get(key)
            if prev is None or float(r.get("score") or 0) > float(prev.get("score") or 0):
                seen[key] = r

    now = datetime.now(timezone.utc)
    ranked: list[dict] = []
    for r in seen.values():
        item = dict(r)
        item["recall_score"] = round(_recall_score(item, now), 6)
        ranked.append(item)
    ranked.sort(key=lambda r: r["recall_score"], reverse=True)

    # 相关度下限（2026-11-19 加）：以前不管分数多低都塞满 3 条，于是 0.3 分的
    # 无关会话也进注入，用户看到的就是「完全没关系」。现在按「绝对下限 + 相对最佳
    # 分数的比例」双重过滤：一条都不够就返回空，让 gate 输出「本轮没有相关记忆」。
    if ranked:
        try:
            abs_floor = float(os.getenv("MEM_GATE_MIN_SCORE", "2.0") or 2.0)
        except (TypeError, ValueError):
            abs_floor = 2.0
        try:
            ratio = float(os.getenv("MEM_GATE_MIN_RATIO", "0.22") or 0.22)
        except (TypeError, ValueError):
            ratio = 0.22
        best = float(ranked[0]["recall_score"])
        floor = max(abs_floor, best * ratio)
        ranked = [r for r in ranked if float(r["recall_score"]) >= floor]

    state_hits = [r for r in ranked if _is_state_doc(r) and _title_hit(r, q)]
    normal = [r for r in ranked if not _is_state_doc(r)]

    picked: list[dict] = []
    n_doc = n_conv = 0
    for r in normal:
        if r.get("source") == "mem.doc":
            if n_doc >= _GATE_DOC_QUOTA:
                continue
            n_doc += 1
        else:
            if n_conv >= _GATE_CONV_QUOTA:
                continue
            n_conv += 1
        picked.append(r)
        if len(picked) >= limit:
            return picked
    # 状态文件是最后手段：只在 query 明确命中标题词时补 1 条（不进 top3 的前两位）
    if state_hits and len(picked) < limit:
        picked.append(state_hits[0])
    return picked


def _gate_log(user_id: str, source: str, conversation_id: str, turn: int | None,
              query: str, merged: list[dict], injected_chars: int, latency_ms: int,
              ok: bool = True, error: str = "", stage_ms: dict | None = None,
              slow: bool = False) -> int:
    """Best-effort recall log row; never raise into the gate path.

    2026-11-18：新增 stage_ms（阶段耗时）与 slow（>=9500ms 危险区），并返回行 id——
    端点层拿到 id 后可以补一条「客户端已断开」（client_gone），把
    「服务端跑完了但客户端早走了」这种同一次调用两边口径不一致的情况记清楚。
    """
    hit_ids = [str(r.get("id")) for r in merged if r.get("id")]
    hit_scores = [round(float(r["score"]), 6) for r in merged
                  if isinstance(r.get("score"), (int, float))]
    try:
        with _conn(MEM_DSN) as c:
            with c.cursor() as cur:
                cur.execute(
                    """insert into mem.gate_log
                       (user_id,source,conversation_id,turn,query,query_hash,hit_ids,
                        hit_scores,count,injected_chars,latency_ms,ok,error,stage_ms,slow)
                       values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,%s)
                       returning id""",
                    (user_id, (source or "gate")[:64], conversation_id or "", turn,
                     (query or "")[:4000], _digest(query or ""),
                     json.dumps(hit_ids, ensure_ascii=False),
                     json.dumps(hit_scores),
                     len(merged), int(injected_chars), int(latency_ms),
                     bool(ok), (error or "")[:500],
                     json.dumps(stage_ms or {}, ensure_ascii=False), bool(slow)),
                )
                row = cur.fetchone()
            c.commit()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def mark_gate_client_gone(log_id: int) -> bool:
    """把一行 gate_log 标成「客户端在服务端跑完前已断开」。

    客户端视角这是失败（模型没拿到上下文），服务端视角 ok 仍为 true（它确实跑完了）。
    """
    if not log_id:
        return False
    try:
        with _conn(MEM_DSN) as c:
            with c.cursor() as cur:
                cur.execute("update mem.gate_log set client_gone = true where id = %s", (log_id,))
            c.commit()
        return True
    except Exception:
        return False


# 客户端 10s 超时的危险区阈值：>= 这个值的请求单独标 slow，便于看趋势。
GATE_SLOW_MS = 9500


def gate_stats(hours: int = 24) -> dict:
    """gate 健康聚合（POST /gate/stats）：服务端与客户端两个视角都给。

    - ok/failed：服务端视角（failed = 抛异常的）
    - timeout：**客户端视角**失败 = client_gone（服务端跑完了但客户端早走了）
    - slow_ge_9500：latency_ms >= 9500 的危险区条数（含成功与失败）
    - stage：各阶段耗时的 p50/p95（毫秒），用于定位 5.5s 花在哪
    """
    hours = max(1, min(int(hours or 24), 24 * 7))
    out: dict[str, Any] = {"window_hours": hours, "total": 0, "ok": 0, "failed": 0,
                           "timeout": 0, "slow_ge_9500": 0,
                           "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None,
                           "stage": {}}
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """select count(*),
                          count(*) filter (where ok),
                          count(*) filter (where not ok),
                          count(*) filter (where client_gone),
                          count(*) filter (where latency_ms >= %(slow)s),
                          percentile_cont(0.5) within group (order by latency_ms),
                          percentile_cont(0.95) within group (order by latency_ms),
                          percentile_cont(0.99) within group (order by latency_ms),
                          max(latency_ms)
                   from mem.gate_log
                   where created_at >= now() - (%(h)s || ' hours')::interval""",
                {"slow": GATE_SLOW_MS, "h": hours},
            )
            row = cur.fetchone()
            out.update({
                "total": int(row[0]), "ok": int(row[1]), "failed": int(row[2]),
                "timeout": int(row[3]), "slow_ge_9500": int(row[4]),
                "p50_ms": round(float(row[5])) if row[5] is not None else None,
                "p95_ms": round(float(row[6])) if row[6] is not None else None,
                "p99_ms": round(float(row[7])) if row[7] is not None else None,
                "max_ms": int(row[8]) if row[8] is not None else None,
            })
            # 阶段耗时：jsonb 展开后按 key 聚合
            cur.execute(
                """select kv.key,
                          count(*),
                          percentile_cont(0.5) within group (order by (kv.value #>> '{}')::int),
                          percentile_cont(0.95) within group (order by (kv.value #>> '{}')::int)
                   from mem.gate_log g, jsonb_each(g.stage_ms) kv
                   where g.created_at >= now() - (%(h)s || ' hours')::interval
                     and g.stage_ms is not null
                   group by kv.key order by kv.key""",
                {"h": hours},
            )
            for key, n, p50, p95 in cur.fetchall():
                out["stage"][key] = {"n": int(n), "p50_ms": round(float(p50)),
                                     "p95_ms": round(float(p95))}
    return out


_CONTEXT_MAX = 4000          # gate 上下文总预算（原 `[:4000]` 硬截断的那个数）
_TRUNC_MARK = "…（截断）"


def _render_block(idx: int, r: dict) -> str:
    title = (r.get("title") or "").strip()
    snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:300]
    kind = r.get("kind") or r.get("scope") or ""
    return f"{idx}. [{kind}] {title}\n   {snippet}"


def _build_context(merged: list[dict]) -> str:
    """按配额拼 gate 上下文（2026-11-17）：core 先占，召回在剩余预算里**按条目边界**取。

    为什么不能再用 `[:4000]` 从头硬截断：core 是 `merged.insert(0, …)` 插在队首的，
    硬截断会把召回段最后一条切成半句话——一条被砍断的记忆是废的，还会误导判断。
    宁可少给一条完整记忆，也不给一条断句。

    边界：① 配额按**渲染后**字符算（含标题与缩进，否则会虚超）；
         ② 连一条完整召回都放不下时，至少保留 1 条（截断它，并标 `…（截断）`），
            与 core 的「首条必留」同一个原则：不允许只剩 core。
    """
    head = ("## 相关记忆（供参考，未审不入）\n"
            "以下为检索到的历史结论，判断以当前事实为准。\n")
    cores = [r for r in merged if r.get("core")]
    recalls = [r for r in merged if not r.get("core")]
    used = len(head)
    blocks: list[str] = []
    idx = 1
    for r in cores:
        blk = _render_block(idx, r)
        if blocks and used + len(blk) + 1 > _CONTEXT_MAX:
            break
        blocks.append(blk)
        used += len(blk) + 1
        idx += 1
    recall_added = False
    for r in recalls:
        blk = _render_block(idx, r)
        if used + len(blk) + 1 <= _CONTEXT_MAX:
            blocks.append(blk)
            used += len(blk) + 1
            idx += 1
            recall_added = True
            continue
        if not recall_added:
            room = _CONTEXT_MAX - used - 1
            if room > len(_TRUNC_MARK) + 20:
                blocks.append(_render_block(idx, r)[: room - len(_TRUNC_MARK)] + _TRUNC_MARK)
        break
    return head + "\n".join(blocks)


def _rule_hit(rec: dict[str, Any], query: str, project: str = "") -> bool:
    q = (query or "").lower()
    blob = " ".join([
        str(rec.get("title") or ""), str(rec.get("summary") or ""),
        str(rec.get("project") or ""), " ".join(rec.get("topics") or []),
    ]).lower()
    if project and str(rec.get("project") or "").lower() == project.lower():
        return True
    terms = [t for t in re.split(r"[\s,，。;；、/|]+", q) if len(t) >= 2]
    return any(t in blob for t in terms)


def _gate_rule_items(user_id: str, query: str, project: str = "") -> list[dict[str, Any]]:
    """已批准的规则无条件注入；pinned/priority/项目命中决定顺序，最多 5 条。"""
    rows = [r for r in _rules.active(user_id) if r.get("kind") == "rule"]
    if not rows:
        return []
    for rec in rows:
        rec["_match"] = _rule_hit(rec, query, project)
    rows.sort(key=lambda r: (bool(r.get("pinned")), bool(r.get("_match")),
                             int(r.get("priority") or 0), str(r.get("ts") or "")),
              reverse=True)
    items = []
    for rec in rows[:5]:
        items.append({
            "scope": "personal", "type": "rule", "source": "mem.doc", "kind": "rule",
            "id": rec.get("path"), "title": rec.get("title") or "",
            "snippet": _snippet(rec.get("summary") or "", 900),
            "score": 100.0 + float(rec.get("priority") or 0),
            "priority": int(rec.get("priority") or 0),
            "pinned": bool(rec.get("pinned")),
            "project": rec.get("project") or "",
            "topics": rec.get("topics") or [],
            "match": bool(rec.get("_match")),
        })
    return items


def _render_rule_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = ["## 规则（最高优先级，先于检索结果；已由 AI 自审/人工批准）"]
    used = len(lines[0])
    for i, item in enumerate(items, 1):
        mark = " pin" if item.get("pinned") else ""
        line = (f"{i}. [rule/{item.get('priority')}{mark}] {item.get('title')}\n"
                f"   {_snippet(item.get('snippet') or '', 800)}")
        if used + len(line) > 2400 and i > 1:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def gate(query: str, user_id: str, source: str = "gate", conversation_id: str = "",
         role: str = "user", limit: int = 5, turn: int | None = None,
         include_pending: bool = False, log_sink: dict | None = None) -> dict:
    """Blocking pre-answer gate: log the prompt, retrieve context, return it.

    2026-11-18：按阶段计时（ingest / recall / core / assemble / log）写进 gate_log.stage_ms，
    端点层可据此补 client_gone；log_sink 用于把 gate_log 行 id 传回端点（不改进 HTTP 响应体）。
    """
    t0 = time.monotonic()
    stages: dict[str, int] = {}

    def _lap(name: str, since: float) -> float:
        stages[name] = _gate_elapsed_ms(since)
        return time.monotonic()

    q = (query or "").strip()
    merged: list[dict] = []
    context = ""
    lap = t0
    try:
        if not q:
            raise ValueError("query 不能为空")
        limit = max(1, min(int(limit), 10))
        trace = None
        if conversation_id:
            event = ingest_event(source, conversation_id, role, q, title="", meta={"gate": True})
            trace = event.get("event_id")
        lap = _lap("ingest", lap)
        merged = select_recall(q, user_id, limit, conversation_id, include_pending)
        lap = _lap("recall", lap)
        # P5 core 常驻（零 DDL）：不管检索有没有命中，core 条目无条件进上下文。
        # 2026-11-17 加字符预算：先按 last_hit 倒序（最新的优先占预算）→ budget() 截断
        # （单条 300 字、累计 2000 字、首条必留）→ **只对真正进了上下文的 id 做 touch()**。
        # 没进上下文却记命中，会让 LRU 淘汰失真，等于给没被用过的记忆续命。
        try:
            active, _hits = _core._load()
            if active and os.environ.get("MEM_CORE", "off").strip().lower() in (
                    "1", "on", "true", "yes"):
                ordered = sorted(active, key=lambda d: _hits.get(d) or active.get(d) or "",
                                 reverse=True)[:_core.MAX_CORE]
                with _conn(MEM_DSN) as cc:
                    with cc.cursor() as cur2:
                        cur2.execute(
                            "select path, kind, title, body, updated_at from mem.doc "
                            "where user_id=%(uid)s and path = any(%(ids)s) "
                            "and path <> all(%(sup)s) limit %(lim)s",
                            _visibility.with_hidden(
                                {"uid": user_id, "ids": ordered, "lim": _core.MAX_CORE}),
                        )
                        core_rows = cur2.fetchall()
                by_id = {row[0]: row for row in core_rows}
                cands = [
                    {
                        "id": d,
                        "kind": by_id[d][1],
                        "title": by_id[d][2] or "",
                        "snippet": _snippet(by_id[d][3]),
                        "date": by_id[d][4].isoformat() if by_id[d][4] else None,
                        "text": (by_id[d][2] or "") + "\n" + _snippet(by_id[d][3]),
                    }
                    for d in ordered if d in by_id
                ]
                picked = _core.budget(cands)
                for r in reversed(picked):
                    merged.insert(0, {
                        "scope": "personal", "source": "mem.doc", "kind": r["kind"],
                        "id": r["id"], "title": r["title"], "snippet": r["snippet"],
                        "date": r["date"], "score": 1.0, "core": True,
                    })
                if picked:
                    _core.touch([r["id"] for r in picked])
        except Exception:
            pass  # core 注入失败不能把 gate 拖死
        lap = _lap("core", lap)
        # 项目开工必读 + 规则层：规则先于检索结果，pinned/priority 决定顺序。
        proj: dict[str, Any] = {"matched": False, "project": "", "reads": [], "block": ""}
        try:
            proj = _projects.match(q)
        except Exception:
            pass
        rule_items: list[dict[str, Any]] = []
        try:
            rule_items = _gate_rule_items(user_id, q, proj.get("project") or "")
            if rule_items:
                rule_ids = {item.get("id") for item in rule_items}
                merged = [r for r in merged if r.get("id") not in rule_ids]
        except Exception:
            rule_items = []
        context = _build_context(merged)
        rule_block = _render_rule_block(rule_items)
        if rule_block:
            context = rule_block + "\n\n" + context
        if proj.get("matched"):
            context = proj["block"] + "\n\n" + context
        lap = _lap("assemble", lap)
        latency = _gate_elapsed_ms(t0)
        logged = [*rule_items, *merged]
        log_id = _gate_log(user_id, source, conversation_id, turn, q, logged, len(context),
                           latency, stage_ms=stages, slow=latency >= GATE_SLOW_MS)
        stages["log"] = _gate_elapsed_ms(lap)
        if log_sink is not None:
            log_sink["id"] = log_id
        return {"ok": True, "trace_id": trace, "query": q, "count": len(merged),
                "rule_count": len(rule_items), "rules": rule_items,
                "results": merged, "context": context,
                "project_reads": {k: proj.get(k) for k in ("matched", "project", "hits", "reads")}}
    except Exception as e:
        latency = _gate_elapsed_ms(t0)
        log_id = _gate_log(user_id, source, conversation_id, turn, q, merged, len(context),
                           latency, ok=False, error=f"{type(e).__name__}: {e}",
                           stage_ms=stages, slow=latency >= GATE_SLOW_MS)
        if log_sink is not None:
            log_sink["id"] = log_id
        raise


# ---------------------------------------------------------------------------
# v1 review-gated（2026-11-16，零 DDL）：
#   remember 默认写 cloud/<user>/pending/...；检索默认排除 pending；
#   review 通过才把 path 从 pending/ 移回正式区，并同步 mem.edge.src（图谱别断链）。
# ---------------------------------------------------------------------------

_ALLOWED_KINDS = {"event", "decision", "artifact", "entity", "belief", "metric", "rule"}
# 出处检查（2026-11-17 扩形态）：只认 `session-xxx` / `文件:行号` / 命令原文的旧正则，
# 会把「带仓库相对路径」的正经出处判成「无出处」——守库人 26 条待审里 17 条被迫走 force，
# 闸门等于没有。所以按形态枚举，不再靠一套窄正则猜：
#   ① session id / uuid   ② 文件:行号 / 文件:行号-行号
#   ③ 仓库相对路径（含 `/` 且带扩展名：docs/... research/... memory-mcp/...）
#   ④ Windows 绝对路径  ⑤ commit <sha>  ⑥ pid=N
#   ⑦ 命令原文（git/curl/python/node/pnpm…）
# 另加 `source:` 显式声明口子：作者写 `source: <任意非空>` 一律算有出处——显式声明优先于正则猜。
_SOURCE_RE = re.compile(
    r"(session[:=_-]?[0-9a-z-]{6,}"                                   # session-xxx
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"                          # uuid 前段
    r"|\bcommit\s+[0-9a-f]{7,40}\b"                                   # commit <sha>
    r"|\bpid\s*[:=]\s*\d+"                                            # pid=1234
    r"|\b[A-Za-z]:\\[^\s，。；、)）」\"']+"                            # C:\... / D:\...
    r"|(?<![\w/])[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*\.(?:md|py|go|ps1|ts|tsx|js"
    r"|json|yaml|yml|sql|sh|txt|csv|log|diff|patch)(?::\d+(?:-\d+)?)?"  # 仓库相对路径 / 文件:行号
    r"|[0-9a-f]{40})"
    r"|\b(?:git|curl|python3?|node|pnpm|npm|pip|psql|pytest|go|dotnet|pwsh|powershell)\s+\S",
    re.I,
)
_SOURCE_DECL_RE = re.compile(r"source\s*[:：]\s*\S", re.I)


def _has_source(body: str) -> bool:
    """有出处：显式 `source:` 声明优先，其次按形态匹配。"""
    text = body or ""
    return bool(_SOURCE_DECL_RE.search(text) or _SOURCE_RE.search(text))


_SOURCE_HINT = (
    "无出处（出处检查未过）：正文里要带以下任一 —— "
    "① 仓库相对路径（docs/… 、research/… 、memory-mcp/…，含 / 且带扩展名）"
    "② 绝对路径（C:\\… / D:\\…）"
    "③ 文件:行号 或 文件:行号-行号 "
    "④ `commit <sha>` / `pid=N` / `session-<id>` "
    "⑤ 命令原文（以 git/curl/python/node/pnpm 等开头）"
    "⑥ 显式写一行 `source: <任意说明>`（作者声明优先，正则猜不到时用这条）"
)
# 冲突检测（2026-11-17 重写）：旧版按「关键词重叠」判，只要两条都出现 `df`/`R@10`
# 这类词就算冲突——今天 26 条待审里 2 条假冲突（a5651064 vs 519513e8 主题完全不同），
# 结果是真正需要人裁的 0 条，全被机器拦下等人。
# 新判据：必须**同 topic/同实体** + **两条各自方向明确且相反** + **都没写「撤回/修正」**。
_DIRECTION_PAIRS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"全作废", "作废", "废弃", "废弃不用", "撤掉"}),
     frozenset({"继续保留", "仍有效", "继续使用", "保留使用"})),
    (frozenset({"上涨", "上升"}), frozenset({"下跌", "下降"})),
    (frozenset({"判赢", "算赢", "判为赢"}),
     frozenset({"判否", "判为否", "不算赢"})),
    (frozenset({"已修好", "已修复", "修复完成"}),
     frozenset({"未修复", "待修", "没修好"})),
)
# 注意：`通过`/`有效`/`增加`/`保留`/`取消` 这类词在我们的技术文里是术语（「有效 gram」「通过审核」），
# 拿它们当方向词会造出假冲突——a5651064 的 `判否` 对 b2ceb918 的 `有效 gram` 就被误判过一次。
# 所以方向词只取「结论性、不可当术语用」的少数几个。
# 「撤回/修正」字样 = 作者在自我修正，直接判 supersede，不进冲突队列。
_SUPERSEDE_RE = re.compile(r"(撤回|修正|订正|推翻|取代|supersede)", re.I)
_REF_OTHER_RE = re.compile(
    r"(前一条|上一条|前述|上述结论|上述裁定|原始裁定|之前那条"
    r"|[0-9a-f]{8}(?![0-9a-f])"                          # 另一个 doc 的 8 位 id 前缀
    r"|[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-/]*\.(?:md|json))",
    re.I,
)


def _directions(text: str) -> dict[int, int]:
    """每条文档在每组方向词上取到哪一侧：{pair_index: +1/-1}；两侧都出现则该组作废。"""
    got: dict[int, int] = {}
    for pi, (pos, neg) in enumerate(_DIRECTION_PAIRS):
        has_pos = any(w in text for w in pos)
        has_neg = any(w in text for w in neg)
        if has_pos != has_neg:
            got[pi] = 1 if has_pos else -1
    return got


def _is_supersede(body: str, title: str) -> bool:
    """写了「撤回/修正」且指向另一条文档 → supersede（自我修正，不算冲突）。"""
    text = f"{title}\n{body}"
    return bool(_SUPERSEDE_RE.search(text) and _REF_OTHER_RE.search(text))


def _review_log(entry: dict) -> None:
    """打回/放行的记录落到 jsonl（不进库，零 DDL；可当审计线索）。"""
    try:
        path = _DF_CACHE_PATH.parent / "review_log.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _find_conflict(cur, user_id: str, path: str, body: str, title: str) -> dict | None:
    """同 topic/同实体里找「结论方向相反」的那条；返回 {type: conflict|supersede, ...}。

    只有三种结果：None（没候选 / 方向不相反 / 已自我修正）、supersede（作者撤回或修正了
    前一条）、conflict（方向确实相反，需要人裁）。旧版只比关键词重叠，会把同主题不同侧面的
    两条判成冲突——今天 2 条假冲突（a5651064 vs 519513e8，只共有 `df`/`R@10`）就是这么来的。
    """
    if _is_supersede(body, title):
        return {"type": "supersede", "path": path}
    mine = _directions(f"{title}\n{body}")
    if not mine:
        return None
    cur.execute(
        f"""
        select d.path, d.title, left(d.body, 400)
          from mem.edge e
          join mem.edge e2 on e2.dst = e.dst and e2.user_id = e.user_id and e2.src <> e.src
          join mem.doc  d  on d.path = e2.src and d.user_id = e2.user_id
         where e.src = %(src)s and e.user_id = %(uid)s
           and d.path not like '%%/pending/%%'
           and d.updated_at >= now() - interval '7 days'
           {_visibility.hidden_param('d.path')}
         order by d.updated_at desc
        """,
        _visibility.with_hidden({"src": path, "uid": user_id}),
    )
    for other_path, other_title, other_body in cur.fetchall():
        other_text = f"{other_title or ''}\n{other_body or ''}"
        if _is_supersede(other_body or "", other_title or ""):
            continue                       # 对方也声明了自我修正，不算冲突
        theirs = _directions(other_text)
        for pi, side in mine.items():
            if pi in theirs and theirs[pi] != side:
                return {"type": "conflict", "path": other_path,
                        "why": f"方向词相反（第 {pi + 1} 组）"}
    return None


def _referenced_doc(cur, user_id: str, body: str) -> str | None:
    """正文里引用到的另一条 doc（8 位 id 前缀唯一命中时才返回它的 path，猜不准就不猜）。"""
    ids = {m for m in re.findall(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])", body or "")}
    if not ids:
        return None
    cur.execute(
        "select path from mem.doc where user_id=%(uid)s and path like any(%(pats)s)"
        " and path not like '%%/rejected/%%' and path <> all(%(sup)s)",
        _visibility.with_hidden({"uid": user_id, "pats": ["%" + i + "%" for i in ids]}),
    )
    paths = [r[0] for r in cur.fetchall()]
    return paths[0] if len(paths) == 1 else None


def review(user_id: str, doc_id: str, decision: str = "pass", reason: str = "", actor: str = "user") -> dict:
    """审一条待审记忆。三项检查全过才放行；decision='force' 可跳过检查（需写 reason）。"""
    decision = (decision or "pass").strip().lower()
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                "select path, kind, title, body from mem.doc where user_id=%s and path=%s",
                (user_id, doc_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("找不到这条记忆：id 要用 remember 返回的原样字符串")
            path, kind, title, body = row[0], row[1], row[2] or "", row[3] or ""
            if "/pending/" not in path:
                return {"ok": False, "id": path, "decision": "skip", "reason": "不在待审区，无需 review"}

            problems: list[str] = []
            if kind not in _ALLOWED_KINDS:
                problems.append(f"kind 不在白名单（{kind}）：只收 {sorted(_ALLOWED_KINDS)}")
            if not _has_source(body):
                problems.append(_SOURCE_HINT)
            conflict = _find_conflict(cur, user_id, path, body, title)
            supersede_of = None
            if conflict and conflict.get("type") == "conflict":
                problems.append(
                    f"与已通过的结论方向相反：{conflict['path']}（{conflict.get('why', '')}）")
            elif conflict and conflict.get("type") == "supersede":
                # 作者自己写了「撤回/修正 + 指向前一条」→ 自我修正，不进冲突队列、不拦。
                supersede_of = conflict.get("path")

            forced = decision == "force"
            if problems and not forced:
                # 归档而不是留在 pending（2026-11-17）：reject 只写日志、文件仍留在 pending/，
                # 而 pending 计数按 `%/pending/%` 数 → 被判 reject 的条目永远计入积压，
                # pending 永远归不了零。挪到 `cloud/<user>/rejected/`（不删文件、可按 id 直取），
                # 检索侧同时排除 `%/rejected/%`，计数自然归零。
                reject_path = path.replace("/pending/", "/rejected/", 1)
                cur.execute(
                    "update mem.doc set path=%s, updated_at=now() where user_id=%s and path=%s",
                    (reject_path, user_id, path),
                )
                cur.execute(
                    "update mem.edge set src=%s where user_id=%s and src=%s",
                    (reject_path, user_id, path),
                )
                _review_log({"ts": datetime.now(timezone.utc).isoformat(), "id": path,
                             "archived_to": reject_path,
                             "decision": "reject", "problems": problems, "reason": reason})
                _history.add(path, "reject", actor, new_hash=reject_path,
                             reason="; ".join(problems))
                # Observation 跟随（2026-11-18）：文档搬去 rejected/ 了，观测记录不能还挂在
                # pending 路径上装活——不跟随就会留下「指向空路径的 active」幽灵。
                try:
                    _observation.relocate(path, reject_path, user_id, status="archived",
                                          reason="review rejected: " + "; ".join(problems)[:180])
                except Exception:
                    pass
                # 事项侧也要知道（2026-11-18）：挂了但被拒 → 关联标 rejected，工作台第 ④ 格可见。
                try:
                    _issues.mark_rejected(path, "; ".join(problems)[:160], reject_path)
                except Exception:
                    pass
                try:
                    _rules.remove(path)
                except Exception:
                    pass
                c.commit()
                return {"ok": False, "id": path, "archived_to": reject_path,
                        "decision": "rejected", "problems": problems,
                        "hint": "已归档到 rejected/（不删、可按 id 直取）；改好再 remember 一遍"}

            new_path = path.replace("/pending/", "/", 1)
            cur.execute(
                "update mem.doc set path=%s, updated_at=now() where user_id=%s and path=%s",
                (new_path, user_id, path),
            )
            cur.execute(
                "update mem.edge set src=%s where user_id=%s and src=%s",
                (new_path, user_id, path),
            )
            _review_log({"ts": datetime.now(timezone.utc).isoformat(), "id": path,
                         "new_id": new_path, "decision": "forced" if forced else "pass",
                         "problems": problems, "reason": reason,
                         "supersede_of": supersede_of})
            _history.add(path, "approve", actor, new_hash=new_path,
                         reason=(reason or ("forced: " + "; ".join(problems) if forced else
                                            ("supersede " + supersede_of if supersede_of else ""))))
            # Observation 跟随：pending → 正式区，obs_id 换成新路径，状态保持 active。
            try:
                _observation.relocate(path, new_path, user_id, status="active",
                                      reason="review passed")
            except Exception:
                pass
            # 事项关联也跟着搬（否则链接指向已不存在的 pending 路径）。
            try:
                _issues.mark_moved(path, new_path)
            except Exception:
                pass
            try:
                _rules.approve(path, new_path)
            except Exception:
                pass
            if supersede_of:
                # 自我修正：把被撤回的那条登记进 supersede（检索时隐藏，不删数据）。
                target = _referenced_doc(cur, user_id, body)
                if target:
                    try:
                        _supersede.add(target, new_path, reason=(title or "")[:160], actor=actor)
                    except Exception:
                        pass
        c.commit()
    return {"ok": True, "id": new_path, "decision": "forced" if forced else "pass",
            "problems_skipped": problems}


def pending_summary(user_id: str) -> dict:
    """待审积压：多少条、最老一条多久了。"""
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                "select count(*), min(updated_at), max(updated_at) from mem.doc "
                "where user_id=%s and path like '%%/pending/%%'",
                (user_id,),
            )
            n, oldest, newest = cur.fetchone()
            cur.execute(
                "select path, kind, title, updated_at from mem.doc "
                "where user_id=%s and path like '%%/pending/%%' order by updated_at limit 5",
                (user_id,),
            )
            samples = [
                {"id": r[0], "kind": r[1], "title": r[2], "date": r[3].isoformat() if r[3] else None}
                for r in cur.fetchall()
            ]
    age_h = None
    if oldest:
        age_h = round((datetime.now(timezone.utc) - oldest).total_seconds() / 3600, 1)
    return {"count": n, "oldest_age_hours": age_h,
            "oldest_at": oldest.isoformat() if oldest else None,
            "newest_at": newest.isoformat() if newest else None, "samples": samples}
# P1 同义短语扩展（2026-11-16，最小词表，不搞同义词库）：
# 目标是填掉「写回失败了怎么办」这类同义鸿沟——query 用一个词、文档用另一个词，gram 永远撞不上。
# 开关 MEM_SYNONYMS=off 可一键关掉（对照实验用），默认 on。
_SYNONYM_GROUPS = (
    ("失败", "重试", "报错", "错误"),
    ("写回", "回填", "落库", "入库"),
    ("超时", "卡住", "慢", "阻塞"),
    ("删除", "清理", "回收", "清除"),
    ("检索", "搜索", "查找", "召回"),
)


def _expand_synonyms(terms: list[str]) -> list[str]:
    if os.environ.get("MEM_SYNONYMS", "on").strip().lower() in ("0", "off", "false", "no"):
        return terms
    extra: list[str] = []
    for group in _SYNONYM_GROUPS:
        if any(any(w in term for w in group) for term in terms):
            extra.extend(group)
    if not extra:
        return terms
    return list(dict.fromkeys([*terms, *extra]))[:12]

# 第二步（2026-11-17）：数字归一化（查询期补裸形/千分位形，不动索引、0 DDL）。
# 病根：11 条 T2 是纯数字题（query 写 20494、doc 里是 20,494），df=0 → 换任何打分公式都是 0 分。
# 做法：抽 query 里所有数字形态，同时补「去分隔符的裸形」和「加千分位的形态」，两个方向都覆盖。
_NUM_RE = re.compile(r"\d[\d,\uFF0C. ]*\d|\d")


_DIGIT_MAX = 8  # 数字变体自身也要有界：多数字 query（版本号、多组编号）不能让 SQL 项数失控


def _digit_variants(terms: list[str], query: str) -> list[str]:
    """数字形态变体。**不占用实词的 16 槽上限**（2026-11-17）。

    为什么单独占额度：数字变体只是同一个数字的另一种写法（`2026-11-16` → `2026/2,026/11/16`），
    跟中文实词不该抢位置。实测 V2-006 一条 query 里日期就产生 4 个变体，挤进同一个 16 槽列表
    等于用「重复的同一件事」占掉实词额度。
    为什么仍要 _DIGIT_MAX：变体数量与 query 里的数字组数成正比，不设上界时
    「v1.2.3 到 v1.2.10 的 20 处改动」这类 query 会拼出几十个 ilike，SQL 直接变慢。
    """
    extra: list[str] = []
    for raw in _NUM_RE.findall(query or ""):
        bare = re.sub(r"[,，. ]", "", raw)
        if not bare:
            continue
        extra.append(bare)
        if len(bare) >= 4:
            grouped = bare
            for i in range(len(bare) - 3, 0, -3):
                grouped = grouped[:i] + "," + grouped[i:]
            extra.append(grouped)
    if not extra:
        return terms
    out = list(dict.fromkeys(terms))[:16]
    seen = set(out)
    added = 0
    for e in dict.fromkeys(extra):
        if e in seen:
            continue
        if added >= _DIGIT_MAX:
            break
        seen.add(e)
        out.append(e)
        added += 1
    return out