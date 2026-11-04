#!/usr/bin/env python3
"""图库检索的英文 query 解析（2026-11-04）。

## 为什么需要这一层

`data_collector.search_images` 原来把 `cover_binding.build_bound_query()` 产出的
**中文实体**直接丢给 Pixabay / Pexels / Unsplash，而这三家都是**英文标签索引**。
2026-11-04 拿到 Pexels Key 后第一次真跑，结果是这样：

    query="皇家马德里"            → 第一条是「酱油饺子特写」(dumplings with soy sauce)
    query="欧冠"                  → 欧元纸币、兔子、欧罗巴钞票
    query="Kylian Mbappe"         → 更衣室、陌生男子肖像（20 张 alt 命中该名字 0 张）
    query="Haaland"               → 奥地利的一个村庄（Walchsee）
    query="FC Internazionale Milano" → 诺坎普（巴萨主场）
    query="Club Atlético de Madrid"  → 阿根廷的 Estadio Libertadores de América

因为 `search_images` 里那一段是 `if core: 查图库`，中文永远满足条件，
所以**每一篇没走到维基的稿子都会配上一张看起来合理、实际无关的图**。
这比无封面更糟：无封面还有 `cover_card` 数据卡兜底，错图是事实错误。

## 所以这一层不追求覆盖率，只做三件事

1. **中文一律不查图库**——不翻译就不查，绝不把中文丢给英文索引；
2. **球员名一律不查**——图库里没有球员肖像的版权图（实测 20 张里 alt 命中 0~2 张），
   返回的一定是路人或风景；
3. **只有实测确实有货的球队才查**（`config/gallery_entities.yaml`），用英文队名或
   球场名检索，且返回图的 alt **必须命中该队主场名**（重音折叠后比对），否则丢弃。

实测覆盖：16 支主流球队里只有 10 支登记成功（巴萨 18/30 张、阿森纳 23/30、
拜仁 11/30、皇马 9/30、曼城 9/30、利物浦 6/30、曼联 3/30、多特 3/30、米兰双雄 2/30）。
其余 6 支（尤文/热刺/马竞/纽卡/巴黎/切尔西）图库里根本没有对应球场照片，一律不登记 ——
登记了也只是把「无图」换成「错图」。

## 用法

    import gallery_query
    g = gallery_query.resolve(["姆巴佩", "皇马"])   # → {"entity": "皇马", "query": "Real Madrid CF", ...}
    if g:
        for img in _gallery_pexels(g["query"], need, accept=g["accept"]):
            ...

    python3 gallery_query.py --selftest
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

try:                                    # PyYAML 是项目既有依赖（config/*.yaml 都在用）
    import yaml
except Exception:                       # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent
ENTITY_MAP = ROOT / "config" / "entity_map.json"
WHITELIST = ROOT / "config" / "gallery_entities.yaml"

_CACHE: dict[str, object] = {}


def fold(s: str) -> str:
    """小写 + 去重音。``Bernabéu`` → ``bernabeu``。

    没有这一步，「皇马 9 张」会被判成「0 张」——Pexels 的 alt 写的是
    ``Santiago Bernabéu``，用 ascii 的 ``bernabeu`` 永远匹配不上。
    这个坑在 2026-11-04 的第一次测量里真的出现过，差点把皇马从白名单里删掉。
    """
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s))
                   if not unicodedata.combining(c)).lower()


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in (s or ""))


def reload() -> None:
    """丢掉缓存（测试改配置后调用）。"""
    _CACHE.clear()


def _load() -> tuple[dict, list]:
    """解析白名单，**合法的进 whitelist，非法的进 invalid 列表**。

    非法条目不能被静默丢弃：这个项目已经吃过太多次「配置写错 → 功能静默失效 →
    几个月没人发现」的亏（has_anchor() 吞 TypeError、_img_topic 取错嵌套层、
    publish_ledger 覆盖写）。所以这里把非法项显式收集起来，
    由 scripts/verify_gallery_entities.py 硬性报红 —— 写错必须有人知道。
    """
    valid: dict = {}
    invalid: list = []
    try:
        if yaml is None:
            return {}, [{"name": "*", "reason": "PyYAML 不可用，无法读取白名单"}]
        if not WHITELIST.exists():
            return {}, [{"name": "*", "reason": f"白名单文件不存在：{WHITELIST}"}]
        data = yaml.safe_load(WHITELIST.read_text(encoding="utf-8")) or {}
        for name, item in (data.get("clubs") or {}).items():
            name = str(name).strip()
            if not isinstance(item, dict):
                invalid.append({"name": name, "reason": "条目不是映射"})
                continue
            q = str(item.get("query") or "").strip()
            acc = tuple(fold(a) for a in (item.get("accept") or []) if str(a).strip())
            if not q:
                invalid.append({"name": name, "reason": "query 为空"})
            elif _has_cjk(q):
                invalid.append({"name": name, "reason": f"query 含中文（{q}）"})
            elif not acc:
                invalid.append({"name": name, "reason": "accept 为空（无法校验）"})
            else:
                valid[name] = {"query": q, "accept": acc}
    except Exception as e:
        return {}, [{"name": "*", "reason": f"解析白名单失败：{type(e).__name__}: {e}"}]
    return valid, invalid


def whitelist() -> dict:
    """白名单：{标准中文名: {"query": str, "accept": tuple}}。只含合法条目。"""
    if "wl" not in _CACHE:
        valid, invalid = _load()
        _CACHE["wl"] = valid
        _CACHE["invalid"] = invalid
    return _CACHE["wl"]                      # type: ignore[return-value]


def invalid() -> list:
    """被丢弃的非法条目（写错名字/空 accept/中文 query）。空列表才算配置健康。"""
    if "invalid" not in _CACHE:
        whitelist()
    return _CACHE.get("invalid") or []       # type: ignore[return-value]


def _alias_map() -> dict:
    """别名/标准名 → 标准中文名（只收 club，球员不进图库）。"""
    if "alias" in _CACHE:
        return _CACHE["alias"]                      # type: ignore[return-value]
    out: dict = {}
    try:
        ents = (json.loads(ENTITY_MAP.read_text(encoding="utf-8")) or {}).get("entities") or {}
        for v in ents.values():
            if not isinstance(v, dict) or v.get("category") != "club":
                continue
            canon = str(v.get("name_zh") or "").strip()
            if not canon:
                continue
            for n in [canon] + list(v.get("aliases") or []):
                n = str(n or "").strip()
                if n:
                    out[n] = canon
                    out[n.lower()] = canon
    except Exception:
        out = {}
    _CACHE["alias"] = out
    return out


def resolve(entities) -> dict | None:
    """挑出第一个「白名单内」的球队实体，返回检索参数；没有则 None。

    返回 ``{"entity": 标准中文名, "matched": 正文里命中的写法,
             "query": 英文检索词, "accept": (可接受的 alt token, ...)}``
    返回 None 表示**不要查图库**（不是「查了没结果」）——调用方据此走数据卡兜底。
    """
    wl = whitelist()
    if not wl:
        return None
    alias = _alias_map()
    for e in entities or []:
        if not isinstance(e, str):
            continue
        key = e.strip()
        if not key:
            continue
        canon = alias.get(key) or alias.get(key.lower()) or key
        ent = wl.get(canon)
        if ent:
            return {"entity": canon, "matched": key,
                    "query": ent["query"], "accept": ent["accept"]}
    return None


def is_accepted(alt: str, accept) -> bool:
    """图库返回图的 alt 是否真的是这个实体。空 alt 一律拒绝（无法校验＝不可信）。"""
    a = fold(alt)
    if not a:
        return False
    return any(tok and tok in a for tok in (accept or ()))


def selftest() -> list:
    cases = []
    cases.append(("重音折叠 Bernabéu", fold("Santiago Bernabéu") == "santiago bernabeu"))
    cases.append(("大小写折叠", fold("CAMP NOU") == "camp nou"))

    g = resolve(["皇家马德里"])                       # 别名 → 标准名
    cases.append(("别名 皇家马德里 → 皇马", bool(g) and g["entity"] == "皇马"
                  and g["query"] == "Real Madrid CF"))
    cases.append(("别名 银河战舰 → 皇马", (resolve(["银河战舰"]) or {}).get("entity") == "皇马"))
    cases.append(("球员不查图库", resolve(["姆巴佩"]) is None))
    cases.append(("混合实体取球队", (resolve(["姆巴佩", "皇马"]) or {}).get("entity") == "皇马"))
    cases.append(("未登记球队不查", resolve(["尤文"]) is None))
    cases.append(("空输入", resolve(None) is None and resolve([]) is None
                  and resolve([""]) is None))
    cases.append(("非字符串实体不炸", resolve([None, 1, "皇马"]) is not None))

    acc = ("bernabeu",)
    cases.append(("alt 命中球场名", is_accepted("Discover the iconic Santiago Bernabéu Stadium", acc)))
    cases.append(("负向：别家球场", not is_accepted("Wide shot of Camp Nou stadium", acc)))
    cases.append(("负向：空 alt", not is_accepted("", acc)))
    cases.append(("负向：空 accept", not is_accepted("Bernabeu", ())))

    wl = whitelist()
    cases.append(("白名单非空", len(wl) >= 5))
    cases.append(("白名单 query 全英文", all(not _has_cjk(v["query"]) for v in wl.values())))
    cases.append(("白名单 accept 全非空", all(v["accept"] for v in wl.values())))
    return cases


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="图库英文 query 解析")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--entities", nargs="*", default=None, help="试解析这些实体")
    a = ap.parse_args()
    if a.selftest:
        fails = []
        for name, ok in selftest():
            print(("  ✅ " if ok else "  ❌ ") + name)
            if not ok:
                fails.append(name)
        print(f"\n{len(fails)} FAIL / {len(selftest())} 项")
        return 1 if fails else 0
    if a.entities is not None:
        print(json.dumps(resolve(a.entities), ensure_ascii=False, indent=1))
        return 0
    for k, v in whitelist().items():
        print(f"{k:<8} query={v['query']:<28} accept={list(v['accept'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
