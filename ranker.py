#!/usr/bin/env python3
"""择优模型分层调用（计划 11.2 / 12.4）

把「该选题今日是否有人关心」交给模型判断，但必须分层以控成本与延迟：

  L1 规则预筛：剔除时效过期、板块配比已满、主体冷门度超阈值的候选（约 40 → 15~20 条）
  L2 信号增强：为候选补齐热度信号（同题报道条数 / 来源媒体权重 / 社媒讨论量 / 搜索指数）
  L3 模型排序：按固定量表（五维度各 1-5 分）打分并排序，输出结构化 JSON 排名
  L4 缓存与限额：同一事实卡 24h 内只判一次；单日 token 超限自动降级为规则排序

同质化防护（计划 12.4）：量表强制覆盖五维度——冲突度 / 人物知名度 / 时效性 /
话题延展性 / 账号粉丝画像匹配度，每周人工抽查一次校准。

无热度信号时，L3 模型判断会退化为随机排序，因此信号源（L2）必须先接入；
本模块对缺失信号一律给保守默认值并标注，绝不假装有信号。
"""
import os
import json
from datetime import datetime, timedelta
from pathlib import Path

from constants import PROJECT_ROOT
from utils import CST

CACHE_PATH = PROJECT_ROOT / "data" / "ranker_cache.json"
ENTITY_MAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"

JUDGE_TTL_HOURS = 24
DEFAULT_DAILY_TOKEN_BUDGET = int(os.environ.get("RANKER_DAILY_TOKEN_BUDGET", "200000"))
DEFAULT_TARGET_MAX = 20          # L1 预筛后目标条数上限（计划 11.2：15~20）

# 五维度量表（计划 12.4）
DIMENSIONS = ["冲突度", "人物知名度", "时效性", "话题延展性", "粉丝画像匹配度"]

_CONFLICT_KW = ["却", "反而", "逆袭", "惨败", "绝杀", "爆冷", "反转", "下课", "翻盘", "打脸",
                "血洗", "复仇", "争议", "内讧", "离队", "转会", "伤停", "禁赛", "崩盘"]

# 来源媒体权重（计划 11.2 L2：来源媒体权重；1.0 为基准）
_SOURCE_WEIGHT = {
    "zhibo8": 1.0, "dongqiudi": 1.0, "hupu": 0.9,
    "罗马诺": 1.4, "天空体育": 1.3, "图片报": 1.2, "队报": 1.2, "官方": 1.5,
}
_SOURCE_WEIGHT_DEFAULT = 0.8

_awareness_cache = None


def load_awareness_map():
    """{name_zh: A/B/C}，来自 config/entity_map.json（缺文件则空表）。"""
    global _awareness_cache
    if _awareness_cache is not None:
        return _awareness_cache
    m = {}
    try:
        data = json.loads(ENTITY_MAP_PATH.read_text(encoding="utf-8"))
        for v in (data.get("entities") or {}).values():
            zh = v.get("name_zh")
            if zh:
                m[zh] = v.get("awareness", "C")
    except Exception:
        pass
    _awareness_cache = m
    return m


def awareness_of(name):
    """主体认知度 A/B/C；未登记视为 C（计划 13.1：未登记不得进生成环节）。"""
    return load_awareness_map().get(name, "C")


# ------------------------------------------------------------
# L4 缓存与限额
# ------------------------------------------------------------
def _load_cache(now=None):
    now = now or datetime.now(CST)
    try:
        c = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    if c.get("date") != now.strftime("%Y-%m-%d"):
        return {"date": now.strftime("%Y-%m-%d"), "judged": {}, "tokens_used": 0}
    c.setdefault("judged", {})
    c.setdefault("tokens_used", 0)
    return c


