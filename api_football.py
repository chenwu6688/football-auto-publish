"""API-Football 洲际赛程补齐（计划 13.2 数据源四源分工）。

计划 13.2 明确：football-data.org 覆盖五大联赛 / 欧冠 / 世界杯 / 欧洲杯；
**中超、亚冠、国联、欧联需 API-Football 补齐**（含伤病与转会字段），
免费档 100 次/日，必须落库缓存。

三条工程纪律：
  1. **联赛 ID 不硬编码猜测**：通过 ``/leagues?search=`` 动态解析并缓存到
     data/fixtures/af_leagues.json（可人工覆盖），避免写死错误的 id。
  2. **额度按日管理 + 落库缓存**：每个联赛每日 1 次 fixtures 查询，结果写
     data/fixtures/af_<联赛>_<日期>.json；业务侧只读缓存、不直连接口。
  3. **绝不阻塞**：无 key / 网络失败 / 额度用尽 → 返回空并降级（计划九）。

产出的 match dict 与 football-data.org 同构，直接并入 fixture_library 的选题池。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = PROJECT_ROOT / "data" / "fixtures"
LEAGUE_CACHE = FIXTURE_DIR / "af_leagues.json"

CST = timezone(timedelta(hours=8))

# 计划 13.2：需 API-Football 补齐的赛事 → 官方英文名（用于动态搜索，非硬编码 id）
LEAGUE_SEARCH = {
    "亚冠": "AFC Champions League",
    "欧联": "Europa League",
    "欧协联": "Europa Conference League",
    "中超": "Chinese Super League",
}

_DEFAULT_HOST = "api-football-v1.p.rapidapi.com"
_MAX_WINDOW_DAYS = 10


def _key() -> str:
    return (os.environ.get("API_FOOTBALL_KEY")
            or os.environ.get("RAPIDAPI_KEY") or "").strip()


def _host() -> str:
    return os.environ.get("API_FOOTBALL_HOST", _DEFAULT_HOST).strip()


def _headers() -> dict:
    return {"x-rapidapi-key": _key(), "x-rapidapi-host": _host()}


def _get(path: str, params: dict, timeout: int = 20):
    """统一请求入口；调用方负责异常处理。"""
    import requests
    url = f"https://{_host()}/v3/{path}"
    return requests.get(url, params=params, headers=_headers(), timeout=timeout)


# ------------------------------------------------------------
# 联赛 ID 动态解析
# ------------------------------------------------------------
def _load_league_cache() -> dict:
    try:
        return json.loads(LEAGUE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_league_ids(names=None, *, refresh: bool = False, timeout: int = 20) -> dict:
    """把中文赛事名解析为 {中文名: {"id":..,"season":..}}；动态搜索 + 缓存。

    无 key / 搜索无结果 / 网络失败 → 该项不返回（降级，不臆测 id）。
    """
    names = list(names or LEAGUE_SEARCH.keys())
    cache = {} if refresh else _load_league_cache()
    resolved = {k: v for k, v in cache.items() if k in names and v.get("id")}
    todo = [n for n in names if n not in resolved]
    if not todo or not _key():
        return resolved

    for name in todo:
        q = LEAGUE_SEARCH.get(name)
        if not q:
            continue
        try:
            r = _get("leagues", {"search": q, "type": "league"}, timeout=timeout)
            r.raise_for_status()
            arr = (r.json() or {}).get("response") or []
        except Exception:
            continue
        if not arr:
            continue
        # 取最相关的一条；season 取当年（跨年赛季由 API 的 current 标记决定）
        for item in arr:
            lg = item.get("league") or {}
            seasons = item.get("seasons") or []
            cur = next((s for s in seasons if s.get("current")), None)
            year = (cur or {}).get("year")
            if lg.get("id") and lg.get("type") == "League":
                resolved[name] = {"id": lg["id"], "season": year, "name_en": lg.get("name", "")}
                break

    if resolved:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        merged = _load_league_cache()
        merged.update(resolved)
        LEAGUE_CACHE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return resolved


# ------------------------------------------------------------
# 赛程抓取与适配
# ------------------------------------------------------------
def _to_fd(fx: dict, comp_name: str) -> dict:
    """API-Football fixture → 与 football-data.org 同构的 match dict。"""
    f = fx.get("fixture") or {}
    t = fx.get("teams") or {}
    lg = fx.get("league") or {}
    return {
        "id": f.get("id"),
        "competition": {"id": f"af-{lg.get('id')}", "name": comp_name},
        "homeTeam": {"name": (t.get("home") or {}).get("name", "")},
        "awayTeam": {"name": (t.get("away") or {}).get("name", "")},
        "utcDate": f.get("date", ""),
        "status": ((f.get("status") or {}).get("short", "") or ""),
        "source": "api-football",
    }


def fetch_league_fixtures(name: str, days: int, *, refresh: bool = False,
                          timeout: int = 20) -> list[dict]:
    """抓取单个联赛的未来赛程（落库缓存）。失败/无 key → []。"""
    info = resolve_league_ids([name]).get(name)
    if not info or not info.get("id"):
        return []

    days = min(days, _MAX_WINDOW_DAYS)
    today = datetime.now(CST).date()
    cache_path = FIXTURE_DIR / f"af_{name}_{today.isoformat()}.json"
    if cache_path.exists() and not refresh:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    date_from = today.isoformat()
    date_to = (today + timedelta(days=days - 1)).isoformat()
    params = {"league": info["id"], "from": date_from, "to": date_to}
    if info.get("season"):
        params["season"] = info["season"]
    try:
        r = _get("fixtures", params, timeout=timeout)
        r.raise_for_status()
        arr = (r.json() or {}).get("response") or []
    except Exception as e:
        print(f"⚠️ API-Football {name} 拉取失败（降级跳过）：{str(e)[:80]}")
        return []

    out = [_to_fd(fx, name) for fx in arr]
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ API-Football {name}：{len(out)} 场未来赛事（缓存 {cache_path.name}）")
    return out


def fetch_fixtures(days: int, names=None, *, refresh: bool = False) -> list[dict]:
    """抓取全部配置联赛；无 key 时安静跳过（返回 []，绝不阻塞）。"""
    if not _key():
        print("ℹ️ 未配置 API_FOOTBALL_KEY，跳过洲际赛程补齐（亚冠/欧联/欧协联/中超）")
        return []
    names = list(names or LEAGUE_SEARCH.keys())
    out: list[dict] = []
    for n in names:
        out.extend(fetch_league_fixtures(n, days, refresh=refresh))
    return out


if __name__ == "__main__":
    import sys
    d = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    res = fetch_fixtures(d)
    print(f"共 {len(res)} 场")
