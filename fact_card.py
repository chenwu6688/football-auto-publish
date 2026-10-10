#!/usr/bin/env python3
"""事实卡（Fact Card）——计划 6.1 / 12.2

把素材编译为结构化、可落库、可溯源的事实卡，是择优、溯源、生命周期与
调参归因的共同底座。

字段（严格对齐计划 6.1）：
    id / 主体 / 动作 / 数值 / 发生时间 / 来源列表 / 可信度 / 可用角度
扩展字段（计划 12.2 生命周期）：
    生命周期 = 进行中 / 已收官 / 已过期

可信度三级阈值（计划 3.2）：
    来源 ≥2 个 → 已确认（可作陈述句）
    来源 =1 个 → 单源（须写「据某媒体报道」）
    来源 =0 个 → 传闻（须写「传闻」「有消息称」）

落库位置：data/facts/fact_cards.json（运行期资产，已 gitignore）
"""
import re
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from constants import PROJECT_ROOT
from utils import CST

FACT_DIR = PROJECT_ROOT / "data" / "facts"
FACT_STORE = FACT_DIR / "fact_cards.json"

# 生命周期阈值（计划 12.2）：事件发生多久后从「进行中 → 已收官 → 已过期」
FACT_SETTLE_DAYS = 7     # 超过 7 天：已收官
FACT_EXPIRE_DAYS = 30    # 超过 30 天：已过期

CONF_CONFIRMED = "已确认"
CONF_SINGLE = "单源"
CONF_RUMOR = "传闻"

LC_ONGOING = "进行中"
LC_SETTLED = "已收官"
LC_EXPIRED = "已过期"

# 动作词 → 规范动作（计划 6.1 动作枚举：转会、续约、伤停、表态、处罚）
_ACTION_RULES = [
    ("转会", ("转会", "加盟", "引进", "签下", "官宣", "买断", "免签", "离队", "租借", "报价", "绯闻")),
    ("续约", ("续约", "续签", "新合同", "长约")),
    ("伤停", ("伤病", "受伤", "伤停", "报销", "缺阵", "手术", "拉伤", "骨折")),
    ("表态", ("表示", "表态", "声称", "回应", "直言", "喊话", "透露")),
    ("处罚", ("处罚", "禁赛", "罚款", "指控", "调查", "红牌", "停赛")),
]
_ACTION_DEFAULT = "其他"

# 可作「主体」的知名实体（与一致性校验同源，保守集合）
_ENTITY_HINTS = (
    "曼城", "曼联", "利物浦", "切尔西", "阿森纳", "热刺", "纽卡", "埃弗顿", "西汉姆", "维拉",
    "皇马", "巴萨", "马竞", "塞维利亚", "瓦伦西亚", "毕尔巴鄂", "国米", "米兰", "尤文", "那不勒斯",
    "罗马", "拉齐奥", "拜仁", "多特", "勒沃库森", "莱比锡", "法兰克福", "巴黎", "马赛", "里昂",
    "国足", "申花", "海港", "国安", "泰山", "蓉城", "浙江", "三镇", "亚泰", "玉昆",
    "梅西", "C罗", "姆巴佩", "哈兰德", "内马尔", "贝林厄姆", "凯恩", "萨拉赫",
)


def _now(now=None):
    return now or datetime.now(CST)


def _iso(dt):
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M") if isinstance(dt, datetime) else (dt or "")


def make_card_id(subject, action, value=None, occurred_at=None):
    """稳定 id：同一（主体+动作+数值+日期）跨源合并为一张卡。"""
    day = (occurred_at or "")[:10] if isinstance(occurred_at, str) else _iso(occurred_at)[:10]
    raw = f"{subject}|{action}|{value}|{day}"
    return "fact_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def derive_confidence(sources):
    """三级可信度：≥2 独立来源→已确认；单源→单源；无源→传闻（计划 3.2）。"""
    names = {(s.get("来源名") or s.get("source") or "").strip()
             for s in (sources or []) if isinstance(s, dict)}
    names.discard("")
    if len(names) >= 2:
        return CONF_CONFIRMED
    if len(names) == 1:
        return CONF_SINGLE
    return CONF_RUMOR


