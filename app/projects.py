"""项目开工必读清单（gate 自动推，不靠模型自己想起来）。

为什么要有这一层（2026-11-18 账号主点名，根因三条之一）：
`apps/skill/README.md:23` 明写着「做 CMS / 列表 / 详情 / 图片 / 分页相关改动前，先读
`cms-ui-playbook.md` 的第 3、4、6 节」——但那份目录不在技能注册表里，检索也不按
「我要动哪个项目哪个页面」推规范。所以规矩写在磁盘上没人被推到面前，最后靠账号主
主动点名才想起来。这一层补的就是「项目 + 任务类型 → 必读清单」的索引。

匹配规则（两条都中才算命中，宁可漏推不要乱推）：
  - 项目词：query 里出现项目名（zhishu-web / 智枢 …）
  - 任务词：query 里出现该项目的页面类型词（cms / 列表 / 详情 / 卡片 / 图片 / 分页 / 迁移 …）

清单本身放 data/observations/project-reads.json，可以加条目而不用改代码；
缺文件自动跳过（不推不存在的路径），推的内容带「为什么读」，不是甩一堆链接。
"""

from __future__ import annotations

from .config import DATA_DIR

import json
import os
import pathlib
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REGISTRY = DATA_DIR / "observations" / "project-reads.json"
# 这些规范文件在 zhishu 主仓里；memory-mcp 只是服务的家，默认指回首仓根。
ZHS_ROOT = pathlib.Path(os.environ.get("ZHS_ROOT", r"D:\code\yunxiao\zhishu"))
BLOCK_MAX_CHARS = 700

_DEFAULT = [
    {
        "project": "zhishu-web",
        "project_words": ["zhishu-web", "zhishu_web", "智枢", "主站", "zhishu.mingzhang"],
        # 路径前缀也算项目命中：真实说法常常只说「/cms/book 详情页」，不点项目名。
        # 少了这一条，验收用例「把 /cms/book 详情页迁到新站」会推不出来（2026-11-18 实测）。
        "path_prefixes": ["/cms/", "cms/", "/api/v1/cms"],
        "task_words": ["cms", "列表", "详情", "卡片", "图片", "分页", "迁移", "页面",
                       "前端", "徽标", "富文本", "编辑器", "导航", "加载更多"],
        "reads": [
            {
                "path": "apps/skill/cms-ui-playbook.md",
                "why": "CMS 列表/详情/图片/分页的实现与验收口径（先读第 3、4、6 节）",
            },
            {
                "path": "docs/org/tech/frontend-tokens.md",
                "why": "前端 token 与视觉口径，改页面不许另起一套样式",
            },
            {
                "path": "docs/24-page-migration-protocol.md",
                "why": "页面迁移的流程、验收判据与禁止项",
            },
        ],
    }
]


def _registry() -> list[dict[str, Any]]:
    if _REGISTRY.exists():
        try:
            data = json.loads(_REGISTRY.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return _DEFAULT


def seed() -> dict[str, Any]:
    """把默认清单落到文件（幂等；已存在就不动，避免覆盖人工加的条目）。"""
    _REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    if _REGISTRY.exists():
        return {"ok": True, "created": False, "path": str(_REGISTRY)}
    _REGISTRY.write_text(json.dumps(_DEFAULT, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "created": True, "path": str(_REGISTRY)}


def match(query: str) -> dict[str, Any]:
    """按「项目词 + 任务词」双命中挑清单；返回 {matched, project, reads, block}。"""
    q = (query or "").lower()
    if not q:
        return {"matched": False, "project": "", "hits": [], "reads": [], "block": ""}
    for item in _registry():
        proj_hits = [w for w in item.get("project_words") or [] if w.lower() in q]
        proj_hits += [w for w in item.get("path_prefixes") or [] if w.lower() in q]
        task_hits = [w for w in item.get("task_words") or [] if w.lower() in q]
        if not proj_hits or not task_hits:
            continue
        reads: list[dict[str, Any]] = []
        for r in item.get("reads") or []:
            rel = (r.get("path") or "").strip()
            if not rel:
                continue
            abs_path = ZHS_ROOT / rel
            reads.append({
                "path": rel,
                "exists": abs_path.exists(),
                "why": r.get("why") or "",
            })
        reads = [r for r in reads if r["exists"]]        # 不推不存在的路径
        if not reads:
            return {"matched": False, "project": item.get("project") or "", "hits": task_hits,
                    "reads": [], "block": ""}
        lines = [
            f"## 开工必读：{len(reads)} 份（项目 {item.get('project')} · 命中 {'/'.join(task_hits[:4])}）",
            "这是规矩不是建议：动这一块之前先把下面几份读完，别靠回忆。",
        ]
        for i, r in enumerate(reads, 1):
            lines.append(f"{i}. {r['path']} —— {r['why']}")
        block = "\n".join(lines)
        if len(block) > BLOCK_MAX_CHARS:
            block = block[:BLOCK_MAX_CHARS] + "…"
        return {"matched": True, "project": item.get("project") or "",
                "hits": task_hits, "reads": reads, "block": block}
    return {"matched": False, "project": "", "hits": [], "reads": [], "block": ""}
