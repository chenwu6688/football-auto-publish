#!/usr/bin/env python3
"""赛程库 + 选题池生成器（计划第一阶段核心）

设计要点（对照《足球自媒体自动化选题与内容生产完善计划》）：
  1. 一级·确定性供给：赛程 / 赛季节点，占内容供给 30%，提前 7 天可预生成。
  2. **只产冲突点选题，不产「XX 前瞻」** —— 赛事前瞻已停发（欧冠 78 篇仅 852 阅读的教训）。
  3. 认知度分级过滤：A 级进新闻流首选，B 级需搭配冲突点，C 级降权仅用于数据/榜单类。
  4. 时效闸门：每条选题带 expires_at，过期不得进入发布流。
  5. 落库缓存：业务侧只读 data/fixtures/topic_pool.json，不直连 API。

上游数据：football-data.org v4（免费层限制：单次查询窗口 ≤ 10 天，多赛事可合并）
  用法：python3 scripts/fixture_library.py [--days 10] [--refresh]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CST = timezone(timedelta(hours=8))

# 可拉取的赛事（免费层实测可用）。欧联 / 中超 / 亚冠 / 国足 不在免费层，
# 需 API-Football 或聚合数据补齐 —— 见计划 13.2 数据源四源分工。
FIXTURE_COMPETITIONS = {
    "2021": "英超",
    "2014": "西甲",
    "2019": "意甲",
    "2002": "德甲",
    "2015": "法甲",
    "2001": "欧冠",
}

# 赛事权重：欧冠 > 五大联赛（决定选题优先级）
COMPETITION_WEIGHT = {"欧冠": 1.0, "英超": 0.95, "西甲": 0.9, "意甲": 0.85, "德甲": 0.85, "法甲": 0.8}

# 窗口上限：免费层硬限制，超过会返回 400 "Specified period must not exceed 10 days"
MAX_WINDOW_DAYS = 10

FIXTURE_DIR = PROJECT_ROOT / "data" / "fixtures"
ENTITY_MAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"

# 冲突点分型 → (认知度组合, 建议栏目, 角度模板)。
# 刻意不含「前瞻」类 —— 前瞻已停发（欧冠 78 篇仅 852 阅读）。
# 模板占位符：{home} {away} {lower} {higher}
ANGLE_RULES = [
    ("AA", "强强对话", "战术榜单", "{home}与{away}正面对撞，胜负手在细节而非实力差"),
    ("AB", "强弱反差", "人物故事", "{lower}的针对性打法，是{higher}这场最大的变数"),
    ("AC", "冷门窗口", "战术榜单", "{higher}的账面优势，掩盖了{lower}的哪些变量"),
    ("BB", "中游缠斗", "战术榜单", "{home}与{away}的较量，牵动的是排名与赛季走向"),
    ("BC", "关注度落差", "数据对照", "同一轮次里的两种境遇：{higher}与{lower}"),
]


def load_entity_map() -> dict:
    if not ENTITY_MAP_PATH.exists():
        raise SystemExit(
            f"❌ 缺少中文实体映射表 {ENTITY_MAP_PATH}\n"
            "   先运行实体映射生成脚本，否则返回的球队名将是英文，无法进入生成流。"
        )
    with open(ENTITY_MAP_PATH, encoding="utf-8") as f:
        data = json.load(f)
    by_en = {}
    for e in data["entities"].values():
        by_en[e["name_en"].lower()] = e
        for a in e.get("aliases", []):
            by_en.setdefault(a.lower(), e)   # 别名兜底（英文别名）
    return {"raw": data, "by_en": by_en}


def resolve_team(name_en: str, emap: dict) -> dict | None:
    """英文队名 → 中文实体。未登记实体返回 None（打回，不进生成）。"""
    if not name_en:
        return None
    return emap["by_en"].get(name_en.strip().lower())


def fetch_window(days: int, emap: dict, refresh: bool = False) -> list[dict]:
    """拉取 today ~ today+days-1 的赛程，带当日缓存。"""
    days = min(days, MAX_WINDOW_DAYS)
    today = datetime.now(CST)
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=days - 1)).strftime("%Y-%m-%d")
    cache_path = FIXTURE_DIR / f"raw_{date_from}.json"

    if cache_path.exists() and not refresh:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        print(f"📦 使用当日缓存 {cache_path.name}（{cached['resultSet']['count']} 场）")
        print("   （如需强制刷新加 --refresh）")
        return cached.get("matches", [])

    key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    if not key or key == "***":
        raise SystemExit(
            "❌ 未设置 FOOTBALL_DATA_KEY。\n"
            "   本地：source scripts/local_env.sh 或 export FOOTBALL_DATA_KEY=...\n"
            "   CI：GitHub Actions Secrets 注入。"
        )

    import requests

    url = "https://api.football-data.org/v4/matches"
    params = {
        "competitions": ",".join(FIXTURE_COMPETITIONS.keys()),
        "dateFrom": date_from,
        "dateTo": date_to,
    }
    print(f"📅 拉取赛程 {date_from} ~ {date_to}（{len(FIXTURE_COMPETITIONS)} 个赛事）...")
    resp = requests.get(url, params=params, headers={"X-Auth-Token": key}, timeout=20)

    if resp.status_code == 403:
        raise SystemExit("❌ 403：token 无效，或该赛事在当前赛季已结束（免费层会直接 403）")
    if resp.status_code == 429:
        raise SystemExit("❌ 429：超出免费层 10 次/分钟限额，请稍后重试")
    resp.raise_for_status()

    payload = resp.json()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    avail = resp.headers.get("x-requests-available-minute", "?")
    print(f"✅ {payload['resultSet']['count']} 场，已缓存 {cache_path.name}（本分钟剩余额度 {avail}）")
    return payload.get("matches", [])


def build_topic_pool(matches: list[dict], emap: dict) -> dict:
    """赛程 → 冲突点选题池。刻意不生成「前瞻」类标题。"""
    topics, dropped_unmapped, dropped_lowaware = [], [], []

    for m in matches:
        home = resolve_team(m.get("homeTeam", {}).get("name", ""), emap)
        away = resolve_team(m.get("awayTeam", {}).get("name", ""), emap)
        if not home or not away:
            missing = m.get("homeTeam", {}).get("name") if not home else m.get("awayTeam", {}).get("name")
            dropped_unmapped.append({"match_id": m.get("id"), "unmapped": missing})
            continue

        comp = COMPETITION_WEIGHT.get(
            next((v for k, v in FIXTURE_COMPETITIONS.items() if k == str((m.get("competition") or {}).get("id"))), ""),
            0.5,
        )
        comp_name = next(
            (v for k, v in FIXTURE_COMPETITIONS.items() if k == str((m.get("competition") or {}).get("id"))),
            (m.get("competition") or {}).get("name", ""),
        )

        aw_pair = (home["awareness"], away["awareness"])
        n_c = aw_pair.count("C")
        if n_c == 2:
            # 双方都是低认知 → 不进新闻流，只保留为背景数据（认知度 C 降权）
            dropped_lowaware.append({"match_id": m.get("id"), "pair": f"{home['name_zh']} vs {away['name_zh']}"})
            continue

        code = "".join(sorted(aw_pair))          # A/B/C 排序后组成 key
        rule = next((r for r in ANGLE_RULES if r[0] == code), None)
        if rule is None:
            dropped_lowaware.append({"match_id": m.get("id"), "pair": f"{home['name_zh']} vs {away['name_zh']}", "code": code})
            continue
        _, angle_type, column, template = rule
        # 认知度较低的一方 = lower（A<B<C 认知度递减，取字母序较大的）
        lower = home if home["awareness"] > away["awareness"] else away
        higher = away if lower is home else home
        conflict = template.format(
            home=home["name_zh"], away=away["name_zh"],
            lower=lower["name_zh"], higher=higher["name_zh"],
        )

        kickoff = m.get("utcDate", "")
        try:
            dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(CST)
        except Exception:
            continue

        # 优先级：认知度 + 赛事权重 + 时效
        awareness_score = {"A": 40, "B": 22, "C": 8}
        base = (awareness_score[home["awareness"]] + awareness_score[away["awareness"]]) / 2
        hours_to_kickoff = max(0.0, (dt - datetime.now(CST)).total_seconds() / 3600)
        freshness = max(0.0, 10 - hours_to_kickoff / 24)     # 越临近越热
        priority = round(base * 1.4 + comp * 20 + freshness, 1)

        topics.append({
            "topic_id": f"fx-{m.get('id')}",
            "source_layer": "确定性",                 # 一级供给（赛程驱动）
            "match_id": m.get("id"),
            "competition": comp_name,
            "kickoff_cst": dt.strftime("%Y-%m-%d %H:%M"),
            "kickoff_weekday": "一二三四五六日"[dt.weekday()],
            "home_zh": home["name_zh"],
            "away_zh": away["name_zh"],
            "awareness_pair": f"{home['awareness']}/{away['awareness']}",
            "angle_type": angle_type,
            "conflict": conflict,
            "suggested_column": column,
            "priority": priority,
            "status": m.get("status"),
            # 时效闸门：赛程类选题必须在开赛前发布。开赛后再发＝旧闻新发（扣 10 分）
            "publish_by": dt.strftime("%Y-%m-%d %H:%M"),
            "fact_source": {"api": "football-data.org/v4", "match_id": m.get("id"), "fd_comp_id": (m.get("competition") or {}).get("id")},
        })

    topics.sort(key=lambda t: -t["priority"])

    by_weekday: dict[str, int] = {}
    for t in topics:
        by_weekday[t["kickoff_weekday"]] = by_weekday.get(t["kickoff_weekday"], 0) + 1

    return {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "window_days": MAX_WINDOW_DAYS,
        "source": "football-data.org v4 /matches（多赛事合并，单次调用）",
        "stats": {
            "matches_total": len(matches),
            "topics": len(topics),
            "dropped_unmapped": len(dropped_unmapped),
            "dropped_low_awareness": len(dropped_lowaware),
            "by_angle": {k: sum(1 for t in topics if t["angle_type"] == k) for k in {r[1] for r in ANGLE_RULES} if any(t["angle_type"] == k for t in topics)},
            "by_weekday": by_weekday,
        },
        "coverage_gap": ["欧联", "中超", "亚冠", "国足"],   # 免费层不含，待 API-Football / 聚合数据补齐
        "topics": topics,
        "_dropped": {"unmapped": dropped_unmapped[:20], "low_awareness": dropped_lowaware[:20]},
    }


def main():
    ap = argparse.ArgumentParser(description="赛程库 + 选题池生成器")
    ap.add_argument("--days", type=int, default=MAX_WINDOW_DAYS, help=f"向前天数（上限 {MAX_WINDOW_DAYS}）")
    ap.add_argument("--refresh", action="store_true", help="忽略当日缓存，强制重新拉取")
    args = ap.parse_args()

    emap = load_entity_map()
    print(f"📖 中文实体映射：{emap['raw']['stats']['total']} 个实体"
          f"（A={emap['raw']['stats']['A']} B={emap['raw']['stats']['B']} C={emap['raw']['stats']['C']}）")

    matches = fetch_window(args.days, emap, refresh=args.refresh)
    pool = build_topic_pool(matches, emap)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / "topic_pool.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

    s = pool["stats"]
    print()
    print(f"🗂  选题池已生成 → {out.relative_to(PROJECT_ROOT)}")
    print(f"   赛程 {s['matches_total']} 场 → 选题 {s['topics']} 条"
          f"（未映射打回 {s['dropped_unmapped']}，低认知过滤 {s['dropped_low_awareness']}）")
    print(f"   分型：{s['by_angle']}")
    print(f"   按星期：{s['by_weekday']}")
    print(f"   ⚠️ 免费层未覆盖：{'、'.join(pool['coverage_gap'])}")
    print()
    print("   Top 8 选题：")
    for t in pool["topics"][:8]:
        print(f"     [{t['priority']:5.1f}] {t['kickoff_cst'][:16]} {t['competition']:3s} "
              f"{t['home_zh']} vs {t['away_zh']}  <{t['angle_type']}>")
        print(f"            └ {t['conflict']}")


if __name__ == "__main__":
    main()
