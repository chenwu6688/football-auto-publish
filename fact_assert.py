"""事实断言校验：只拦「矛盾」，不拦「缺失」。

## 为什么要有这个模块

现行 `validate_article_vs_match_data` 的判据是**存在性**的：
「正文必须出现主队名」「必须出现客队名」「必须写出这个比分」。
一旦字面没命中就判失败 → 打回重写 → 重试用尽 → 熔断。

2026-10-12 至 11-05 共 4 次自动熔断，**4 次全是这个范式造成的误报**：

| 日期 | 现场 | 真实情况 |
|---|---|---|
| 10-12 | 缺少客队名: 拜仁慕尼黑 | 正文写「拜仁」，别名表未覆盖全称 |
| 10-13 | 缺少客队名: 大田市民首发 | 采集粘连「首发」，正文写简称「大田」 |
| 10-24 | 比分不一致: 蓉城 0-1 英博 | 正文写「0比1」，三个判定点各写各的正则 |
| 11-05 | 缺少客队名: 血洗 | 采集把赛果动词切进队名字段 |

这四次**没有一次是稿子写错了**，全部是「没按我们预期的字面写」。
而用户的核心诉求是「不改变原意、不臆造」——那是**矛盾**，不是**缺失**。
把「缺失」当「矛盾」拦，等于用误杀换安全感。

## 判据

- **矛盾（拦）**：正文与本场事实卡直接冲突。比分方向/数值不符、进球数断言超过实际、
  通篇只提了别的球队（疑似编造对阵）。
- **缺失（不拦，只记分）**：没写队名、没写比分。正文用「主队」「他们」指代是正常写法。
- **无法归因（不拦，记 suspicious）**：出现一个与本场不符的比分，但邻近没有任何队名，
  无法判断是「引用了别的比赛」还是「编造」。这类只降分，不拦——宁可漏一次，
  也不再用一条无法证伪的规则去停掉整批。

## 与既有代码的关系

不复制任何判定逻辑：比分正则、队名别名、最长匹配全部复用 `orchestrator` 的既有实现
（延迟导入避免循环依赖）。**同一份规则只准有一处实现**，这个模块只改变「怎么用」：
从「必须命中」改成「不得冲突」。

用法：
    import fact_assert
    res = fact_assert.verify(article, fixture, match_context=ctx)
    if not res["ok"]:            # 只在有矛盾时拦
        ...
    score = res["coverage"]      # 覆盖度不足只影响打分，不阻断

自检：`python3 fact_assert.py --selftest`
"""

from __future__ import annotations

import re

#: 比分窗口：以比分数字为中心，左右各看这么多字符来判断「这是哪两支队之间的比分」
_NEIGHBOR = 14

#: 进球数断言：命中即需要 goals 数据支撑（高 precision，保留原逻辑的精华）
_CLAIM_PATTERNS = (
    (re.compile(r"梅开二度"), 2),
    (re.compile(r"帽子戏法"), 3),
    (re.compile(r"大四喜"), 4),
    (re.compile(r"独造\s*(\d+)\s*球"), None),  # None = 用捕获的组号
)


def _orch():
    """延迟导入：orchestrator 体量大且未来会反过来调用本模块。"""
    import orchestrator
    return orchestrator


