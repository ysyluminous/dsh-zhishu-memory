from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

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
            weights[i] = 1.0 / (1.0 + math.log(max(1.0, df))) if df >= 3 else 1e-6
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
            # df<3 的 gram 基本是切词噪声（么解/决的），不参与覆盖度门槛，但仍可命中。
            weights[i] = 1.0 / (1.0 + math.log(df)) if df >= 3 else 1e-6
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
    params = {**params, "incl_pending": bool(include_pending)}
    rows: list[dict[str, Any]] = []
    cur.execute(
        f"""select path, kind, title, body, updated_at, {score_doc} as score
            from mem.doc
            where user_id = %(uid)s and ({match_doc})
              and (%(incl_pending)s or path not like '%%/pending/%%')
            order by score desc, updated_at desc limit %(lim)s""",
        params,
    )
    for row in cur.fetchall():
        rows.append({
            "scope": "personal",
            "source": "mem.doc",
            "kind": row[1],
            "id": row[0],
            "title": row[2] or "",
            "snippet": _snippet(row[3]),
            "date": row[4].isoformat() if row[4] else None,
            "score": round(float(row[5]) * _DOC_SCORE_WEIGHT, 4),
        })
    cur.execute(
        f"""select id, source, title, snippet, updated_at, {score_conv} as score
            from mem.conv
            where user_id = %(uid)s and ({match_conv})
            order by score desc, updated_at desc limit %(lim)s""",
        params,
    )
    for row in cur.fetchall():
        rows.append({
            "scope": "personal",
            "source": "mem.conv",
            "kind": "conversation",
            "id": row[0],
            "title": row[2] or row[1],
            "snippet": _snippet(row[3] or ""),
            "date": row[4].isoformat() if row[4] else None,
            "score": round(float(row[5]), 4),
        })
    rows.sort(key=lambda x: (x.get("score") or 0.0, x.get("date") or ""), reverse=True)
    return rows[:limit]


