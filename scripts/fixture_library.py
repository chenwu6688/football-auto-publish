#!/usr/bin/env python3
"""赛程库 + 选题池生成器（计划第一阶段核心）

设计要点（对照《足球自媒体自动化选题与内容生产完善计划》）：
  1. 一级·确定性供给：赛程 / 赛季节点，占内容供给 30%，提前 7 天可预生成。
  2. **只产冲突点选题，不产「XX 前瞻」** —— 赛事前瞻已停发（欧冠 78 篇仅 852 阅读的教训）。
  3. 认知度分级过滤：A 级进新闻流首选，B 级需搭配冲突点，C 级降权仅用于数据/榜单类。
  4. 时效闸门：每条选题带 expires_at，过期不得进入发布流。
  5. 落库缓存：业务侧只读 data/fixtures/topic_pool.json，不直连 API。
  6. **14 天窗口 + 每日额度台账**（计划 11.4）：每日一次批量拉取未来 14 天赛程写库；
     免费额度按日计，缓存后每日调用量控制在 3 次以内 —— 由 api_budget 台账强制把关。

上游数据：football-data.org v4（免费层单次查询窗口 ≤ 10 天，故 14 天窗口拆两块）
        + 聚合数据 juhe.cn（补中超，中文源）+ API-Football（亚冠/欧联/欧协联/中超）
  用法：python3 scripts/fixture_library.py [--days 14] [--refresh]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import api_budget  # noqa: E402  （计划 11.4 每日接口额度台账）

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
COMPETITION_WEIGHT = {"欧冠": 1.0, "英超": 0.95, "西甲": 0.9, "意甲": 0.85, "德甲": 0.85, "法甲": 0.8, "中超": 0.7}

# 计划 11.4：目标落库窗口 14 天；免费层单次查询硬上限 10 天，故拆块请求。
TARGET_WINDOW_DAYS = 14
FD_MAX_WINDOW_DAYS = 10
MAX_WINDOW_DAYS = FD_MAX_WINDOW_DAYS   # 兼容旧引用（免费层单次上限）

# 计划 11.4：每日调用量 ≤3 次。前两档固定给 football-data（14 天须两块），
# 第三档按日轮换一个补源，4 日一轮；各源缓存跨日有效，故覆盖不缺口。
SUPPLEMENTS = (
    ("api-football", "欧联"),
    ("juhe", "中超"),
    ("api-football", "亚冠"),
    ("api-football", "欧协联"),
)

FIXTURE_DIR = PROJECT_ROOT / "data" / "fixtures"
ENTITY_MAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"

# 冲突点分型 → (认知度组合, 建议栏目, 角度模板, 人视角叙事要求)。
# 刻意不含「前瞻」类 —— 前瞻已停发（欧冠 78 篇仅 852 阅读）。
# 模板占位符：{home} {away} {lower} {higher}
#
# R11 数据结论落地（2026-10-13，建议3「换角度不换内容」）：
#   同一条赛程线索，「写人」比「写事」高 3-10 倍点击；赛事名词/纯对阵式垫底。
#   故角度一律从**人的处境**（主帅/核心球员/更衣室）切入，并**内嵌悬念词**
#   （没想到/原来/竟然/居然/反转，回测 +87%），不再做「A队 vs B队」纯战术罗列。
#   → 产量与选题不变，只换叙事角度（本节改动不改赛程线索本身）。
#   模板禁用词：前瞻/预览/展望/预测首发/赛前预测（计划 7.1：前瞻体占比为零）。
ANGLE_RULES = [
    ("AA", "强强对话", "战术榜单",
     "{home}对{away}，原来两位主帅才是最大变数",
     "以两位主帅的处境与选择为切口（谁压力更大、谁敢先变阵），不做纯实力对比。"),
    ("AB", "强弱反差", "人物故事",
     "没想到{lower}主帅这场压力比{higher}还大",
     "聚焦弱势一方主帅/核心球员的处境（他要证明什么、输不起的是什么），有人味、有细节。"),
    ("AC", "冷门窗口", "战术榜单",
     "{higher}账面更强，可{lower}主帅手里原来还有牌",
     "写弱势一方主帅手里的**具体变量**（还有什么牌可打、要赌哪一环），不空喊冷门。"),
    ("BB", "中游缠斗", "战术榜单",
     "{home}与{away}缠斗，原来名次之争压在两位核心身上",
     "把中游排名压力落到两名关键球员身上（谁的状态决定谁先掉队），少讲积分多讲人。"),
    ("BC", "关注度落差", "数据对照",
     "同一轮，{higher}被盯着，{lower}主帅原来更想赢",
     "对照两队在聚光灯下与无人问津下的不同处境，用数据落差讲人，不作纯数据罗列。"),
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


# 中超国内高关注度球队（按头条画像：男 95%+、31-50 占 43%、50+ 占 51%，
# 中超是其本土赛事，传统强队认知度接近五大联赛二线）。其余中超队默认 B 级。
CSL_TOP_TIER = {
    "上海海港", "北京国安", "山东泰山", "上海申花",
    "成都蓉城", "武汉三镇",
}

def resolve_csl_team(name_zh: str) -> dict:
    """中超队名（已是中文）→ 实体。Juhe 为中文权威源，未知队名也按 B 级放行不丢弃。"""
    if not name_zh:
        return None
    name_zh = name_zh.strip()
    return {
        "name_zh": name_zh, "name_en": "", "aliases": [],
        "awareness": "A" if name_zh in CSL_TOP_TIER else "B",
    }

def resolve_team(name: str, emap: dict, competition: str = "") -> dict | None:
    """队名 → 中文实体。
    - 先查 entity_map（英文名/别名→中文，含英文队名与中文别名两种键）；
      这样 API-Football 返回的英文队名（含中超）也能解析（计划 13.1 增量维护）。
    - 未命中且为中文名（聚合数据/人工赛历等中文源）：按 B 级放行（媒体惯用名，人工维护）。
    - 仍未命中：返回 None（未登记实体不进生成，计划 13.1）。
    """
    if not name:
        return None
    mapped = emap["by_en"].get(name.strip().lower())
    if mapped and mapped.get("name_zh"):
        return mapped
    if any("\u4e00" <= c <= "\u9fff" for c in name):
        return resolve_csl_team(name)
    return None


def todays_supplement(today=None) -> tuple:
    """计划 11.4：第三档额度按日轮换的补源（4 日一轮）。返回 (source, name)。"""
    t = today or datetime.now(CST).date()
    return SUPPLEMENTS[t.toordinal() % len(SUPPLEMENTS)]


def _fd_chunks(days: int) -> list[tuple[int, int]]:
    """把 1..days 的窗口按免费层 10 天上限切块，返回 [(offset_start, n_days)]。

    例：14 天 → [(0, 10), (10, 4)]，即第 1~10 天一块、第 11~14 天一块。
    """
    days = max(1, int(days))
    out, off = [], 0
    while off < days:
        n = min(FD_MAX_WINDOW_DAYS, days - off)
        out.append((off, n))
        off += n
    return out


def _fd_request(key: str, date_from: str, date_to: str) -> dict | None:
    """单块 football-data 请求（调用方已过额度台账）。失败 → None（降级）。"""
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
    avail = resp.headers.get("x-requests-available-minute", "?")
    print(f"✅ {payload.get('resultSet', {}).get('count', '?')} 场（本分钟剩余额度 {avail}）")
    return payload


def fetch_window(days: int, emap: dict, refresh: bool = False) -> list[dict]:
    """拉取 today ~ today+days-1 赛程（计划 11.4：14 天窗口 + 落库缓存 + 额度台账）。"""
    return fetch_window_report(days, emap, refresh=refresh)["matches"]


def fetch_window_report(days: int, emap: dict, refresh: bool = False) -> dict:
    """分块拉取窗口赛程，返回 {matches, window_days, granted_calls, refused_calls}。

    免费层单次查询窗口硬上限 10 天，故 14 天窗口拆成多块；
    每块真实请求前先过 api_budget 台账，被拒绝则跳过该块并告警（降级不阻塞）。
    """
    key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    today = datetime.now(CST)
    stamp = today.strftime("%Y-%m-%d")
    matches: list[dict] = []
    seen: set = set()
    window_days = 0
    granted = refused = 0

    for i, (off, n) in enumerate(_fd_chunks(days)):
        d_from = (today + timedelta(days=off)).strftime("%Y-%m-%d")
        d_to = (today + timedelta(days=off + n - 1)).strftime("%Y-%m-%d")
        cache_name = f"raw_{stamp}.json" if i == 0 else f"raw_{stamp}_c{i}.json"
        cpath = FIXTURE_DIR / cache_name

        payload = None
        if cpath.exists() and not refresh:
            try:
                payload = json.loads(cpath.read_text(encoding="utf-8"))
                print(f"📦 使用当日缓存 {cache_name}"
                      f"（{payload.get('resultSet', {}).get('count', '?')} 场）")
            except Exception:
                payload = None

        if payload is None:
            if not key or key == "***":
                if i == 0:
                    raise SystemExit(
                        "❌ 未设置 FOOTBALL_DATA_KEY。\n"
                        "   本地：source scripts/local_env.sh 或 export FOOTBALL_DATA_KEY=...\n"
                        "   CI：GitHub Actions Secrets 注入。"
                    )
                print(f"⚠️ 未设置 FOOTBALL_DATA_KEY，跳过窗口补块 {d_from} ~ {d_to}")
                break
            # 计划 11.4：真实外部调用一律先过额度台账
            grant = api_budget.record("football-data", note=f"{d_from}~{d_to}")
            if not grant["ok"]:
                refused += 1
                print(f"⚠️ 赛程窗口 {d_from} ~ {d_to} 跳过（额度台账拒绝：{grant['reason']}）；"
                      f"今日全局 {grant['total_used']}/{grant['total_cap']}")
                continue
            granted += 1
            payload = _fd_request(key, d_from, d_to)
            if payload is None:
                continue
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

        for m in (payload.get("matches", []) or []):
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            matches.append(m)
        window_days += n

    print("   （如需强制刷新加 --refresh）")
    return {"matches": matches, "window_days": window_days,
            "granted_calls": granted, "refused_calls": refused}


def build_topic_pool(matches: list[dict], emap: dict,
                     window_days: int = TARGET_WINDOW_DAYS) -> dict:
    """赛程 → 冲突点选题池。刻意不生成「前瞻」类标题。

    计划 11.4 的赛程只能作话题激活器；同时做时效过滤：缓存跨日有效后，
    已开赛的场次不得再进池（旧闻新发，计划 11.1 时效闸门）。
    """
    topics, dropped_unmapped, dropped_lowaware = [], [], []
    dropped_past = 0
    now = datetime.now(CST)

    for m in matches:
        comp_id = str((m.get("competition") or {}).get("id", ""))
        comp_name = next(
            (v for k, v in FIXTURE_COMPETITIONS.items() if k == comp_id),
            (m.get("competition") or {}).get("name", ""),
        )
        home = resolve_team(m.get("homeTeam", {}).get("name", ""), emap, comp_name)
        away = resolve_team(m.get("awayTeam", {}).get("name", ""), emap, comp_name)
        if not home or not away:
            missing = m.get("homeTeam", {}).get("name") if not home else m.get("awayTeam", {}).get("name")
            dropped_unmapped.append({"match_id": m.get("id"), "unmapped": missing})
            continue

        comp = COMPETITION_WEIGHT.get(comp_name, 0.5)

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
        _, angle_type, column, template, narrative = rule
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
        # 时效闸门（计划 11.1）：已开赛场次不进池（缓存跨日后尤须过滤，避免旧闻新发）
        if dt < now:
            dropped_past += 1
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
            # 人视角叙事要求（R11「写人 > 写事」）：供生成侧提示词据此把角度落在人的处境上
            "angle_hint": narrative,
            "suggested_column": column,
            # 计划 6.2 固定四板块：转会动态／人物故事／中国足球／战术榜单。
            # 赛程类选题映射：中超→中国足球；人物角度(suggested_column=人物故事)→人物故事；其余战术/数据角度→战术榜单。
            "板块": ("中国足球" if comp_name == "中超" else
                     ("人物故事" if column == "人物故事" else "战术榜单")),
            "priority": priority,
            "status": m.get("status"),
            # 时效闸门：赛程类选题必须在开赛前发布。开赛后再发＝旧闻新发（扣 10 分）
            "publish_by": dt.strftime("%Y-%m-%d %H:%M"),
            "fact_source": (
                {"api": "juhe.cn/football/query(zhongchao)", "match_id": m.get("id"),
                 "stage": m.get("match_stage", ""), "source": "juhe"}
                if m.get("source") == "juhe" else
                {"api": "football-data.org/v4", "match_id": m.get("id"),
                 "fd_comp_id": (m.get("competition") or {}).get("id")}
            ),
        })

    topics.sort(key=lambda t: -t["priority"])

    by_weekday: dict[str, int] = {}
    for t in topics:
        by_weekday[t["kickoff_weekday"]] = by_weekday.get(t["kickoff_weekday"], 0) + 1

    return {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "window_days": window_days,
        "window_target_days": TARGET_WINDOW_DAYS,
        "source": ("football-data.org v4 /matches（多赛事合并，按 10 天上限分块，"
                   "覆盖 %d 天）" % window_days),
        "stats": {
            "matches_total": len(matches),
            "topics": len(topics),
            "dropped_unmapped": len(dropped_unmapped),
            "dropped_low_awareness": len(dropped_lowaware),
            "dropped_past": dropped_past,
            "by_angle": {k: sum(1 for t in topics if t["angle_type"] == k) for k in {r[1] for r in ANGLE_RULES} if any(t["angle_type"] == k for t in topics)},
            "by_weekday": by_weekday,
        },
        # 欧联/亚冠/欧协联：已由 API-Football 接入（需 API_FOOTBALL_KEY，缺失时自动跳过）；
        # 国足：年度赛事少，由人工赛历 config/manual_fixtures.yaml 兜底（计划 11.4）。
        "coverage_gap": ["国足（人工赛历兜底）"],
        "topics": topics,
        "_dropped": {"unmapped": dropped_unmapped[:20], "low_awareness": dropped_lowaware[:20]},
    }


def fetch_juhe_fixtures(refresh: bool = False, allow_network: bool = True) -> list[dict]:
    """聚合数据 juhe.cn 中超赛程（中文源，补 football-data 免费层没有的中超）。

    只取「未开赛」的未来赛事；已完赛(status=3)/进行中(status=2)不进前瞻池
    （赛事前瞻已停发，只产冲突点选题）。降级：无 key / 网络错 / error_code!=0
    → 返回 [] 不崩，不影响主流程。真实调用由 api_budget 台账把关（计划 11.4）。

    ``allow_network=False`` 用于「非轮换日」：只读缓存，绝不消耗额度。
    """
    cache_path = FIXTURE_DIR / "raw_juhe_csl.json"
    if cache_path.exists() and not refresh:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            print(f"📦 使用 Juhe 中超缓存 {cache_path.name}（{len(cached)} 场未来赛事）")
            return cached
        except Exception:
            pass

    if not allow_network:
        print("📦 Juhe 中超：非轮换日且无缓存，跳过（不消耗额度）")
        return []

    key = os.environ.get("JUHE_API_KEY", "").strip()
    if not key or key == "***":
        print("⚠️ 未设置 JUHE_API_KEY，跳过中超赛程（不影响主流程）")
        return []

    # 先确认可用再记台账，避免无 key 白占额度
    grant = api_budget.record("juhe", note="中超赛程")
    if not grant["ok"]:
        print(f"⚠️ Juhe 中超刷新跳过（额度台账拒绝：{grant['reason']}）；"
              f"今日全局 {grant['total_used']}/{grant['total_cap']}")
        return []

    import urllib.request
    import urllib.parse
    url = "http://apis.juhe.cn/fapig/football/query?" + urllib.parse.urlencode(
        {"key": key, "type": "zhongchao"}
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"⚠️ Juhe 中超拉取失败（降级跳过）：{e}")
        return []

    if data.get("error_code", -1) != 0:
        print(f"⚠️ Juhe 返回错误（降级跳过）：error_code={data.get('error_code')} reason={data.get('reason')}")
        return []

    matches: list[dict] = []
    today = datetime.now(CST).date()
    for day in data.get("result", {}).get("matchs", []):
        d = day.get("date", "")
        try:
            ddate = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        for it in day.get("list", []):
            if it.get("status") != "1":        # 只取未开赛
                continue
            if ddate < today:                   # 过去日期跳过（旧闻）
                continue
            ts = (it.get("time_start") or "19:35").strip()
            matches.append({
                "id": f"csl-{d}-{it.get('team1')}-{it.get('team2')}",
                "competition": {"id": "csl", "name": "中超"},
                "homeTeam": {"name": it.get("team1", "")},
                "awayTeam": {"name": it.get("team2", "")},
                "utcDate": f"{d}T{ts}:00+08:00",
                "status": "SCHEDULED",
                "source": "juhe",
                "match_stage": it.get("match_stage", ""),
            })

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=False, indent=2)
    print(f"✅ Juhe 中超：{len(matches)} 场未来赛事（已缓存 {cache_path.name}）")
    return matches


def load_manual_fixtures(path: Path | None = None) -> list[dict]:
    """读取人工赛历（计划 11.4 / 13.2）：国足 / 中超 / 欧联的格式补位。

    输出与接口赛程同构的 match dict，直接并入选题池。
    仅登记未来赛程；过去日期跳过（旧闻）。
    """
    p = Path(path) if path else (PROJECT_ROOT / "config" / "manual_fixtures.yaml")
    if not p.exists():
        return []
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"⚠️ 人工赛历解析失败（跳过）: {e}")
        return []

    today = datetime.now(CST).date()
    out = []
    for it in (cfg.get("fixtures") or []):
        d = str(it.get("date", "")).strip()
        home = str(it.get("home", "")).strip()
        away = str(it.get("away", "")).strip()
        if not (d and home and away):
            continue
        try:
            ddate = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if ddate < today:
            continue
        ts = str(it.get("time") or "19:35").strip()
        comp = str(it.get("competition") or "人工赛历").strip()
        out.append({
            "id": f"manual-{d}-{home}-{away}",
            "competition": {"id": "manual", "name": comp},
            "homeTeam": {"name": home},
            "awayTeam": {"name": away},
            "utcDate": f"{d}T{ts}:00+08:00",
            "status": "SCHEDULED",
            "source": "manual",
            "note": str(it.get("note") or ""),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="赛程库 + 选题池生成器")
    ap.add_argument("--days", type=int, default=TARGET_WINDOW_DAYS,
                    help=f"向前天数（计划 11.4 目标 {TARGET_WINDOW_DAYS} 天；"
                         f"football-data 单块上限 {FD_MAX_WINDOW_DAYS} 天）")
    ap.add_argument("--refresh", action="store_true", help="忽略当日缓存，强制重新拉取")
    args = ap.parse_args()

    emap = load_entity_map()
    print(f"📖 中文实体映射：{emap['raw']['stats']['total']} 个实体"
          f"（A={emap['raw']['stats']['A']} B={emap['raw']['stats']['B']} C={emap['raw']['stats']['C']}）")

    # ① football-data：14 天窗口（按 10 天上限分块），每块过额度台账
    win = fetch_window_report(args.days, emap, refresh=args.refresh)
    matches = win["matches"]

    # ② 补源：第三档额度按日轮换（计划 11.4：每日调用量 ≤3 次）
    sup_src, sup_name = todays_supplement()
    print(f"🔁 今日补源轮换：{sup_src} · {sup_name}"
          f"（4 日一轮，其余联赛读最近缓存、不消耗额度）")
    csl = fetch_juhe_fixtures(refresh=args.refresh, allow_network=(sup_src == "juhe"))

    af_refreshed: list[dict] = []
    if sup_src == "api-football":
        try:
            import api_football
            af_refreshed = api_football.fetch_league_fixtures(sup_name, args.days, refresh=True)
        except Exception as e:
            print(f"⚠️ API-Football 刷新异常（跳过）：{str(e)[:80]}")

    # ③ 其余洲际联赛：只读最近缓存（业务侧只读库，绝不消耗额度）
    af_cached: list[dict] = []
    try:
        import api_football
        af_cached = api_football.fetch_all_cached()
    except Exception as e:
        print(f"⚠️ API-Football 缓存读取异常（跳过）：{str(e)[:80]}")

    af, _seen_af = [], set()
    for m in af_refreshed + af_cached:
        if m.get("id") in _seen_af:
            continue
        _seen_af.add(m.get("id"))
        af.append(m)

    manual = load_manual_fixtures()
    all_matches = matches + csl + manual + af
    print(f"   （其中洲际赛程：{len(af)} 场，来自 API-Football：当日刷新 {len(af_refreshed)} + 缓存 {len(af_cached)}）")
    pool = build_topic_pool(all_matches, emap, window_days=win["window_days"])
    print(f"   （其中中超未来赛事：{sum(1 for m in all_matches if m.get('source') == 'juhe')} 场，来自聚合数据 juhe.cn）")
    if manual:
        print(f"   （其中人工赛历：{len(manual)} 场，来自 config/manual_fixtures.yaml）")

    # 计划 11.4：把当日额度使用情况一并落进选题池，便于复盘「每日 ≤3 次」
    budget_rep = api_budget.summary()
    pool["window_covered_days"] = win["window_days"]
    pool["api_budget"] = budget_rep
    if budget_rep["total_used"] > budget_rep["total_cap"]:
        print(f"   ❌ 今日接口调用 {budget_rep['total_used']} 次，超出计划 11.4 的 "
              f"{budget_rep['total_cap']} 次上限！")

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / "topic_pool.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

    s = pool["stats"]
    try:
        out_disp = out.relative_to(PROJECT_ROOT)
    except Exception:
        out_disp = out
    print()
    print(f"🗂  选题池已生成 → {out_disp}")
    print(f"   窗口 {win['window_days']}/{TARGET_WINDOW_DAYS} 天"
          f"（football-data 实调 {win['granted_calls']} 次，被额度拒绝 {win['refused_calls']} 次）")
    print(f"   赛程 {s['matches_total']} 场 → 选题 {s['topics']} 条"
          f"（未映射打回 {s['dropped_unmapped']}，低认知过滤 {s['dropped_low_awareness']}，"
          f"过期剔除 {s['dropped_past']}）")
    print(f"   分型：{s['by_angle']}")
    print(f"   按星期：{s['by_weekday']}")
    print(f"   🧾 接口额度：{budget_rep['total_used']}/{budget_rep['total_cap']} 次"
          f"（计划 11.4 上限 {api_budget.DEFAULT_DAILY_TOTAL_CAP} 次/日）")
    print(f"   ⚠️ 免费层未覆盖：{'、'.join(pool['coverage_gap'])}")
    print()
    print("   Top 8 选题：")
    for t in pool["topics"][:8]:
        print(f"     [{t['priority']:5.1f}] {t['kickoff_cst'][:16]} {t['competition']:3s} "
              f"{t['home_zh']} vs {t['away_zh']}  <{t['angle_type']}>")
        print(f"            └ {t['conflict']}")


if __name__ == "__main__":
    main()