def _save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_hit(cache, fact_id, now):
    rec = (cache.get("judged") or {}).get(fact_id)
    if not rec:
        return None
    try:
        at = datetime.strptime(rec["at"], "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    except Exception:
        return None
    if (now - at).total_seconds() <= JUDGE_TTL_HOURS * 3600:
        return rec
    return None


def token_budget_ok(cache, budget=None):
    return (cache.get("tokens_used", 0) < (budget or DEFAULT_DAILY_TOKEN_BUDGET))


# ------------------------------------------------------------
# L1 规则预筛
# ------------------------------------------------------------
def l1_prefilter(candidates, ctx=None, *, target_max=DEFAULT_TARGET_MAX):
    """剔除时效过期 / 板块已满 / 主体冷门度超阈值（C 级）的候选，并截到目标条数。

    ctx: {now, max_age_hours, section_remaining: {板块: 剩余篇数}, allowed_sections}
    返回 (kept, dropped:list[(cand, reason)])。
    """
    ctx = ctx or {}
    now = ctx.get("now") or datetime.now(CST)
    max_age = ctx.get("max_age_hours", 72)
    remaining = ctx.get("section_remaining") or {}
    allowed = set(ctx.get("allowed_sections") or [])

    kept, dropped = [], []
    for c in (candidates or []):
        if not isinstance(c, dict):
            continue
        card = c.get("card") or c
        section = c.get("板块") or card.get("板块") or ""
        if allowed and section not in allowed:
            dropped.append((c, f"板块不在允许集({section})"))
            continue
        if remaining and section in remaining and remaining[section] <= 0:
            dropped.append((c, f"板块配比已满({section})"))
            continue
        life = card.get("生命周期")
        if life == "已过期":
            dropped.append((c, "时效过期"))
            continue
        age = _age_hours(card.get("发生时间"), now)
        if age is not None and age > max_age:
            dropped.append((c, f"超时效({age:.0f}h>{max_age}h)"))
            continue
        subj = card.get("主体") or c.get("主体") or ""
        if subj and awareness_of(subj) == "C":
            dropped.append((c, f"主体冷门度超阈值(C 级:{subj})"))
            continue
        kept.append(c)

    if len(kept) > target_max:
        kept.sort(key=lambda x: rule_total(x, None), reverse=True)
        dropped += [(c, "超出目标条数上限") for c in kept[target_max:]]
        kept = kept[:target_max]
    return kept, dropped


def _age_hours(occurred_at, now):
    try:
        dt = datetime.strptime((occurred_at or "")[:16], "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    except Exception:
        try:
            dt = datetime.strptime((occurred_at or "")[:10], "%Y-%m-%d").replace(tzinfo=CST)
        except Exception:
            return None
    return (now - dt).total_seconds() / 3600.0


# ------------------------------------------------------------
# L2 信号增强
# ------------------------------------------------------------
def l2_enrich(candidates, signals=None):
    """为候选补齐热度信号；缺失信号给保守默认值并标记 _signal_missing。

    signals: {主体 或 fact_id: {同题报道条数, 来源媒体权重, 社媒讨论量, 搜索指数}}
    返回带 `_signals` 的候选列表（原地补字段）。
    """
    signals = signals or {}
    for c in (candidates or []):
        card = c.get("card") or c
        key = card.get("id") or card.get("主体")
        sig = dict(signals.get(key) or signals.get(card.get("主体")) or {})
        missing = [k for k in ("同题报道条数", "来源媒体权重", "社媒讨论量", "搜索指数")
                   if k not in sig]
        # 来源权重可由来源名兜底推算
        if "来源媒体权重" not in sig:
            src = ""
            for s in (card.get("来源列表") or []):
                src = s.get("来源名", "") or src
            sig["来源媒体权重"] = _SOURCE_WEIGHT.get(src, _SOURCE_WEIGHT_DEFAULT)
            if "来源媒体权重" in missing:
                missing.remove("来源媒体权重")
        sig.setdefault("同题报道条数", 0)
        sig.setdefault("社媒讨论量", 0)
        sig.setdefault("搜索指数", 0)
        c["_signals"] = sig
        c["_signal_missing"] = missing
    return candidates


# ------------------------------------------------------------
# L3 排序：规则打分（兜底） + 模型打分
# ------------------------------------------------------------
def _dim_conflict(card):
    text = f"{card.get('主体','')}{card.get('动作','')}"
    hits = sum(1 for k in _CONFLICT_KW if k in text)
    angles = len(card.get("可用角度") or [])
    return _clamp(1 + hits + (1 if angles >= 3 else 0))


def _dim_fame(card):
    return {"A": 5, "B": 3, "C": 1}.get(awareness_of(card.get("主体", "")), 1)


def _dim_freshness(card, now=None):
    now = now or datetime.now(CST)
    age = _age_hours(card.get("发生时间"), now)
    if age is None:
        return 3
    if age < 0:
        return 5            # 未来赛事（赛程驱动）
    if age <= 24:
        return 5
    if age <= 72:
        return 3
    return 1


def _dim_extensibility(card, cand=None):
    n = len(card.get("可用角度") or [])
    base = 3 + (1 if n >= 3 else 0) + (1 if n >= 2 else 0)
    sig = (cand or {}).get("_signals") or {}
    if sig.get("同题报道条数", 0) >= 3:
        base += 1
    return _clamp(base)


def _dim_profile_fit(card):
    """账号粉丝画像匹配度：地域（上海/江苏/广东…）与怀旧题材加分（计划 13.5）。"""
    text = f"{card.get('主体','')}{card.get('动作','')}"
    score = 3
    if any(k in text for k in ("退役", "经典", "回顾", "昔日", "怀旧")):
        score += 1
    if any(k in text for k in ("上海", "申花", "海港", "江苏", "广东", "湖北", "四川", "成都")):
        score += 1
    return _clamp(score)


def _clamp(v, lo=1, hi=5):
    return max(lo, min(hi, int(round(v))))


def rule_score(card, cand=None, now=None):
    """规则版五维打分（L1 排序与 L4 降级共用）。返回 {维度: 分}。"""
    return {
        "冲突度": _dim_conflict(card),
        "人物知名度": _dim_fame(card),
        "时效性": _dim_freshness(card, now),
        "话题延展性": _dim_extensibility(card, cand),
        "粉丝画像匹配度": _dim_profile_fit(card),
    }


def rule_total(card, cand=None, now=None):
    return sum(rule_score(card, cand, now).values())


def llm_score_batch(candidates, *, now=None, call_llm=None):
    """L3 模型排序：对一批候选按五维量表打分（1-5）。

    call_llm(messages) -> (obj, model)；返回 {fact_id: {维度: 分}}。
    未提供 call_llm 时直接返回 {} （由调用方降级到规则排序）。
    """
    if not call_llm or not candidates:
        return {}, []
    items = [{"id": (c.get("card") or c).get("id", (c.get("card") or c).get("主体")),
              "主体": (c.get("card") or c).get("主体"),
              "动作": (c.get("card") or c).get("动作"),
              "信号": c.get("_signals", {})} for c in candidates]
    sys_prompt = ("你是头条号足球选题主编。按固定量表给每个选题打分，"
                  "每个维度 1-5 分（5 最高）：" + "、".join(DIMENSIONS) +
                  "。只输出 JSON：{\"scores\":[{\"id\":\"...\",\"冲突度\":n,\"人物知名度\":n,"
                  "\"时效性\":n,\"话题延展性\":n,\"粉丝画像匹配度\":n}]}")
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps({"候选": items}, ensure_ascii=False)}]
    try:
        obj, model = call_llm(messages)
    except Exception as e:
        return {}, [f"L3 模型打分失败，降级规则排序: {e}"]
    out = {}
    for row in (obj or {}).get("scores", []):
        cid = row.get("id")
        if cid:
            out[cid] = {d: _clamp(row.get(d, 3)) for d in DIMENSIONS}
    return out, [f"L3 模型打分完成（{model}）"]