def search_personal(query: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """个人检索：gram 模式按 IDF 加权覆盖度召回 + 加权排序；legacy 模式保持原行为。

    user_id 过滤是 SQL 硬条件，检索结果不会跨用户（隔离用例见 tests/test_search_isolation.py）。
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(limit, 50))
    terms = _terms(q) or [q]
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
            out = _personal_rows(cur, params, len(terms), limit, first_op, weights)
            if gram_mode and len(out) < 3 and len(terms) > 1:
                # 召回不足时补一轮 OR 并合并（漏召回比排错更严重，宁可多给候选再精排）
                extra = _personal_rows(cur, params, len(terms), limit, "or", weights)
                seen = {r.get("id") for r in out}
                out += [r for r in extra if r.get("id") not in seen]
                out.sort(key=lambda x: (x.get("score") or 0.0, x.get("date") or ""), reverse=True)
                out = out[:limit]
            if not out and len(terms) > 1:
                out = _personal_rows(cur, params, len(terms), limit, "or", weights)
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
            "source": row[1],
            "id": row[0],
            "title": row[2] or "",
            "snippet": _snippet(row[3]),
            "date": row[4].isoformat() if row[4] else None,
        }
        for row in rows
    ]


def search(query: str, user_id: str, limit: int = 10, scope: str = "auto") -> dict[str, Any]:
    scope = (scope or "auto").lower()
    limit = max(1, min(limit, 50))
    if scope == "personal":
        results = search_personal(query, user_id, limit)
    elif scope == "public":
        results = search_public(query, limit)
    else:
        personal = search_personal(query, user_id, limit)
        public = search_public(query, limit)
        results = (personal + public)[:limit]
    return {"query": query, "scope": scope, "results": results, "count": len(results)}


def remember(user_id: str, text: str, kind: str = "event", title: str = "", topics: str = "") -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("内容不能为空")
    kind = (kind or "event").strip().lower()
    if kind not in {"event", "decision", "artifact", "entity", "belief", "metric"}:
        kind = "event"
    first = body.splitlines()[0].strip().lstrip("#").strip()
    title = (title or first)[:120]
    topic_list = [t.strip() for t in (topics or "").split(",") if t.strip()]
    digest = hashlib.sha256(body.encode()).hexdigest()
    day = datetime.now().strftime("%Y%m%d")
    path = f"cloud/{user_id}/{day}-{digest[:8]}.md"

    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
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
    return {"ok": True, "id": path, "kind": kind, "title": title, "topics": topic_list}


def whoami(user_id: str) -> dict[str, Any]:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select id, name, created_at from mem.app_user where id = %s", (user_id,))
            row = cur.fetchone()
    return {"user_id": user_id, "name": row[1] if row else user_id, "created_at": row[2].isoformat() if row else None}


def stats(user_id: str) -> dict[str, Any]:
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
                tid = _new_id()
                cur.execute(
                    """insert into mem.todo (id,user_id,title,status,priority,source)
                       values (%s,%s,%s,'pending',2,%s)""",
                    (tid, user_id, title, f"idea:{idea_id}:{step_id}"),
                )
                created.append(tid)
        c.commit()
    return {"ok": True, "created": len(created), "todo_ids": created}


from . import todo_store as _todo_store


def todo_add(user_id: str, title: str, detail: str = "", due_at: str = "",
             priority: int = 2, project: str = "", source: str = "",
             owner: str = "human") -> dict:
    return _todo_store.todo_add(user_id, title, detail, due_at, priority, project, source, owner)


def todo_list(user_id: str, status: str = "", limit: int = 50) -> dict:
    return _todo_store.todo_list(user_id, status, limit)


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
        raise ValueError("????????")
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
        out.append({"id": r[0], "source": r[1], "title": r[2] or "",
                    "updated_at": r[3].astimezone().isoformat() if r[3] else None,
                    "message_count": r[4],
                    "last_user": (r[5] or "")[:300]})
    return {"count": len(out), "conversations": out}


def conv_get(conv_id: str, since: str = "", until: str = "", limit: int = 100,
             offset: int = 0) -> dict:
    start = _ms(since, None)
    end = _ms(until, None)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    conds = []
    args: list = [conv_id]
    if start:
        conds.append("created_at >= to_timestamp(%s)")
        args.append(start / 1000.0)
    if end:
        conds.append("created_at <= to_timestamp(%s)")
        args.append(end / 1000.0)
    extra = (" and " + " and ".join(conds)) if conds else ""
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute("select id,source,title,updated_at from mem.conv where id=%s", (conv_id,))
            meta = cur.fetchone()
            if not meta:
                raise ValueError("?????")
            cur.execute(
                f"""select
                      (select count(*) from mem.conv_message where conversation_id=%s{extra})
                    + (select count(*) from mem.live_event
                       where conversation_id=%s and reconciled_at is null{extra})""",
                [conv_id, *args[1:], conv_id, *args[1:]],
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                f"""select seq,role,created_at,text from (
                      select seq,role,created_at,text,omni_id ord
                      from mem.conv_message where conversation_id=%s{extra}
                      union all
                      select seq,role,created_at,text,id ord
                      from mem.live_event
                      where conversation_id=%s and reconciled_at is null{extra}
                    ) u order by seq asc, created_at asc, ord asc limit %s offset %s""",
                [conv_id, *args[1:], conv_id, *args[1:], limit, offset],
            )
            rows = cur.fetchall()
    messages = []
    for r in rows:
        text = r[3] or ""
        messages.append({"seq": r[0], "role": r[1],
                         "at": r[2].astimezone().isoformat() if r[2] else None,
                         "text": text[:20000], "truncated": len(text) > 20000})
    return {"id": meta[0], "source": meta[1], "title": meta[2] or "",
            "updated_at": meta[3].astimezone().isoformat() if meta[3] else None,
            "total": total, "offset": offset, "returned": len(messages),
            "backend": "pg", "messages": messages}

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
        raise ValueError("conversation_id ????")
    role = (role or "user").strip().lower()
    if role not in {"user", "assistant", "system", "tool"}:
        raise ValueError("role ??? user/assistant/system/tool")
    text = (text or "").replace("\x00", "")
    if not text.strip():
        raise ValueError("text ????")
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


def _recall_score(r: dict, now: datetime) -> float:
    base = float(r.get("score") or 0.0)
    return base * _KIND_WEIGHT.get(str(r.get("kind") or ""), 1.0) * _recall_decay(r, now)


def select_recall(query: str, user_id: str, limit: int = 5,
                  conversation_id: str = "") -> list[dict]:
    """gate 的召回选择：宽召回（每个检索词各查一次）→ 分层 → 加权 → 配额。"""
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit), 10))
    terms = [t for t in re.split(r"[\s,，。;；、/|]+", q) if len(t) >= 2][:4]
    seen: dict[tuple, dict] = {}
    for term in [q, *terms]:
        for r in search_personal(term, user_id, _GATE_CANDIDATES_PER_TERM):
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
              ok: bool = True, error: str = "") -> None:
    """Best-effort recall log row; never raise into the gate path."""
    hit_ids = [str(r.get("id")) for r in merged if r.get("id")]
    hit_scores = [round(float(r["score"]), 6) for r in merged
                  if isinstance(r.get("score"), (int, float))]
    try:
        with _conn(MEM_DSN) as c:
            with c.cursor() as cur:
                cur.execute(
                    """insert into mem.gate_log
                       (user_id,source,conversation_id,turn,query,query_hash,hit_ids,
                        hit_scores,count,injected_chars,latency_ms,ok,error)
                       values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)""",
                    (user_id, (source or "gate")[:64], conversation_id or "", turn,
                     (query or "")[:4000], _digest(query or ""),
                     json.dumps(hit_ids, ensure_ascii=False),
                     json.dumps(hit_scores),
                     len(merged), int(injected_chars), int(latency_ms),
                     bool(ok), (error or "")[:500]),
                )
            c.commit()
    except Exception:
        pass


def gate(query: str, user_id: str, source: str = "gate", conversation_id: str = "",
         role: str = "user", limit: int = 5, turn: int | None = None) -> dict:
    """Blocking pre-answer gate: log the prompt, retrieve context, return it."""
    t0 = time.monotonic()
    q = (query or "").strip()
    merged: list[dict] = []
    context = ""
    try:
        if not q:
            raise ValueError("query ????")
        limit = max(1, min(int(limit), 10))
        trace = None
        if conversation_id:
            event = ingest_event(source, conversation_id, role, q, title="", meta={"gate": True})
            trace = event.get("event_id")
        merged = select_recall(q, user_id, limit, conversation_id)
        lines = ["## ???????", "???????????????????????????"]
        for i, r in enumerate(merged, 1):
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:300]
            kind = r.get("kind") or r.get("scope") or ""
            lines.append(f"{i}. [{kind}] {title}\n   {snippet}")
        context = "\n".join(lines)[:4000]
        _gate_log(user_id, source, conversation_id, turn, q, merged, len(context),
                  _gate_elapsed_ms(t0))
        return {"ok": True, "trace_id": trace, "query": q, "count": len(merged),
                "results": merged, "context": context}
    except Exception as e:
        _gate_log(user_id, source, conversation_id, turn, q, merged, len(context),
                  _gate_elapsed_ms(t0), ok=False, error=f"{type(e).__name__}: {e}")
        raise