def _is_date_or_time(text: str, start: int, end: int) -> bool:
    """这个数字对是不是日期/时间的一部分（2026-11-05、15:30）。"""
    left = text[max(0, start - 6):start]
    right = text[end:end + 4]
    if re.search(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*$", left):
        return True
    if re.match(r"\s*[-/日号]", right) and re.search(r"\d{4}\s*[-/年]", left):
        return True
    if re.match(r"\s*(点|时|分|秒|分钟)", right):
        return True
    if re.search(r"(周|第)\s*$", left) and re.match(r"\s*轮", right):
        return True
    return False


def _scan_scores(text: str):
    """抽出比分形态并保留位置：[(start, end, a, b)]。

    复用 orchestrator._SCORE_RE 的形态定义（0-1 / 0比1 / 0：1 等价），
    但**在原文上取位置**——归一化会改变偏移量，窗口判断就失效了。
    """
    o = _orch()
    out = []
    for m in o._SCORE_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if _is_date_or_time(text, m.start(), m.end()):
            continue
        # 过滤「3-4名」「2-3个」这类量词用法：后面紧跟量词的不算比分
        if re.match(r"\s*(名|个|人|场|次|天|年|月|轮)", text[m.end():m.end() + 3]):
            continue
        out.append((m.start(), m.end(), a, b))
    return out


def _mentions(text: str, teams) -> list[tuple[int, int, str]]:
    """正文里所有球队提及的 (start, end, team)，按最长匹配消除子串串味。

    只做「谁在哪」，不做判定；判定在 `_score_verdict` 里。
    """
    o = _orch()
    spans = []
    for t in teams:
        if not t:
            continue
        for form in o._team_forms(t):
            if not form:
                continue
            start = text.find(form)
            while start != -1:
                spans.append((start, start + len(form), t))
                start = text.find(form, start + 1)
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    kept, covered = [], []
    for a, b, t in spans:
        if any(a >= x and b <= y for x, y in covered):
            continue
        kept.append((a, b, t))
        covered.append((a, b))
    return sorted(kept)


def _nearest(mentions, pos: int, window: int, *, before: bool):
    """离 pos 最近的球队提及（只在 window 距离内），返回 (team, distance)。"""
    best, best_d = "", 10 ** 6
    for a, b, t in mentions:
        if before and b <= pos:
            d = pos - b
        elif not before and a >= pos:
            d = a - pos
        else:
            continue
        if d < best_d:
            best, best_d = t, d
    if best_d > window:
        return "", 10 ** 6
    return best, best_d


def _scan_claims(content: str, known_players=()):
    o = _orch()
    out = []
    for pat, need in _CLAIM_PATTERNS:
        for m in pat.finditer(content):
            # 断言的主体：往前找最近出现的一个已知球员名
            head = content[max(0, m.start() - 20):m.start()]
            who = ""
            for p in known_players:
                idx = head.rfind(p)
                if idx >= 0:
                    who = p
                    break
            if not who:
                continue
            n = int(m.group(1)) if need is None else need
            out.append((who, n))
    return out


def verify(article: dict, fixture: dict, match_context=None, *, expected=None) -> dict:
    """校验一篇稿子与本场事实是否**冲突**。

    返回::

        {
          "ok": bool,                 # False 仅表示"有矛盾"
          "judged": bool,             # 有没有足够事实可判（无事实 → 不判，不拦）
          "contradictions": [...],    # 拦
          "suspicious": [...],        # 记分不拦
          "missing": [...],           # 记分不拦
          "coverage": 0.0,            # 事实覆盖度 0~1（只用于打分/观察）
        }
    """
    o = _orch()
    content = ((article or {}).get("content") or "") + (article or {}).get("title", "") or ""
    home = (fixture or {}).get("home_team") or ""
    away = (fixture or {}).get("away_team") or ""
    hg = (fixture or {}).get("home_score")
    ag = (fixture or {}).get("away_score")

    # 队名先清洗（赛果动词粘连）——复用既有实现，不重写一份
    home = o._clean_team(home) if hasattr(o, "_clean_team") else home
    away = o._clean_team(away) if hasattr(o, "_clean_team") else away
    if home and not o._looks_like_team(home):
        home = ""
    if away and not o._looks_like_team(away):
        away = ""

    contradictions, suspicious, missing = [], [], []
    fixture_teams = [t for t in (home, away) if t]

    # 事实不够 → 不判（未判定 ≠ 通过，调用方必须把 judged 记进台账）
    if not fixture_teams and hg is None:
        return {"ok": True, "judged": False, "contradictions": [], "suspicious": [],
                "missing": ["事实卡为空"], "coverage": 0.0}

    # ── 1. 对阵：只提别的队 = 疑似编造对阵；没提本场队 = 缺失（不拦）
    mentioned_fixture = [t for t in fixture_teams if o._team_in_content(t, content)]
    known_here = [t for t in o._teams_in_content(content, o._KNOWN_TEAMS)] if hasattr(o, "_KNOWN_TEAMS") else []
    if fixture_teams and not mentioned_fixture and known_here:
        contradictions.append({
            "type": "team_mismatch",
            "detail": f"正文出现的是 {'/'.join(known_here[:3])}，与事实卡 {'/'.join(fixture_teams)} 无关",
        })
    for t in fixture_teams:
        if t not in mentioned_fixture:
            missing.append(f"未提及 {t}")

    # ── 2. 比分：方向 + 数值（这是原实现最容易漏的一处）
    if hg is not None and ag is not None:
        hit_fixture_score = False
        universe = list(fixture_teams)
        if hasattr(o, "_KNOWN_TEAMS"):
            universe = list(dict.fromkeys(universe + list(o._KNOWN_TEAMS)))
        mentions = _mentions(content, universe)
        for start, end, a, b in _scan_scores(content):
            left, _ = _nearest(mentions, start, _NEIGHBOR, before=True)
            right, _ = _nearest(mentions, end, _NEIGHBOR, before=False)
            # 只要比分两侧出现**本场之外的球队**，这条比分就是另一场比赛的（旧账/他场），
            # 整条跳过。2026-11-05 的正文里就有「4-0赢曼联的旧账」——原实现会把它
            # 归到 13 字前的「科莫」头上，判成「科莫进 4 球」。
            strangers = [t for t in (left, right)
                         if t and not any(o._same_team(t, f) for f in fixture_teams)]
            if strangers:
                continue
            if not left and not right:
                # 归因不了：可能是「上轮 2-1」这类旧账，只记 suspicious
                if (a, b) not in ((int(hg), int(ag)), (int(ag), int(hg))):
                    suspicious.append(f"无法归因的比分 {a}-{b}（事实 {hg}-{ag}）")
                continue
            # 只有左队识别出来时，按左队的角色校验单侧数值
            if left and not right:
                if o._same_team(left, home):
                    if a == int(hg):
                        hit_fixture_score = True
                    else:
                        contradictions.append({
                            "type": "score_value",
                            "detail": f"{left} 进球写作 {a}，事实卡是 {hg}",
                        })
                elif o._same_team(left, away):
                    if a == int(ag):
                        hit_fixture_score = True
                    else:
                        contradictions.append({
                            "type": "score_value",
                            "detail": f"{left} 进球写作 {a}，事实卡是 {ag}",
                        })
                continue
            if not left and right:
                if o._same_team(right, home):
                    if b == int(hg):
                        hit_fixture_score = True
                    else:
                        contradictions.append({
                            "type": "score_value",
                            "detail": f"{right} 失球写作 {b}，事实卡是 {hg}",
                        })
                elif o._same_team(right, away):
                    if b == int(ag):
                        hit_fixture_score = True
                    else:
                        contradictions.append({
                            "type": "score_value",
                            "detail": f"{right} 失球写作 {b}，事实卡是 {ag}",
                        })
                continue
            # 两侧都识别出来 → 严格按「左队是主还是客」比对方向
            if o._same_team(left, right):
                continue
            if o._same_team(left, home):
                want = (int(hg), int(ag))
            else:
                want = (int(ag), int(hg))
            if (a, b) == want:
                hit_fixture_score = True
            else:
                contradictions.append({
                    "type": "score_direction",
                    "detail": f"{left} {a}-{b} {right}，事实卡是 {home} {hg}-{ag} {away}",
                })
        if not hit_fixture_score:
            missing.append(f"未写出比分 {hg}-{ag}")

    # ── 3. 进球数断言：高 precision，超过实际即矛盾
    players = []
    for g in (fixture or {}).get("goals", []) or []:
        raw = g.get("scorer_name", g.get("scorer", ""))
        if raw:
            name = o._clean_player_name(raw)[0] if hasattr(o, "_clean_player_name") else raw
            if name:
                players.append(name)
    if expected and expected.get("scorers"):
        players.extend([s for s in expected["scorers"] if s])
    if players:
        counts = {}
        for g in (fixture or {}).get("goals", []) or []:
            raw = g.get("scorer_name", g.get("scorer", ""))
            if raw:
                name = o._clean_player_name(raw)[0] if hasattr(o, "_clean_player_name") else raw
                if name:
                    counts[name] = counts.get(name, 0) + 1
        for who, claimed in _scan_claims(content, tuple(set(players))):
            actual = counts.get(who, 0)
            if claimed > actual:
                contradictions.append({
                    "type": "player_claim",
                    "detail": f"{who} 实际 {actual} 球，正文称 {claimed}",
                })

    covered = 0
    total = len(fixture_teams) + (1 if hg is not None else 0)
    covered += len(mentioned_fixture)
    if hg is not None and not any(m.startswith("未写出比分") for m in missing):
        covered += 1
    coverage = (covered / total) if total else 0.0

    # 去重：标题与正文拼接后同一条比分会被扫两次（同一事实报两条），
    # 会让人误以为「一篇稿子犯了两个错」——矛盾计数是复盘依据，不能虚高。
    _seen, _uniq = set(), []
    for c in contradictions:
        k = (c.get("type"), c.get("detail"))
        if k in _seen:
            continue
        _seen.add(k)
        _uniq.append(c)

    return {
        "ok": not _uniq,
        "judged": True,
        "contradictions": _uniq,
        "suspicious": suspicious,
        "missing": missing,
        "coverage": round(coverage, 2),
    }


# ============================================================
# 自检：4 个历史熔断现场必须放过，4 个真矛盾必须拦住
# ============================================================

def _selftest() -> int:
    """跑一遍正反用例。任何一条判反即返回 1。"""
    import orchestrator as o

    def fx(home, away, hg, ag, goals=None):
        return {"home_team": home, "away_team": away, "home_score": hg,
                "away_score": ag, "goals": goals or []}

    cases = [
        # (标签, fixture, 正文, 期望 ok)
        ("10-12 拜仁全称", fx("马竞", "拜仁慕尼黑", 0, 2),
         "马竞在主场0-2输给拜仁，防线两次被打穿。", True),
        ("10-13 大田简称", fx("海港", "大田市民首发", 3, 1),
         "海港3-1赢球，大田首发名单里有朴规现。", True),
        ("10-24 比分中文写法", fx("蓉城", "英博", 0, 1),
         "蓉城0比1输给英博，领先3分却掉了链子。", True),
        ("11-05 赛果动词粘连", fx("朗斯", "科莫血洗", 1, 9),
         "朗斯在主场1-9被科莫血洗，法布雷加斯赛后只提了4-0赢曼联的旧账。", True),
        # 真矛盾：必须拦
        ("比分写反", fx("蓉城", "英博", 0, 1),
         "蓉城1-0英博，拿下关键三分。", False),
        ("比分数值造假", fx("朗斯", "科莫", 1, 9),
         "朗斯主场0-2输给科莫，全场被压制。", False),
        ("进球数造假", fx("皇马", "巴萨", 2, 1, [{"scorer_name": "姆巴佩"}]),
         "皇马2-1巴萨，姆巴佩梅开二度。", False),
        ("编造对阵", fx("蓉城", "英博", 0, 1),
         "皇马3-0大胜巴萨，姆巴佩独造3球。", False),
        # 缺失：不拦，只记 coverage
        ("通篇不提队名", fx("蓉城", "英博", 0, 1),
         "这场比赛的过程比比分更难看，主队上半场就乱了。", True),
    ]

    bad = []
    for label, fixture, body, want in cases:
        res = verify({"title": "", "content": body}, fixture)
        got = res["ok"]
        flag = "✅" if got == want else "❌"
        print(f"  {flag} {label:16s} ok={got!s:5s} 期望={want!s:5s} "
              f"矛盾={len(res['contradictions'])} 缺失={len(res['missing'])} "
              f"覆盖={res['coverage']}")
        if got != want:
            bad.append((label, res))

    # 空事实卡 = 未判定，不能算通过
    empty = verify({"content": "随便一段话"}, fx("", "", None, None))
    if empty["judged"]:
        bad.append(("空事实卡必须标未判定", empty))
    print(f"  {'✅' if not empty['judged'] else '❌'} 空事实卡 → judged={empty['judged']}（期望 False）")

    # 覆盖率：缺失场景必须真的记到 0 附近，而不是静默当成满分
    low = verify({"content": "这场比赛很乱。"}, fx("蓉城", "英博", 0, 1))
    if low["coverage"] > 0.1:
        bad.append(("缺失必须反映到 coverage", low))
    print(f"  {'✅' if low['coverage'] <= 0.1 else '❌'} 缺失场景 coverage={low['coverage']}（期望 ≈0）")

    print()
    if bad:
        print(f"❌ 自检失败 {len(bad)} 项")
        for label, res in bad:
            print("   -", label, res)
        return 1
    print(f"✅ 事实断言自检通过：{len(cases) + 2} 项（4 个历史熔断现场全部放过，4 个真矛盾全部拦住）")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
