"""把正文里已经陈述的比赛事实画成图——零版权风险，相关性不需要「证明」。

为什么要有这个模块
------------------
2026-11-05 用户提出「能不能直接调媒体图、去掉 logo」。答案是法律上反向有害
（著作权法 53 条(7) 把「故意删除权利管理信息」单列为侵权行为，见
docs/audit/2026-11-05-配图版权与替代方案.md）。真正的解法不是找更好的图源，而是
**换一类图**：把正文已经写出来的事实可视化。数据源就是正文本身，所以：

- 相关性天然成立：图里画的是文章说的那支队、那个比分、那个积分；
- 版权完全干净：自己算、自己画，不复制任何他人作品；
- 信息增量：读者看完标题再看到积分榜位置，比看一张球员特写更有用。

三种图（全部由真实结构化数据驱动，拿不到数据就返回 None，**绝不画假图**）
--------------------------------------------------------------------
- ``standings`` 积分榜区间图：正文谈到某队名次/积分时
- ``goals``     进球时间轴：正文谈到某场比赛的进程时
- ``score``     比分卡：正文谈到某场具体比分、但战报没给进球细节时

相关性契约
----------
每张图在**绘图函数内部**自己声明 ``anchor``（画的是谁、哪个数字、数据来自哪），
交 ``image_relevance.check()`` 在入库前校验它是否真的在正文里出现过。
anchor 不接受调用方传入——否则就会出现「图里画皇马、anchor 写巴萨」这种自欺。

命令行
------
    python3 data_chart.py --selftest
    python3 data_chart.py --fontstatus
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import cover_card as _cc

PROJECT_ROOT = Path(__file__).resolve().parent
ENTITY_MAP = PROJECT_ROOT / "config" / "entity_map.json"

W, H = 1200, 800          # 3:2，正文图——过下载端 600×400 闸门
MARGIN = 72

BG_TOP = (16, 22, 36)
BG_BOTTOM = (30, 39, 58)
INK = (242, 244, 248)
INK_DIM = (156, 165, 180)
INK_FAINT = (104, 114, 132)
ACCENT = (214, 48, 49)
AWAY = (74, 134, 200)
GRID = (56, 68, 88)
ROW_HL = (44, 34, 46)      # 高亮行底色：只在深底上提亮一点点，不抢红色竖条

LAST_ERROR = ""

_CACHE: dict = {"index": None}


# ---------------------------------------------------------------- 实体解析

def _build_index() -> dict:
    """队名/别名 → 实体。standings 来自 football-data.org 的英文队名，正文是中文。"""
    idx: dict[str, dict] = {}
    try:
        data = json.loads(ENTITY_MAP.read_text(encoding="utf-8")) or {}
    except Exception as exc:                      # 配置缺失不能让整批崩
        globals()["LAST_ERROR"] = f"entity_map 读取失败: {exc}"
        return idx

    for item in (data.get("entities") or {}).values():
        if not isinstance(item, dict):
            continue
        if str(item.get("category") or "") not in ("club", "team", "national"):
            continue
        keys = set()
        for field in ("name_zh", "name_en", "fd_tla"):
            v = str(item.get(field) or "").strip()
            if len(v) >= 2:
                keys.add(v.lower())
        for a in item.get("aliases") or []:
            a = str(a or "").strip()
            if len(a) >= 2:
                keys.add(a.lower())
        for k in keys:
            idx.setdefault(k, item)
    return idx


def _index() -> dict:
    if _CACHE["index"] is None:
        _CACHE["index"] = _build_index()
    return _CACHE["index"]


def all_teams() -> list[dict]:
    """全部已登记球队实体（去重）。相关性闸门要靠它做「最长匹配消解」。"""
    seen, out = set(), []
    for item in _index().values():
        k = id(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def resolve_team(query: str) -> dict | None:
    """中文名/英文名/别名/三字码 → 实体。查不到就 None（不猜）。"""
    q = (query or "").strip().lower()
    if len(q) < 2:
        return None
    idx = _index()
    if q in idx:
        return idx[q]
    # 「皇家马德里足球俱乐部」这类长名，去掉后缀再试
    for suffix in ("足球俱乐部", "俱乐部", "足球"):
        if q.endswith(suffix) and len(q) - len(suffix) >= 2:
            hit = idx.get(q[: -len(suffix)].lower())
            if hit:
                return hit
    return None


def entity_names(item: dict) -> set[str]:
    """实体在正文里可能出现的全部写法（小写），用于相关性校验。"""
    names = set()
    if not isinstance(item, dict):
        return names
    for field in ("name_zh", "name_en"):
        v = str(item.get(field) or "").strip()
        if len(v) >= 2:
            names.add(v.lower())
    for a in item.get("aliases") or []:
        a = str(a or "").strip()
        if len(a) >= 2:
            names.add(a.lower())
    zh = str(item.get("name_zh") or "")
    for suffix in ("足球俱乐部", "俱乐部"):
        if zh.endswith(suffix) and len(zh) - len(suffix) >= 2:
            names.add(zh[: -len(suffix)].lower())
    return names


# ---------------------------------------------------------------- 绘图底座

def _canvas():
    from PIL import Image, ImageDraw
    img = _cc._gradient((W, H), BG_TOP, BG_BOTTOM)
    return img, ImageDraw.Draw(img)


def _chrome(draw, kicker: str, tag: str, f_kicker, f_tag):
    """顶部品牌条 + 左上角栏目 + 右上角标签 + 分隔线。三种图共用。"""
    draw.rectangle([0, 0, W, 10], fill=ACCENT)
    draw.text((MARGIN, 42), kicker, font=f_kicker, fill=INK)
    if tag:
        tw = draw.textlength(tag, font=f_tag)
        x0 = W - MARGIN - tw - 32
        draw.rounded_rectangle([x0, 38, W - MARGIN, 78], radius=20,
                               outline=GRID, width=2)
        draw.text((x0 + 16, 48), tag, font=f_tag, fill=INK_DIM)
    draw.line([(MARGIN, 112), (W - MARGIN, 112)], fill=GRID, width=1)


def _footer(draw, source: str, f_foot):
    draw.line([(MARGIN, H - 86), (W - MARGIN, H - 86)], fill=GRID, width=1)
    draw.text((MARGIN, H - 66), source, font=f_foot, fill=INK_FAINT)
    draw.text((W - MARGIN - 108, H - 66), "老六说球", font=f_foot, fill=INK_FAINT)


def _save(img, out_dir: Path | str | None, filename: str) -> str:
    d = Path(out_dir) if out_dir else _cc.CARD_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    img.save(p, "JPEG", quality=90, optimize=True)
    return str(p)


def _text_at(draw, x: float, cy: float, text: str, font, fill,
             align: str = "left") -> float:
    """按**实际墨迹盒**在 cy 上垂直居中画一行字，返回它的宽度。

    Pillow 的 ``anchor=`` 对 .ttc（Noto CJK）不稳，所以自己量 bbox。
    画图最怕「队名被一条线穿过」这种错位，量真实上下沿是唯一可靠做法。
    """
    if not text:
        return 0.0
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    w, h = x1 - x0, y1 - y0
    left = x - w if align == "right" else (x - w / 2 if align == "center" else x)
    draw.text((left - x0, cy - h / 2 - y0), text, font=font, fill=fill)
    return w


# ---------------------------------------------------------------- 积分榜区间图

def pick_standings_team(article_text: str, standings: dict) -> tuple[str, dict, dict] | None:
    """在积分榜里找正文提到的、-awareness 最高的那支队。

    返回 (联赛名, 该行, 实体)。一支都没提到 → None。
    """
    if not isinstance(standings, dict) or not standings:
        return None
    low = (article_text or "").lower()
    if not low:
        return None

    best = None
    for league, table in standings.items():
        if not isinstance(table, list):
            continue
        for row in table:
            if not isinstance(row, dict):
                continue
            team_en = str(row.get("team") or "").strip()
            item = resolve_team(team_en)
            if not item:
                continue
            for name in entity_names(item):
                if len(name) >= 2 and name in low:
                    # 名字越长越具体（「皇家马德里」优先于「马德里」），先到先得但要择优
                    score = (len(name), -int(row.get("position") or 999))
                    if best is None or score > best[0]:
                        best = (score, league, row, item)
                    break
    if not best:
        return None
    return best[1], best[2], best[3]


def chart_standings(entity: dict, row: dict, table: list[dict], league: str,
                    out_dir: Path | str | None = None, filename: str = "chart-standings.jpg",
                    span: int = 3) -> dict | None:
    """画该队上下各 span 名的积分榜区间，高亮该队。"""
    try:
        pos = int(row.get("position") or 0)
        pts = row.get("points")
        if pos <= 0 or pts is None:
            globals()["LAST_ERROR"] = "名次或积分为空"
            return None
        team_zh = str(entity.get("name_zh") or row.get("team") or "").strip()
        if not team_zh:
            globals()["LAST_ERROR"] = "队名为空"
            return None

        rows = [r for r in table if isinstance(r, dict) and r.get("position")]
        window = [r for r in rows if abs(int(r["position"]) - pos) <= span]
        if len(window) < 2:
            globals()["LAST_ERROR"] = "积分榜窗口不足 2 行"
            return None

        img, draw = _canvas()
        f_brand = _cc._font(30)
        f_tag = _cc._font(24)
        f_row = _cc._font(30)
        f_num = _cc._font(32)
        f_small = _cc._font(21)
        f_foot = _cc._font(19)

        _chrome(draw, "积分榜", f"{league} · 截至当前", f_brand, f_tag)

        # 表头
        top = 150
        draw.text((MARGIN, top), "名次", font=f_small, fill=INK_FAINT)
        draw.text((MARGIN + 96, top), "球队", font=f_small, fill=INK_FAINT)
        draw.text((W - MARGIN - 250, top), "场次", font=f_small, fill=INK_FAINT)
        draw.text((W - MARGIN - 130, top), "积分", font=f_small, fill=INK_FAINT)
        draw.line([(MARGIN, top + 34), (W - MARGIN, top + 34)], fill=GRID, width=1)

        # 行高自适应：5 行时撑满，7 行时收紧，避免下方留一大片空
        area_top = top + 58
        area_bottom = H - 120
        row_h = max(52, min(92, int((area_bottom - area_top) / max(1, len(window)))))
        y = area_top
        for r in window:
            rp = int(r["position"])
            is_me = rp == pos
            cy = y + row_h / 2 - 4

            if is_me:
                draw.rounded_rectangle([MARGIN - 18, y - 6, W - MARGIN, y + row_h - 14],
                                       radius=10, fill=ROW_HL)
                draw.rectangle([MARGIN - 18, y - 6, MARGIN - 12, y + row_h - 14],
                               fill=ACCENT)

            name = str(r.get("team") or "")
            item = resolve_team(name)
            if item and item.get("name_zh"):
                name = str(item["name_zh"])
            ink = INK if is_me else INK_DIM

            _text_at(draw, MARGIN + 12, cy, str(rp), f_num,
                     ACCENT if is_me else INK_FAINT)
            _text_at(draw, MARGIN + 96, cy, name, f_row, ink)
            _text_at(draw, W - MARGIN - 240, cy, str(r.get("played") or "-"),
                     f_num, INK_FAINT)
            _text_at(draw, W - MARGIN - 130, cy,
                     str(r.get("points") if r.get("points") is not None else "-"),
                     f_num, ACCENT if is_me else INK)
            y += row_h

        _footer(draw, "数据来源：football-data.org 联赛积分榜（本图由数据重绘）", f_foot)

        path = _save(img, out_dir, filename)
        return {
            "path": path,
            "kind": "standings",
            "width": W, "height": H,
            "caption": f"{team_zh}目前第{pos}，{pts}分",
            "anchor": {
                "kind": "standings",
                "entities": [team_zh],
                "numbers": [str(pos), str(pts)],
                "source": "football-data.org 积分榜",
                "probe": f"{team_zh} 第{pos} {pts}分",
            },
        }
    except Exception as exc:
        globals()["LAST_ERROR"] = f"积分榜图异常: {type(exc).__name__}: {exc}"
        return None


# ---------------------------------------------------------------- 进球时间轴

def chart_goals(fixture: dict, out_dir: Path | str | None = None,
                filename: str = "chart-goals.jpg") -> dict | None:
    """0–90 分钟时间轴，主队进球在上、客队在下，标注球员与分钟。"""
    try:
        home = str(fixture.get("home_team") or "").strip()
        away = str(fixture.get("away_team") or "").strip()
        goals = [g for g in (fixture.get("goals") or []) if isinstance(g, dict)]
        if not home or not away or not goals:
            globals()["LAST_ERROR"] = "缺队名或无进球事件"
            return None

        hs = fixture.get("home_score")
        as_ = fixture.get("away_score")
        if hs is None or as_ is None:
            hs = sum(1 for g in goals if g.get("scorer_team") == "home")
            as_ = sum(1 for g in goals if g.get("scorer_team") == "away")

        item_h, item_a = resolve_team(home), resolve_team(away)
        zh_h = str((item_h or {}).get("name_zh") or home)
        zh_a = str((item_a or {}).get("name_zh") or away)

        img, draw = _canvas()
        f_brand = _cc._font(30)
        f_tag = _cc._font(24)
        f_team = _cc._font(38)
        f_score = _cc._font(96)
        f_ev = _cc._font(24)
        f_small = _cc._font(19)
        f_foot = _cc._font(19)

        league = str(fixture.get("league") or fixture.get("competition") or "")
        _chrome(draw, "比赛进程", league, f_brand, f_tag)

        # 三栏对称：主队靠左、比分居中、客队靠右，同一水平中线
        cy_head = 250
        _text_at(draw, MARGIN, cy_head, zh_h, f_team, INK)
        _text_at(draw, W / 2, cy_head, f"{int(hs)} - {int(as_)}", f_score, INK, "center")
        _text_at(draw, W - MARGIN, cy_head, zh_a, f_team, INK_DIM, "right")

        # 时间轴
        axis_y = 520
        x0, x1 = MARGIN + 30, W - MARGIN - 30
        draw.line([(x0, axis_y), (x1, axis_y)], fill=GRID, width=3)
        for m in (0, 15, 30, 45, 60, 75, 90):
            x = x0 + (x1 - x0) * m / 90.0
            draw.line([(x, axis_y - 8), (x, axis_y + 8)], fill=INK_FAINT, width=2)
            _text_at(draw, x, axis_y + 26, f"{m}'", f_small, INK_FAINT, "center")

        # 进球标记。同侧标签挨太近会叠字，按"上一个标签右沿 + 12"顺推。
        last_edge = {"home": -1e9, "away": -1e9}
        for g in goals[:12]:
            try:
                minute = max(0, min(120, int(g.get("minute") or 0)))
            except (TypeError, ValueError):
                continue
            x = x0 + (x1 - x0) * min(minute, 90) / 90.0
            is_home = str(g.get("scorer_team") or "home") == "home"
            side = "home" if is_home else "away"
            color = ACCENT if is_home else AWAY
            draw.ellipse([x - 10, axis_y - 10, x + 10, axis_y + 10], fill=color)

            label = str(g.get("scorer") or "").strip()
            txt = f"{label} {minute}'" if label else f"{minute}'"
            tw = draw.textlength(txt, font=f_ev)
            lx = x - tw / 2
            if lx < last_edge[side] + 12:
                lx = last_edge[side] + 12
            lx = max(MARGIN, min(lx, W - MARGIN - tw))
            last_edge[side] = lx + tw
            ly = axis_y - 74 if is_home else axis_y + 74
            _text_at(draw, lx, ly, txt, f_ev, color)

        _footer(draw, "数据来源：直播吧战报进球事件（本图由数据重绘）", f_foot)

        path = _save(img, out_dir, filename)
        return {
            "path": path,
            "kind": "goals",
            "width": W, "height": H,
            "caption": f"{zh_h} {int(hs)}-{int(as_)} {zh_a}",
            "anchor": {
                "kind": "goals",
                "entities": [zh_h, zh_a],
                "numbers": [str(int(hs)), str(int(as_))],
                "source": "直播吧战报进球事件",
                "probe": f"{zh_h}{int(hs)}-{int(as_)}{zh_a}",
            },
        }
    except Exception as exc:
        globals()["LAST_ERROR"] = f"进球时间轴异常: {type(exc).__name__}: {exc}"
        return None


# ---------------------------------------------------------------- 比分卡

def chart_score(fixture: dict, out_dir: Path | str | None = None,
                filename: str = "chart-score.jpg") -> dict | None:
    """无进球细节时的比分卡：大字比分 + 双方 + 联赛轮次。"""
    try:
        home = str(fixture.get("home_team") or "").strip()
        away = str(fixture.get("away_team") or "").strip()
        hs, as_ = fixture.get("home_score"), fixture.get("away_score")
        if not home or not away or hs is None or as_ is None:
            globals()["LAST_ERROR"] = "缺队名或比分"
            return None

        item_h, item_a = resolve_team(home), resolve_team(away)
        zh_h = str((item_h or {}).get("name_zh") or home)
        zh_a = str((item_a or {}).get("name_zh") or away)

        img, draw = _canvas()
        f_brand = _cc._font(30)
        f_tag = _cc._font(24)
        f_team = _cc._font(52)
        f_score = _cc._font(190)
        f_foot = _cc._font(19)

        league = str(fixture.get("league") or fixture.get("competition") or "")
        _chrome(draw, "赛果", league, f_brand, f_tag)

        # 极简版式：大字比分居中，队名分列两端，底部一条收束线。
        _text_at(draw, W / 2, 330, f"{int(hs)} : {int(as_)}", f_score, INK, "center")
        _text_at(draw, MARGIN, 500, zh_h, f_team, INK)
        _text_at(draw, W - MARGIN, 500, zh_a, f_team, INK_DIM, "right")
        draw.line([(MARGIN, 572), (W - MARGIN, 572)], fill=GRID, width=1)

        _footer(draw, "数据来源：公开赛事比分（本图由数据重绘）", f_foot)

        path = _save(img, out_dir, filename)
        return {
            "path": path,
            "kind": "score",
            "width": W, "height": H,
            "caption": f"{zh_h} {int(hs)}-{int(as_)} {zh_a}",
            "anchor": {
                "kind": "score",
                "entities": [zh_h, zh_a],
                "numbers": [str(int(hs)), str(int(as_))],
                "source": "公开赛事比分",
                "probe": f"{zh_h}{int(hs)}-{int(as_)}{zh_a}",
            },
        }
    except Exception as exc:
        globals()["LAST_ERROR"] = f"比分卡异常: {type(exc).__name__}: {exc}"
        return None


# ---------------------------------------------------------------- 总entry

def candidates(art: dict, match_data: dict, out_dir: Path | str | None = None,
               prefix: str = "chart") -> list[dict]:
    """给一篇文章产出候选数据图（按信息密度排序），已过「数据是否真实存在」检查。

    注意：这里**不做相关性校验**——那是 image_relevance 的职责。本函数只保证
    「图画得出来」且「anchor 与画面一致」。
    """
    text = f"{art.get('title', '')}\n{art.get('content', '')}"
    out: list[dict] = []
    md = match_data if isinstance(match_data, dict) else {}

    # 1. 积分榜区间（信息密度最高，优先）
    standings = md.get("standings") or {}
    picked = pick_standings_team(text, standings)
    if picked:
        league, row, item = picked
        table = standings.get(league) or []
        c = chart_standings(item, row, table, league, out_dir=out_dir,
                            filename=f"{prefix}-standings.jpg")
        if c:
            out.append(c)

    # 2. 进球时间轴 / 3. 比分卡（同一场只出一张，优先时间轴）
    fixtures = md.get("all_fixtures") or []
    if not fixtures:
        for league, rows in (md.get("fixtures_by_league") or {}).items():
            for f in rows or []:
                if isinstance(f, dict):
                    fixtures.append({**f, "league": f.get("league") or league})

    low = text.lower()
    for f in fixtures:
        if not isinstance(f, dict):
            continue
        home, away = str(f.get("home_team") or ""), str(f.get("away_team") or "")
        if not home or not away:
            continue
        ih, ia = resolve_team(home), resolve_team(away)
        zh_h = str((ih or {}).get("name_zh") or home)
        zh_a = str((ia or {}).get("name_zh") or away)
        # 两队都必须在正文里出现过，否则这场跟文章无关
        if not all(n in low for n in (zh_h.lower(), zh_a.lower())):
            continue
        c = chart_goals(f, out_dir=out_dir, filename=f"{prefix}-goals.jpg")
        if c is None:
            c = chart_score(f, out_dir=out_dir, filename=f"{prefix}-score.jpg")
        if c:
            out.append(c)
            break
    return out


def font_status() -> tuple[bool, str]:
    return _cc.font_status()


# ---------------------------------------------------------------- 自检

def selftest() -> list[tuple[str, bool]]:
    import tempfile

    results: list[tuple[str, bool]] = []
    ok, detail = _cc.font_status()
    results.append((f"中文字体可用（{detail}）", ok))
    if not ok:
        return results

    tmp = Path(tempfile.mkdtemp(prefix="chart-"))

    table = [
        {"position": 1, "team": "FC Barcelona", "points": 34, "played": 13, "goal_diff": 21},
        {"position": 2, "team": "Real Madrid CF", "points": 27, "played": 13, "goal_diff": 14},
        {"position": 3, "team": "Club Atlético de Madrid", "points": 25, "played": 13, "goal_diff": 11},
        {"position": 4, "team": "Villarreal CF", "points": 23, "played": 13, "goal_diff": 6},
        {"position": 5, "team": "Real Betis Balompié", "points": 21, "played": 13, "goal_diff": 3},
    ]
    item = resolve_team("Real Madrid CF")
    results.append(("英文队名可反查中文实体", bool(item) and item.get("name_zh") == "皇马"))
    # 关键：正文写全称「皇家马德里」、积分榜给英文名、规范名是「皇马」——三者必须能
    # 归一。否则相关性校验会把「正文写了全称」误判成「正文没提这队」，图就白丢。
    results.append(("正文全称能归一到同一实体",
                    bool(item) and "皇家马德里" in entity_names(item)))
    results.append(("中文全称可作为查询词反查", bool(resolve_team("皇家马德里"))))

    if item:
        c = chart_standings(item, table[1], table, "西甲", out_dir=tmp, filename="s.jpg")
        results.append(("积分榜图可渲染", bool(c) and Path(c["path"]).exists()))
        results.append(("积分榜图 anchor 带名次与积分",
                        bool(c) and set(c["anchor"]["numbers"]) == {"2", "27"}))
    else:
        results.append(("积分榜图可渲染", False))
        results.append(("积分榜图 anchor 带名次与积分", False))

    fixture = {
        "home_team": "Real Madrid CF", "away_team": "Valencia CF",
        "home_score": 2, "away_score": 1, "league": "西甲",
        "goals": [
            {"minute": 23, "scorer": "姆巴佩", "scorer_team": "home"},
            {"minute": 67, "scorer": "索莱尔", "scorer_team": "away"},
            {"minute": 88, "scorer": "贝林厄姆", "scorer_team": "home"},
        ],
    }
    g = chart_goals(fixture, out_dir=tmp, filename="g.jpg")
    results.append(("进球时间轴可渲染", bool(g) and Path(g["path"]).exists()))
    results.append(("进球图 anchor 两队齐全",
                    bool(g) and len(g["anchor"]["entities"]) == 2))

    # 负向：无进球事件必须返回 None，不能画一张空时间轴充数
    empty = chart_goals({**fixture, "goals": []}, out_dir=tmp, filename="e.jpg")
    results.append(("无进球事件返回 None（不画假图）", empty is None))

    # 负向：缺比分必须返回 None
    bad = chart_score({"home_team": "A队", "away_team": "B队"}, out_dir=tmp, filename="b.jpg")
    results.append(("缺比分返回 None", bad is None))

    # candidates：文章提到两队才出比赛图
    art_hit = {"title": "皇马2-1瓦伦西亚，姆巴佩破荒", "content": "皇家马德里主场迎战瓦伦西亚。"}
    got = candidates(art_hit, {"all_fixtures": [fixture]}, out_dir=tmp, prefix="hit")
    results.append(("正文提到两队 → 出比赛图", any(c["kind"] in ("goals", "score") for c in got)))

    art_miss = {"title": "英超争冠悬念", "content": "阿森纳与曼城的差距。"}
    got2 = candidates(art_miss, {"all_fixtures": [fixture]}, out_dir=tmp, prefix="miss")
    results.append(("正文没提两队 → 不出比赛图",
                    not any(c["kind"] in ("goals", "score") for c in got2)))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="正文事实数据图")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--fontstatus", action="store_true")
    args = ap.parse_args()

    if args.fontstatus:
        ok, detail = font_status()
        print(("OK " if ok else "FAIL ") + detail)
        return 0 if ok else 1
    if args.selftest:
        bad = 0
        for name, ok in selftest():
            print(("PASS " if ok else "FAIL ") + name)
            bad += 0 if ok else 1
        return 1 if bad else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
