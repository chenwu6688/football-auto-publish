#!/usr/bin/env python3
"""标题 brief —— 把「先取两个数字，再组句」变成可执行的输入（P1-2，2026-11-04）。

## 为什么要有这一层

2026-11-03 的教训是：**改口径要先 grep 供给侧**。这次要求标题含「数字 / 对比 / 专名」，
如果只在出口加闸门、让模型自己去找数字，结果一定是两类：
  a) 模型从记忆里编一个数字（编造的比分比写得平淡危险得多）；
  b) 模型反复写不过被打回，稿子被丢掉（产能白烧）。

所以供给侧要先把**事实卡里现成的数字**摊在模型面前，让它「挑两个」而不是「想两个」：

    可引用数字：国安 4-7（亚冠第3轮）、第9位/12分、近5轮1胜、张玉宁7球
    可检索专名：北京国安 / 泰港 / 亚冠 / 张玉宁
    可写对照：4-7（比分）/ 第9位 vs 第3位 / 近5轮1胜4负

## 纪律

- **只搬运、不生成**：本模块只做「从 topic / 事实卡里取已有数字」，绝不补默认值、
  绝不四舍五入、绝不把「没有」写成 0。取不到就返回空列表，prompt 里明说「无可用数字」，
  由模型退回到「专名+对比」路径（三要素下限是 2 项）。
- 事实卡没有的（历史交锋、转会费、伤停天数）一律不进 brief——那是编造入口。
- `candidates()` 出的兜底标题只用于「标题重拟失败但正文可用」的场景，
  **必须是机械拼接**（不改写、不抒情），保证不新增事实。

命令行：
    python3 title_brief.py --selftest
"""

from __future__ import annotations

import argparse
import re

import title_elements

# 兜底标题模板：只用 {home}/{away}/{cmp}/{hn}/{an} 这类已核实的字段
_TEMPLATES = (
    "{comp}：{home}{hs}分对{away}{as_}分，{conflict}",
    "{home}vs{away}：{hs}分对{as_}分，{conflict}",
    "{comp}{round}：{home}{hs}分，{away}{as_}分，{conflict}",
    "{home}{hs}分，{away}{as_}分，{conflict}这场怎么看",
)


