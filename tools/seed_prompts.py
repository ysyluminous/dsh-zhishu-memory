"""Seed the five extraction templates into mem.prompt_template."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN  # noqa: E402

COMMON_SCHEMA = {
    "summary": "string",
    "key_points": ["string"],
    "entities": [{"type": "person|org|project|product|book|concept|place|event|metric|topic",
                  "name": "string", "aliases": ["string"], "confidence": "0-1"}],
    "relations": [{"src": "string", "relation": "string", "dst": "string", "confidence": "0-1"}],
    "tags": ["string"],
    "quotes": [{"text": "string", "note": "string"}],
    "coverage": {"people": "bool", "time": "bool", "numbers": "bool",
                 "decisions": "bool", "evidence": "bool"},
    "confidence": "0-1",
}

TEMPLATES = [
    {
        "key": "extract.conversation.v1",
        "source_type": "conversation",
        "title": "会话抽取",
        "description": "从一段用户与 AI 的会话中抽取结论、决策、事实、偏好、待办、实体和未决问题。",
        "template": """你是个人记忆抽取器。下面是用户与 AI 的一段会话。

只提取用户明确表达、明确确认或反复使用的内容；AI 的猜测不算，系统提示和工具输出不算。
不要编造；没有的字段给空数组。输出严格 JSON，不要 Markdown 代码块。

字段：
- summary：3-5 句，说明这段会话解决了什么、结论是什么
- decisions：[{question, decision, rationale, reversibility, confidence}]
  reversibility 取 easy / hard / irreversible
- facts：[{statement, evidence, confidence}]，evidence 用会话里的原话或短引用
- preferences：字符串数组，用户明确表达的偏好/习惯
- tasks：[{title, owner, due, status}]，owner 只能是 user 或 ai，due 没有就 null
- open_questions：字符串数组
- entities / relations / tags / quotes / coverage / confidence：按通用 schema

会话标题：{{title}}
元数据：{{meta}}
正文：
{{content}}""",
        "output_schema": {**COMMON_SCHEMA,
                          "decisions": [{"question": "string", "decision": "string",
                                         "rationale": "string", "reversibility": "easy|hard|irreversible",
                                         "confidence": "0-1"}],
                          "facts": [{"statement": "string", "evidence": "string", "confidence": "0-1"}],
                          "preferences": ["string"],
                          "tasks": [{"title": "string", "owner": "user|ai", "due": "string|null",
                                     "status": "string"}],
                          "open_questions": ["string"]},
    },
    {
        "key": "extract.article.v1",
        "source_type": "article",
        "title": "文章/章节抽取",
        "description": "从一篇文章或章节中抽取摘要、要点、概念、引用、可行动项和问题。",
        "template": """你是知识整理器。阅读下面的文章或章节，输出严格 JSON。

只基于原文；不要补充原文没有的事实。没有的字段给空数组。

字段：
- summary：3-5 句
- key_points：5-10 条，每条一句
- concepts：[{name, definition}]，只收文中解释过的概念
- quotes：[{text, note}]，text 必须是原文片段
- actionables：读者可以立刻做的事
- questions：文中提出或留下的问题
- entities / relations / tags / coverage / confidence：按通用 schema

标题：{{title}}
元数据：{{meta}}
正文：
{{content}}""",
        "output_schema": {**COMMON_SCHEMA,
                          "concepts": [{"name": "string", "definition": "string"}],
                          "actionables": ["string"],
                          "questions": ["string"]},
    },
    {
        "key": "extract.book.v1",
        "source_type": "book",
        "title": "图书抽取",
        "description": "从一本书或一个章节抽取主旨、结构、核心观点、关联书和引用。",
        "template": """你是读书笔记整理器。阅读下面的图书/章节内容，输出严格 JSON。

只基于原文。没有的字段给空数组。

字段：
- summary：这本书/这一章在讲什么，3-5 句
- thesis：作者最核心的主张，一句话
- structure：章节或论证结构，[{part, purpose}]
- key_ideas：5-10 条核心观点
- related_books：[{title, relation}]，relation 说明为什么相关
- quotes：[{text, note}]，必须是原文
- entities / relations / tags / coverage / confidence：按通用 schema

书名/章节：{{title}}
元数据：{{meta}}
正文：
{{content}}""",
        "output_schema": {**COMMON_SCHEMA,
                          "thesis": "string",
                          "structure": [{"part": "string", "purpose": "string"}],
                          "key_ideas": ["string"],
                          "related_books": [{"title": "string", "relation": "string"}]},
    },
    {
        "key": "extract.resource.v1",
        "source_type": "cms_resource",
        "title": "资源抽取",
        "description": "从 CMS 资源中抽取类型、适用人群、主题、工具/链接、质量判断和实体。",
        "template": """你是资源库整理器。阅读下面的资源说明，输出严格 JSON。

只基于原文，不猜下载地址或价格。

字段：
- summary：这个资源是什么、解决什么问题，2-4 句
- resource_type：工具 / 教程 / 模板 / 数据集 / 课程 / 其他
- target_user：适合谁
- topics：主题标签
- tools_links：文中出现的工具或链接
- quality：{signal, concerns}，signal 是值得收藏的理由，concerns 是风险或不足
- entities / relations / tags / coverage / confidence：按通用 schema

标题：{{title}}
元数据：{{meta}}
正文：
{{content}}""",
        "output_schema": {**COMMON_SCHEMA,
                          "resource_type": "string",
                          "target_user": "string",
                          "topics": ["string"],
                          "tools_links": ["string"],
                          "quality": {"signal": "string", "concerns": "string"}},
    },
    {
        "key": "extract.x_post.v1",
        "source_type": "x_post",
        "title": "X 帖子抽取",
        "description": "从 X 帖子抽取钩子、主题、主张、实体、链接和是否值得索引。",
        "template": """你是内容情报整理器。阅读下面的 X 帖子，输出严格 JSON。

只基于帖子本身；不要补背景。

字段：
- summary：1-3 句
- hook：作者用什么抓住注意力
- topic：一句话主题
- claims：作者的主张，逐条列出
- links：帖子里的链接
- engagement_note：互动数据说明了什么（没有数据就写 unknown）
- worth_indexing：{decision, reason}，decision 取 yes / no / maybe
- entities / relations / tags / coverage / confidence：按通用 schema

帖子：{{title}}
元数据：{{meta}}
正文：
{{content}}""",
        "output_schema": {**COMMON_SCHEMA,
                          "hook": "string",
                          "topic": "string",
                          "claims": ["string"],
                          "links": ["string"],
                          "engagement_note": "string",
                          "worth_indexing": {"decision": "yes|no|maybe", "reason": "string"}},
    },
]


def main() -> None:
    with psycopg.connect(MEM_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            for t in TEMPLATES:
                cur.execute(
                    """insert into mem.prompt_template
                       (key,version,source_type,title,description,template,output_schema,model,temperature,enabled)
                       values (%s,1,%s,%s,%s,%s,%s,'',0.2,true)
                       on conflict (key,version) do update
                         set source_type=excluded.source_type, title=excluded.title,
                             description=excluded.description, template=excluded.template,
                             output_schema=excluded.output_schema, enabled=true""",
                    (t["key"], t["source_type"], t["title"], t["description"], t["template"],
                     json.dumps(t["output_schema"], ensure_ascii=False)),
                )
        c.commit()
        with c.cursor() as cur:
            cur.execute("select key,title from mem.prompt_template where key like 'extract.%' and version=1 order by key")
            for row in cur.fetchall():
                print(row)


if __name__ == "__main__":
    main()
