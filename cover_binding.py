"""封面与正文绑定（计划 2.2 / 4.1「相关性差 -10」）。

对策白纸黑字：封面与正文强绑定，禁用随机图库配图。

判定原则：
- 配图检索 query 必须由「正文实体」（球员/球队，取自实体词典）或标题词派生；
- 图库通用词（football / stadium / soccer…）一律不构成绑定，不得作为封面来源；
- 找不到绑定图时，宁可不配图，也不放随机图库图。

本模块被 data_collector.search_images 调用；orchestrator 亦可用来对既有配图做复核。
"""

from __future__ import annotations

import re

# 图库通用检索词：命中即视为「随机图库配图」，不构成正文绑定
GENERIC_TERMS = {
    "football", "soccer", "stadium", "match", "sports", "sport", "ball",
    "football match", "football match action", "football match stadium",
    "football stadium", "soccer ball", "goal", "pitch",
}

_ENTITY_TERMS: set[str] | None = None


def _entity_terms() -> set[str]:
    """复用抽取器的实体词典（config/entity_map.json）。"""
    global _ENTITY_TERMS
    if _ENTITY_TERMS is None:
        try:
            from extractor import _entity_names
            _ENTITY_TERMS = _entity_names()
        except Exception:
            _ENTITY_TERMS = set()
    return _ENTITY_TERMS


def article_entities(article: dict, limit: int = 6) -> list[str]:
    """从正文/标题中提取实体词（长名优先，避免「曼城」压过「曼城vs利物浦」）。"""
    if not isinstance(article, dict):
        return []
    blob = f"{article.get('title', '')}\n{article.get('content', '')}"
    terms = _entity_terms()
    hits = sorted((t for t in terms if t and t in blob), key=len, reverse=True)
    # 去子串冗余：已被更长实体覆盖的短名不重复计入
    out: list[str] = []
    for h in hits:
        if any(h in o for o in out):
            continue
        out.append(h)
        if len(out) >= limit:
            break
    return out


def title_terms(title: str, limit: int = 5) -> list[str]:
    """标题派生检索词（去除标点与停用词）。"""
    if not title:
        return []
    filler = {"的", "了", "是", "在", "和", "也", "都", "就", "要", "会", "能", "不", "这", "那", "吗", "呢"}
    cleaned = re.sub(r"[？?！!：:，,。、\s]+", " ", title)
    terms = [t for t in cleaned.split() if len(t) >= 2 and t not in filler]
    return terms[:limit]


def is_generic_query(q: str) -> bool:
    """通用图库词（不构成绑定）。"""
    if not q:
        return True
    ql = q.strip().lower()
    if ql in GENERIC_TERMS:
        return True
    # 去掉所有通用词与常见装饰词后为空 → 判定通用
    stripped = ql
    for t in sorted(GENERIC_TERMS, key=len, reverse=True):
        stripped = stripped.replace(t, " ")
    stripped = re.sub(r"[\s\-_]+", "", stripped)
    return stripped == ""


def build_bound_query(entities, fallback_terms=None, *, suffix="football") -> str:
    """由实体（优先）或标题词构造绑定检索 query；两者皆无则返回空串。"""
    ents = [e for e in (entities or []) if e]
    if ents:
        return f"{' '.join(ents[:3])} {suffix}".strip()
    fb = [t for t in (fallback_terms or []) if t and not is_generic_query(t)]
    if fb:
        return f"{' '.join(fb[:3])} {suffix}".strip()
    return ""


def image_is_bound(image: dict, entities, fallback_terms=None) -> bool:
    """判断一张候选图是否与正文绑定。

    绑定条件（满足其一）：
    - 来源为实体站点（wikipedia/footyrenders），即按实体精确抓取；
    - 检索 query 绑定正文（非通用词，由实体或标题词派生）；
    - 图片 alt/description/title 命中正文实体或标题词。
    """
    if not isinstance(image, dict):
        return False
    if image.get("source") in ("wikipedia", "footyrenders"):
        return True
    # query 层面绑定：只要检索词非通用、且由正文派生，即视为绑定
    q = str(image.get("query", "")).strip()
    if q and not is_generic_query(q):
        return True
    text = " ".join(str(image.get(k, "")) for k in ("alt", "description", "title")).lower()
    if not text:
        return False
    for e in (entities or []):
        if e and e.lower() in text:
            return True
    for t in (fallback_terms or []):
        if t and len(t) >= 2 and t.lower() in text:
            return True
    return False


def filter_bound_images(images, entities, fallback_terms=None) -> list:
    """只保留与正文绑定的候选图；通用图库图被剔除。"""
    return [img for img in (images or []) if image_is_bound(img, entities, fallback_terms)]


def bind_topic_query(topic: dict, *, suffix="football") -> tuple[str, list[str]]:
    """给定 topic，返回 (绑定 query, 实体列表)。query 为空表示无绑定素材。"""
    ents = article_entities(topic)
    q = build_bound_query(ents, title_terms(topic.get("title", "")), suffix=suffix)
    return q, ents