def infer_action(text):
    """从文本推断规范动作（转会/续约/伤停/表态/处罚）。"""
    t = text or ""
    for action, kws in _ACTION_RULES:
        if any(k in t for k in kws):
            return action
    return _ACTION_DEFAULT


def infer_subject(text):
    """从文本抓取主体（知名实体优先，其次「XX队/俱乐部」式短语）。"""
    t = text or ""
    for e in _ENTITY_HINTS:
        if e in t:
            return e
    m = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,8}(?:队|俱乐部|主帅|教练|球星))", t)
    return m.group(1) if m else ""


def infer_value(text):
    """抓取数值（金额/年限/比分/排名/年龄等），无则 None。"""
    m = re.search(r"(\d+(?:\.\d+)?\s*(?:亿|万)?\s*(?:欧元|英镑|镑|万|年|岁|%|分|球|名|位)?)", text or "")
    return m.group(1).strip() if m else None


def build_fact_card(subject, action, value=None, occurred_at=None, sources=None,
                    angles=None, now=None):
    """构建一张事实卡（严格对齐计划 6.1 字段 + 12.2 生命周期）。"""
    now = _now(now)
    sources = list(sources or [])
    for s in sources:
        s.setdefault("抓取时间", _iso(now))
    card = {
        "id": make_card_id(subject, action, value, occurred_at),
        "主体": subject or "",
        "动作": action or _ACTION_DEFAULT,
        "数值": value,
        "发生时间": _iso(occurred_at) if isinstance(occurred_at, datetime) else (occurred_at or ""),
        "来源列表": sources,
        "可信度": derive_confidence(sources),
        "可用角度": list(angles) if angles else suggest_angles(action, subject),
        "生命周期": LC_ONGOING,
        "updated_at": _iso(now),
    }
    card["生命周期"] = compute_lifecycle(card, now)
    return card


def compute_lifecycle(card, now=None):
    """计划 12.2：进行中 / 已收官 / 已过期。无法判定时间时按「进行中」保守处理。"""
    now = _now(now)
    raw = (card or {}).get("发生时间") or ""
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    except Exception:
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=CST)
        except Exception:
            return LC_ONGOING
    age_days = (now - dt).total_seconds() / 86400.0
    if age_days < 0:            # 未来事件（赛程等）
        return LC_ONGOING
    if age_days > FACT_EXPIRE_DAYS:
        return LC_EXPIRED
    if age_days > FACT_SETTLE_DAYS:
        return LC_SETTLED
    return LC_ONGOING


def suggest_angles(action, subject):
    """由动作派生 2-3 个可用角度（计划 6.1 可用角度）。"""
    s = subject or "当事人"
    table = {
        "转会": [f"{s}为什么值这个价", f"{s}转会后谁最受伤", f"{s}这笔交易划算吗"],
        "续约": [f"{s}续约背后的博弈", f"{s}续约对球队意味着什么", f"{s}留队是双赢吗"],
        "伤停": [f"{s}缺阵谁来补位", f"{s}这次伤停影响多大", f"{s}的伤病史说明什么"],
        "表态": [f"{s}这番话几个意思", f"{s}表态是否另有目的", f"{s}为何此时发声"],
        "处罚": [f"{s}被罚冤不冤", f"{s}处罚力度是否合理", f"{s}事件还有多少隐情"],
    }
    return table.get(action, [f"{s}这件事的关键在哪"])


def upsert_cards(store, new_cards, now=None):
    """按 id 合并入库：合并来源、刷新可信度与生命周期。返回 (store, added, updated)。"""
    now = _now(now)
    store = store or {}
    idx = {c.get("id"): c for c in store.get("cards", [])}
    added = updated = 0
    for c in (new_cards or []):
        cid = c.get("id")
        if not cid:
            continue
        if cid in idx:
            old = idx[cid]
            seen = {(s.get("地址") or s.get("来源名") or "") for s in old.get("来源列表", [])}
            for s in c.get("来源列表", []):
                key = s.get("地址") or s.get("来源名") or ""
                if key and key not in seen:
                    old.setdefault("来源列表", []).append(s)
                    seen.add(key)
            old["可信度"] = derive_confidence(old.get("来源列表"))
            if not old.get("可用角度"):
                old["可用角度"] = c.get("可用角度", [])
            old["生命周期"] = compute_lifecycle(old, now)
            old["updated_at"] = _iso(now)
            updated += 1
        else:
            idx[cid] = c
            added += 1
    store["cards"] = list(idx.values())
    store["updated_at"] = _iso(now)
    return store, added, updated


