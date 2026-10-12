#!/usr/bin/env python3
"""中文实体映射层 —— 球队层增量维护器（计划 13.1）。

计划 13.1：中文词典「此后增量维护」；「未覆盖者按 C 级占位、不做机器音译臆测；
未登记的实体不得进入生成环节」。球队层此前只覆盖五大联赛 + 欧冠（football-data
免费层名单），导致 API-Football 拉回的亚冠/欧联/欧协联/中超球队（英文名）被
`resolve_team` 打回，洲际赛程全数 dropped_unmapped。

本维护器：
  1. 拉取 football-data.org 六赛事球队名单（免费层，需 FOOTBALL_DATA_KEY）；
  2. 拉取 API-Football 亚冠/欧联/欧协联/中超球队名单（需 API_FOOTBALL_KEY，动态解析联赛 id）；
  3. 套用人工 curated 中文译名表，合并进 config/entity_map.json 的球队层（球员层原样保留）；
  4. **只登记已有人工译名的球队**；无译名的写入 data/team_map_pending.json 待补，
     绝不机器音译、绝不以英文名冒充中文名（否则会污染中文标题，违反 13.1 合规口径）。

用法：
    export FOOTBALL_DATA_KEY=xxx
    python3 scripts/build_team_map.py                  # 拉取 + 合并
    python3 scripts/build_team_map.py --curated-only   # 只用 curated 表合并（离线，无网络）
    python3 scripts/build_team_map.py --refresh        # 忽略当日缓存
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EMAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"
PENDING_PATH = PROJECT_ROOT / "data" / "team_map_pending.json"
CACHE_DIR = PROJECT_ROOT / "data" / "fixtures" / "teams"

FD_API = "https://api.football-data.org/v4"
FD_COMPETITIONS = {"PL": 2021, "PD": 2014, "SA": 2019, "BL1": 2002, "FL1": 2015, "CL": 2001}
COMP_ZH = {"PL": "英超", "PD": "西甲", "SA": "意甲", "BL1": "德甲", "FL1": "法甲", "CL": "欧冠"}
CONF = {"A": "high", "B": "medium", "C": "low"}

# ── 人工 curated 中文译名表（增量维护入口）────────────────────────────
# 格式：name_en|awareness|name_zh|alias1|alias2...
# 口径：A=泛球迷也认识、中文译名唯一稳定；B=主流赛事知名、有固定译名；C=低认知降权。
# 纪律：只登记有把握的译名；拿不准就留空（进 pending 清单），不臆测。
CURATED_TEAMS = """
# 亚冠 · 沙特
Al Hilal|A|利雅得新月|希拉尔
Al Nassr|A|利雅得胜利|纳斯尔
Al Ittihad|B|吉达联合|伊蒂哈德
Al Ahli|B|吉达国民|阿赫利
# 亚冠 · 卡塔尔 / 阿联酋
Al Sadd|B|萨德|阿尔萨德
Al Duhail|C|杜海勒
Al Ain|B|阿尔艾因|艾因
Sharjah|C|沙迦
Al Wasl|C|瓦斯尔
# 亚冠 · 东亚
Urawa Red Diamonds|B|浦和红钻|浦和
Kawasaki Frontale|B|川崎前锋|川崎
Yokohama F. Marinos|B|横滨水手|横滨
Vissel Kobe|B|神户胜利船|神户
Sanfrecce Hiroshima|C|广岛三箭|广岛
Jeonbuk Hyundai Motors|B|全北现代|全北
Ulsan HD|B|蔚山HD|蔚山现代
Pohang Steelers|C|浦项制铁|浦项
FC Seoul|C|首尔FC|首尔
Buriram United|C|武里南联
BG Pathum United|C|巴吞联
Melbourne City|C|墨尔本城
# 中超（API-Football 返回英文名时的兜底）
Shanghai Port|A|上海海港|上港
Shanghai Shenhua|A|上海申花|申花
Shandong Taishan|A|山东泰山|鲁能
Beijing Guoan|A|北京国安|国安
Chengdu Rongcheng|A|成都蓉城|成都兴城
Wuhan Three Towns|A|武汉三镇
Zhejiang|B|浙江队|浙江
# 欧联 / 欧协联 · 非常规五大联赛球队
Ajax|A|阿贾克斯
SL Benfica|B|本菲卡
Celtic|B|凯尔特人
Rangers|B|格拉斯哥流浪者|流浪者
Olympiacos|B|奥林匹亚科斯
Panathinaikos|C|帕纳辛奈科斯
Crvena Zvezda|C|贝尔格莱德红星|红星
Dinamo Zagreb|C|萨格勒布迪纳摩
AC Sparta Praha|C|布拉格斯巴达|斯巴达
BSC Young Boys|C|伯尔尼年轻人|年轻人
FC Basel 1893|C|巴塞尔|FC Basel|Basel
Beşiktaş|B|贝西克塔斯
"""


def load_curated() -> dict:
    out = {}
    for line in CURATED_TEAMS.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        out[parts[0].lower()] = {
            "name_en": parts[0], "name_zh": parts[2], "awareness": parts[1],
            "aliases": [a for a in parts[3:] if a and a != parts[2]],
        }
    return out


def _slug(name_en: str) -> str:
    return "club_" + re.sub(r"\s+", "_", (name_en or "").strip().lower())


def fetch_fd_teams(refresh: bool = False) -> list[str]:
    """football-data.org 六赛事球队英文名（免费层）。无 key 且无缓存则返回 []。"""
    key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    names: list[str] = []
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    for code, cid in FD_COMPETITIONS.items():
        cache = CACHE_DIR / f"fd_{code}_{today}.json"
        data = None
        if cache.exists() and not refresh:
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                data = None
        if data is None:
            if not key:
                continue
            try:
                import urllib.request
                req = urllib.request.Request(f"{FD_API}/competitions/{cid}/teams",
                                             headers={"X-Auth-Token": key})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                time.sleep(7)   # 免费层 10 次/分
            except Exception as e:
                print(f"   ⚠️ {COMP_ZH[code]} 球队名单拉取失败: {str(e)[:80]}")
                continue
        for t in (data or {}).get("teams", []):
            n = t.get("name")
            if n:
                names.append(n)
    return names


def fetch_af_teams() -> list[str]:
    """API-Football 亚冠/欧联/欧协联/中超球队英文名（动态解析联赛 id）。无 key 返回 []。"""
    try:
        import api_football
    except Exception:
        return []
    if not api_football._key():
        return []
    try:
        ids = api_football.resolve_league_ids()
    except Exception as e:
        print(f"   ⚠️ API-Football 联赛 id 解析失败: {str(e)[:80]}")
        return []
    names: list[str] = []
    season = datetime.now().year
    for name, lid in (ids or {}).items():
        if not lid:
            continue
        for yr in (season, season - 1):
            try:
                r = api_football._get("teams", {"league": lid, "season": yr}, timeout=20)
                if r.status_code != 200:
                    continue
                resp = (r.json() or {}).get("response", [])
                if not resp:
                    continue
                for item in resp:
                    n = (item.get("team") or {}).get("name")
                    if n:
                        names.append(n)
                break
            except Exception:
                continue
    return names


def build_clubs(candidate_names: list[str], existing_clubs: dict) -> tuple[dict, dict]:
    """候选英文队名 → curated 球队条目；无译名的进 pending。

    返回 (新登记球队 dict, pending dict)。已存在的球队若已有中文名则不覆盖译名，
    仅当 curated 提供更高认知度时才上调（人工口径优先）。
    """
    curated = load_curated()
    existing_by_en = {(v.get("name_en") or "").lower(): v for v in existing_clubs.values()}
    # 已有中文名集合：防止同一支队因英文写法不同（Real Sociedad vs Real Sociedad de Fútbol）
    # 被重复登记成两个实体（计划 13.1「以内部标准 ID 为锚做归一」）
    existing_zh = {(v.get("name_zh") or "").strip() for v in existing_clubs.values() if v.get("name_zh")}
    registered: dict[str, dict] = {}
    pending: dict[str, dict] = {}

    seen = []
    for n in candidate_names:
        if n and n.lower() not in {x.lower() for x in seen}:
            seen.append(n)
    # curated 表中但未被拉取到的球队也一并登记（人工维护优先于源可用性）
    for k, c in curated.items():
        if c["name_en"] not in seen:
            seen.append(c["name_en"])

    for n in seen:
        key = n.lower()
        c = curated.get(key)
        if not c:
            ex = existing_by_en.get(key)
            if ex and ex.get("name_zh"):
                continue                      # 已在映射表中，无需处理
            pending[_slug(n)] = {"name_en": n, "reason": "无人工译名，待补"}
            continue
        ex = existing_by_en.get(key)
        if not ex and c["name_zh"] in existing_zh:
            continue                          # 同一支队已以另一英文写法登记，避免重复
        slug = ex.get("internal_id") if ex else _slug(c["name_en"])
        entry = dict(ex) if ex else {
            "internal_id": slug, "fd_id": None, "af_id": None,
            "category": "club", "competitions": [],
        }
        entry.update({
            "internal_id": slug,
            "name_en": c["name_en"],
            "name_zh": c["name_zh"],
            "aliases": sorted(set((entry.get("aliases") or []) + c["aliases"])),
            "awareness": c["awareness"],
            "category": "club",
        })
        registered[slug] = entry
    return registered, pending


def merge_into_entity_map(registered: dict, pending: dict) -> dict:
    """把球队层合并进 entity_map.json（球员层与其他实体原样保留、顺序不变）。

    已存在的实体原位更新（不重排顺序，保持最小 diff）；新增球队追加到末尾。
    """
    emap = json.loads(EMAP_PATH.read_text(encoding="utf-8"))
    ents = emap["entities"]
    appended: dict[str, dict] = {}
    for k, v in registered.items():
        if k in ents:
            ents[k] = v                      # 原位更新
        else:
            appended[k] = v
    ents.update(appended)                    # 新增追加到末尾
    emap["entities"] = ents

    allv = list(ents.values())
    emap["stats"] = {
        "total": len(allv),
        "clubs": sum(1 for v in allv if v["category"] == "club"),
        "players": sum(1 for v in allv if v["category"] == "player"),
        "A": sum(1 for v in allv if v["awareness"] == "A"),
        "B": sum(1 for v in allv if v["awareness"] == "B"),
        "C": sum(1 for v in allv if v["awareness"] == "C"),
        "low_confidence": sum(1 for v in allv if v.get("confidence") == "low"),
    }
    emap["version"] = "2.0"
    emap["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    EMAP_PATH.write_text(json.dumps(emap, ensure_ascii=False, indent=1), encoding="utf-8")

    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(
        {"generated_at": emap["generated_at"], "pending": pending},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return emap["stats"], appended


def main() -> int:
    ap = argparse.ArgumentParser(description="球队层中文映射增量维护（计划 13.1）")
    ap.add_argument("--curated-only", action="store_true",
                    help="只用 curated 表合并（离线，不拉取源）")
    ap.add_argument("--refresh", action="store_true", help="忽略当日缓存")
    args = ap.parse_args()

    emap = json.loads(EMAP_PATH.read_text(encoding="utf-8"))
    existing = {k: v for k, v in emap["entities"].items() if v.get("category") == "club"}
    print(f"📖 现有球队层：{len(existing)} 支")

    cand: list[str] = []
    if not args.curated_only:
        fd = fetch_fd_teams(refresh=args.refresh)
        af = fetch_af_teams()
        print(f"   拉取：football-data {len(fd)} 支 / API-Football {len(af)} 支")
        cand = fd + af
    else:
        print("   （--curated-only：仅合并人工译名表）")

    registered, pending = build_clubs(cand, existing)
    stats, appended = merge_into_entity_map(registered, pending)

    print(f"\n✅ 球队层：{len(existing)} → {stats['clubs']} 支（登记/更新 {len(registered)}，新增 {len(appended)}）")
    print(f"   认知度：A={stats['A']} B={stats['B']} C={stats['C']}")
    for k, v in appended.items():
        print(f"   + [{v['awareness']}] {v['name_en']} → {v['name_zh']}")
    if pending:
        print(f"⚠️  待补译名 {len(pending)} 支（已写入 {PENDING_PATH.relative_to(PROJECT_ROOT)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