def _num(v) -> int | None:
    """只接受真实数值；None/''/-1 占位符一律当缺失（项目硬纪律：-1 = 缺）。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if v >= 0 else None
    return None


def _clean(text: str, limit: int = 14) -> str:
    """把冲突点压成可进标题的短句：去标点、去空白、截断。"""
    t = re.sub(r"[，。！？、；：\s\"'「」『』《》()（）]", "", str(text or ""))
    return t[:limit]


def collect(topic: dict, facts: dict | None = None) -> dict:
    """从选题与事实卡里收集「可引用数字 / 可检索专名 / 可写对照」。

    返回 {"numbers": [...], "proper": [...], "contrasts": [...], "missing": [...]}；
    每一项都是**可直接写进标题的字符串**，空列表表示确实没有，不是 0。
    """
    topic = topic or {}
    facts = dict(topic) if facts is None else dict(facts)
    for k, v in topic.items():           # topic 有的字段优先（更贴近本条选题）
        facts.setdefault(k, v)

    home = str(facts.get("home_zh") or "").strip()
    away = str(facts.get("away_zh") or "").strip()
    comp = str(facts.get("competition") or facts.get("league") or "").strip()

    numbers: list[str] = []
    contrasts: list[str] = []

    # 比分（战报/结果稿）
    sc = facts.get("score") or {}
    hg, ag = _num(sc.get("home")), _num(sc.get("away"))
    if hg is None:
        hg, ag = _num(facts.get("home_score")), _num(facts.get("away_score"))
    if hg is not None and ag is not None:
        numbers.append(f"{home}{hg}-{ag}{away}" if home and away else f"{hg}-{ag}")
        contrasts.append(f"{hg}-{ag} 的比分对照")

    # 积分与排名（赛程前瞻）
    hs, as_ = facts.get("home_standing") or {}, facts.get("away_standing") or {}
    hp, ap = _num(hs.get("points")), _num(as_.get("points"))
    hpos, apos = _num(hs.get("position")), _num(as_.get("position"))
    if hp is not None:
        numbers.append(f"{home}{hp}分" if home else f"{hp}分")
    if ap is not None:
        numbers.append(f"{away}{ap}分" if away else f"{ap}分")
    if hp is not None and ap is not None:
        contrasts.append(f"{hp}分对{ap}分")
    if hpos is not None and apos is not None:
        numbers.append(f"第{hpos}位对第{apos}位")
        contrasts.append(f"第{hpos}位 vs 第{apos}位")

    # 净胜球
    hgd, agd = _num(hs.get("gd")), _num(as_.get("gd"))
    if hgd is not None and agd is not None and (hgd or agd):
        contrasts.append(f"净胜球{hgd}对{agd}")

    # 轮次 / 半场 / 进球明细
    rnd = _num(facts.get("round")) or _num(facts.get("matchday"))
    if rnd:
        numbers.append(f"第{rnd}轮")
    ht_h, ht_a = _num(sc.get("ht_home")), _num(sc.get("ht_away"))
    if ht_h is not None and ht_a is not None:
        numbers.append(f"半场{ht_h}-{ht_a}")

    goals = facts.get("goals") or []
    scorers = [str(g.get("player_zh") or "").strip() for g in goals if g.get("player_zh")]
    top = facts.get("home_top_scorer") or facts.get("away_top_scorer")
    if isinstance(top, dict) and top.get("player_zh") and _num(top.get("goals")) is not None:
        numbers.append(f"{top['player_zh']}{_num(top['goals'])}球")

    # 近期战绩（筛选项里有 streak 才用；没有不编）
    streak = facts.get("streak") or facts.get("recent_record")
    if isinstance(streak, str) and re.search(r"\d", streak):
        numbers.append(streak[:12])

    # 专名：队名 / 赛事 / 进球者 / 关键词里的人工实体
    proper = [x for x in (home, away, comp) if x]
    for nm in scorers[:3]:
        if nm and nm not in proper:
            proper.append(nm)
    for kw in (topic.get("keywords_cn") or [])[:4]:
        kw = str(kw).strip()
        if kw and len(kw) >= 2 and kw not in proper:
            proper.append(kw)

    missing = []
    if not numbers:
        missing.append("数字")
    if not contrasts:
        missing.append("对比")
    if not proper:
        missing.append("专名")

    # 去重保序
    def _dedup(xs):
        out = []
        for x in xs:
            if x and x not in out:
                out.append(x)
        return out

    return {"numbers": _dedup(numbers), "proper": _dedup(proper),
            "contrasts": _dedup(contrasts), "missing": missing}


def brief_text(topic: dict, facts: dict | None = None, *, limit: int = 6) -> str:
    """渲染成 prompt 里的一段「标题素材」（没有就明说没有，绝不填 0）。"""
    c = collect(topic, facts)
    lines = ["## 标题素材（只能从这里挑，不得新编）"]
    lines.append("- 可引用数字：" + ("、".join(c["numbers"][:limit]) or "（事实卡里没有可用数字，"
                                "本条只能走「专名+对比」路径，禁止自己造数）"))
    lines.append("- 可检索专名：" + ("、".join(c["proper"][:limit]) or "（无）"))
    lines.append("- 可写对照：" + ("、".join(c["contrasts"][:limit]) or "（无明确对照，可用「A vs B」明示对比）"))
    lines.append("- 组句顺序：**先选定 1 个数字 + 1 个专名（有对照就再加 1 个对照），"
                 "再写成一句话**；不要先写句子再往里塞数字。")
    return "\n".join(lines)


def candidates(topic: dict, facts: dict | None = None, *, limit: int = 4) -> list[str]:
    """机械拼接的兜底标题（只在重拟失败时用，保证不新增事实）。

    只返回能通过三要素闸门的候选；一个都拼不出来返回空列表（调用方据此保留原稿）。
    """
    c = collect(topic, facts)
    facts = facts or topic or {}
    home = str(facts.get("home_zh") or "").strip()
    away = str(facts.get("away_zh") or "").strip()
    comp = str(facts.get("competition") or facts.get("league") or "").strip()
    hs = next((n for n in c["numbers"] if n.startswith(home or "\x00") and n.endswith("分")), "")
    as_ = next((n for n in c["numbers"] if n.startswith(away or "\x00") and n.endswith("分")), "")
    hs = hs[len(home):] if home and hs.startswith(home) else hs
    as_ = as_[len(away):] if away and as_.startswith(away) else as_
    if not hs or not as_:
        hpos = next((n for n in c["numbers"] if n.startswith("第") and "位对" in n), "")
        if hpos:
            m = re.match(r"第(\d+)位对第(\d+)位", hpos)
            hs, as_ = (f"第{m.group(1)}位" if m else ""), (f"第{m.group(2)}位" if m else "")
    rnd = next((n for n in c["numbers"] if re.fullmatch(r"第\d+轮", n)), "")
    conflict = _clean(facts.get("conflict") or facts.get("why_pick") or "")

    out: list[str] = []
    for tpl in _TEMPLATES:
        if not (home and away and hs and as_):
            break
        try:
            t = tpl.format(comp=comp or "联赛", home=home, away=away, hs=hs, as_=as_,
                           round=rnd or "", conflict=conflict)
        except (KeyError, IndexError):
            continue
        t = re.sub(r"\s+", "", t).strip("，,：:")
        if len(t) < 12 or t in out:
            continue
        if title_elements.check(t)["ok"]:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def selftest() -> list[tuple[str, bool]]:
    """用例来自真实 topic/facts 形态；改字段口径必须同步改这里。"""
    cases: list[tuple[str, bool]] = []

    fx = {"home_zh": "北京国安", "away_zh": "泰港", "competition": "亚冠",
          "home_standing": {"position": 9, "points": 12, "gd": -2},
          "away_standing": {"position": 3, "points": 19, "gd": 5},
          "conflict": "中超球队亚冠客场连续崩盘，防守体系早有隐患"}
    c = collect(fx)
    cases.append(("积分进数字", "北京国安12分" in c["numbers"]))
    cases.append(("积分对照进对比", "12分对19分" in c["contrasts"]))
    cases.append(("排名进数字", "第9位对第3位" in c["numbers"]))
    cases.append(("专名含双方队名", {"北京国安", "泰港", "亚冠"} <= set(c["proper"])))

    # 缺数据时返回空列表，不填 0（项目硬纪律）
    c2 = collect({"home_zh": "甲队", "away_zh": "乙队"})
    cases.append(("无比分/积分 → numbers 为空而不是 0", c2["numbers"] == []))
    cases.append(("缺失项如实上报", "数字" in c2["missing"] and "对比" in c2["missing"]))

    # 占位符 -1 必须当缺失，不能写成「-1 分」
    c3 = collect({"home_zh": "甲队", "away_zh": "乙队",
                  "home_standing": {"position": -1, "points": -1},
                  "away_standing": {"position": -1, "points": -1}})
    cases.append(("占位符 -1 不当数据", c3["numbers"] == []))

    # brief_text 在没有数字时必须明说「禁止自己造数」
    txt = brief_text({"home_zh": "甲队", "away_zh": "乙队"})
    cases.append(("无数字时 prompt 明说禁止造数", "禁止自己造数" in txt))

    # 兜底标题必须过三要素闸门，且不新增事实
    cands = candidates(fx)
    cases.append(("兜底标题非空", len(cands) >= 1))
    cases.append(("兜底标题全部过三要素", all(title_elements.check(t)["ok"] for t in cands)))
    cases.append(("兜底标题含双方队名", all(("北京国安" in t and "泰港" in t) for t in cands)))
    cases.append(("兜底标题不出现编造数字（只有 12/19/9/3）",
                  all(not re.search(r"\d+", t.replace("12", "").replace("19", "")
                                    .replace("9", "").replace("3", "")) for t in cands)))
    # 连队名都没有 → 拼不出候选，返回空（不硬造）
    cases.append(("无队名时不给兜底标题", candidates({}) == []))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="标题素材 brief（只搬运事实卡里的数字）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if not args.selftest:
        ap.print_help()
        return 0
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