def load_store(path=None):
    p = Path(path or FACT_STORE)
    if not p.exists():
        return {"cards": [], "updated_at": ""}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"cards": [], "updated_at": ""}


def save_store(store, path=None):
    p = Path(path or FACT_STORE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def refresh_lifecycles(store, now=None):
    """批量刷新生命周期（数据回流时调用，让过期事实卡下沉）。"""
    now = _now(now)
    for c in (store or {}).get("cards", []):
        c["生命周期"] = compute_lifecycle(c, now)
    return store


def summarize_store(store):
    """统计：按可信度 / 生命周期分布。"""
    cards = (store or {}).get("cards", [])
    conf, life = {}, {}
    for c in cards:
        conf[c.get("可信度", "?")] = conf.get(c.get("可信度", "?"), 0) + 1
        life[c.get("生命周期", "?")] = life.get(c.get("生命周期", "?"), 0) + 1
    return {"total": len(cards), "可信度": conf, "生命周期": life}


# ------------------------------------------------------------
# 生产者：从采集素材编译事实卡
# ------------------------------------------------------------
def cards_from_fixtures(fixtures, date_str=None, now=None):
    """比赛素材 → 事实卡（比分事实，双源可提升可信度）。"""
    now = _now(now)
    cards = []
    for f in (fixtures or []):
        if not isinstance(f, dict):
            continue
        home, away = f.get("home_team", ""), f.get("away_team", "")
        hs, as_ = f.get("home_score"), f.get("away_score")
        if not home or not away or hs is None or as_ is None:
            continue
        subject = f"{home} vs {away}"
        value = f"{hs}-{as_}"
        sources = []
        if f.get("source"):
            sources.append({"来源名": f["source"], "地址": f.get("source_url", "")})
        if f.get("data_confidence") == "high":
            # 双源一致：补一条独立来源，使可信度升为「已确认」
            sources.append({"来源名": f"{f.get('source','media')}-交叉校验",
                            "地址": f.get("source_url", "")})
        occurred = f.get("utc_date") or date_str or _iso(now)[:10]
        card = build_fact_card(subject, "比赛结果", value, occurred, sources,
                               suggest_angles("其他", subject), now)
        cards.append(card)
    return cards


def cards_from_news(items, date_str=None, now=None):
    """新闻/转会素材 → 事实卡（标题+来源，可溯源）。"""
    now = _now(now)
    cards = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        action = infer_action(title)
        subject = infer_subject(title)
        if not subject:
            continue
        card = build_fact_card(
            subject, action, infer_value(title),
            it.get("published_at") or date_str or _iso(now)[:10],
            [{"来源名": it.get("source", "media"), "地址": it.get("url", "")}],
            suggest_angles(action, subject), now)
        cards.append(card)
    return cards


def compile_and_persist(match_data=None, news_items=None, date_str=None,
                        store_path=None, now=None):
    """把素材编译为事实卡并并入落库文件。返回 (store, stats)。"""
    now = _now(now)
    match_data = match_data or {}
    cards = []
    cards += cards_from_fixtures(match_data.get("all_fixtures", []), date_str, now)
    news = list(news_items or []) + list(match_data.get("news_articles", [])) \
        + list(match_data.get("transfer_news", []))
    cards += cards_from_news(news, date_str, now)
    store = load_store(store_path)
    store, added, updated = upsert_cards(store, cards, now)
    store = refresh_lifecycles(store, now)
    save_store(store, store_path)
    stats = summarize_store(store)
    stats.update({"added": added, "updated": updated, "compiled": len(cards)})
    return store, stats


if __name__ == "__main__":
    import sys
    md = {}
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        md = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    store, stats = compile_and_persist(md, date_str=datetime.now(CST).strftime("%Y-%m-%d"))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