# ------------------------------------------------------------
# 总入口：L1 → L2 → L3 → L4
# ------------------------------------------------------------
def rank_candidates(candidates, ctx=None, signals=None, *, use_llm=True,
                    call_llm=None, now=None, target_max=DEFAULT_TARGET_MAX,
                    token_budget=None):
    """分层择优。返回 {"ranking":[...], "mode": "llm"|"rule", "dropped":[...], "notes":[...]}。"""
    now = now or datetime.now(CST)
    notes = []

    # L1
    kept, dropped = l1_prefilter(candidates, {**(ctx or {}), "now": now}, target_max=target_max)
    notes.append(f"L1 预筛：{len(candidates or [])} → {len(kept)} 条（剔除 {len(dropped)}）")

    # L2
    kept = l2_enrich(kept, signals)

    # L4（判定换 L3 是否可用）
    cache = _load_cache(now)
    budget_ok = token_budget_ok(cache, token_budget)
    if not budget_ok:
        notes.append("L4 单日 token 超限，降级为规则排序")

    scores = {}
    mode = "rule"
    if use_llm and call_llm and budget_ok:
        to_judge = [c for c in kept if not _cache_hit(cache, (c.get("card") or c).get("id", ""), now)]
        for c in kept:                       # 命中缓存的直接复用
            cid = (c.get("card") or c).get("id", "")
            hit = _cache_hit(cache, cid, now)
            if hit:
                scores[cid] = {d: hit["scores"][d] for d in DIMENSIONS if d in hit.get("scores", {})}
        if to_judge:
            new_scores, llm_notes = llm_score_batch(to_judge, now=now, call_llm=call_llm)
            notes += llm_notes
            for cid, sc in new_scores.items():
                scores[cid] = sc
                cache["judged"][cid] = {"scores": sc, "at": now.strftime("%Y-%m-%d %H:%M"),
                                        "total": sum(sc.values())}
            cache["tokens_used"] = cache.get("tokens_used", 0) + len(to_judge) * 300
            _save_cache(cache)
        if scores:
            mode = "llm"
            notes.append(f"L4 缓存：本次直接判定 {len(to_judge)} 条，命中缓存 {len(kept)-len(to_judge)} 条")
    else:
        if use_llm and not call_llm:
            notes.append("未提供模型调用，使用规则排序")

    # 汇总打分（模型分优先，缺则规则分）
    ranking = []
    for c in kept:
        card = c.get("card") or c
        cid = card.get("id", card.get("主体"))
        dims = scores.get(cid) or rule_score(card, c, now)
        total = sum(dims.values())
        ranking.append({"id": cid, "主体": card.get("主体"), "板块": c.get("板块") or card.get("板块"),
                        "维度": dims, "总分": total})
    ranking.sort(key=lambda r: r["总分"], reverse=True)
    return {"ranking": ranking, "mode": mode, "dropped": [d[1] for d in dropped], "notes": notes}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", help="候选 JSON 文件（事实卡数组）")
    args = ap.parse_args()
    cands = []
    if args.candidates and Path(args.candidates).exists():
        cands = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
        cands = [{"card": c} for c in cands]
    res = rank_candidates(cands, use_llm=False)
    print(json.dumps(res, ensure_ascii=False, indent=2))
