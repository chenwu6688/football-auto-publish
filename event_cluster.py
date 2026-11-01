#!/usr/bin/env python3
"""单事件多切口编排（P0-2，2026-11-01 头部账号对标）。

## 病根

同一个金球奖，2026-10-26 那天：

    我爱英超（10.3 万粉）  15 条
    英超那些事（15 万粉）  —— 同事件亦有覆盖
    我方（~0 粉）          1 条

不是「写得少」，是**一个事件只写一个角度**。头部账号的 15 条拆开来是这样的：

    结果角度   「凯恩拿下2026金球奖，第70届颁奖礼伦敦落幕」
    数据角度   「73球！凯恩这个金球奖，数据比梅西那年还硬」
    冲突角度   「英格兰等了25年，这次终于不用再吵该给谁」
    人物情绪   「凯恩上台第一句谢的是穆里尼奥」
    影响角度   「这座奖杯之后，拜仁的更衣室要变天了」
    历史对比   「上一次英格兰人拿金球，还是2001年的欧文」

六类角度并不是我拍脑袋分的，是从那 15 条标题反推出来的分类。同一个事实基础，
换一个切口就是一条独立的稿子，读者不会觉得重复——而一个角度写一篇，
当天就只剩一条稿子去和 15 条抢流量。

## 本模块做什么

只做三件事，不碰生成：

1. ``classify_angle(title)``：把标题/角度归入六类之一（规则判定，可单测）；
2. ``s_events()``：找出「今天该做多切口」的 S 级节点事件；
3. ``plan_angles(event, used_angles)``：产出 3~4 个角度选题（含角度指令与差异化约束）。

## 四条硬约束（都在代码里，不靠自觉）

- **仅 S 级事件**：`config/season_calendar.yaml` 的节点需显式写 ``level: S``。
  刻意不接受「A 级也凑合」——逐日都多切口就是超发，正是 10 月产能失控的老路。
- **必须有已核实事实**（``facts``）：没有事实做多角度，等于用三个角度说三遍空话。
- **占配额不超发**：角度稿占用批次 ``max_articles`` 槽位，并给主稿留位。
- **同事件互不重复标题句式**：``distinct_enough()`` 检查，重复即拒。

纪律：配置/日历读不到 → 返回空（不触发），绝不因读不到配置而臆造事件。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
CALENDAR_PATH = PROJECT_ROOT / "config" / "season_calendar.yaml"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

CST = timezone(timedelta(hours=8))

# 单个 S 级事件最多出几篇（计划：3~4 篇；取 3 是保守值——4 篇时第 4 篇通常已无新信息）
MAX_ANGLES_PER_EVENT = 3
# 单日多切口稿上限（多个 S 级事件同日时也不能无限放大）
MAX_ANGLES_PER_DAY = 4

# 六类角度：名称 → (判定关键词, 写作指令)
# 顺序即优先级：一条标题同时像「结果」和「数据」时，按结果计（先发生的信息层次更外显）。
ANGLE_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("结果", ("获奖", "夺魁", "当选", "摘得", "捧起", "拿下", "加冕", "冠军", "首夺"),
     "只写「谁、凭什么拿下」这一件事：先把结果说清，再用一个具体事实解释它为什么站得住。"),
    ("冲突", ("争议", "质疑", "不服", "炮轰", "骂", "吵", "怒", "该不该", "配不上", "值不值"),
     "只写争议双方：谁不服、不服的理由是什么、另一方怎么回。不要下结论，把火留给评论区。"),
    ("人物情绪", ("哭", "落泪", "哽咽", "拥抱", "感谢", "谢", "母亲", "父亲", "家人", "儿子", "妻子"),
     "只写人：一个具体场景 + 一句原话式的细节，写他的处境和代价，不写评选逻辑。"),
    ("影响", ("意味着", "接下来", "下赛季", "未来", "转会", "身价", "地位", "格局", "之后", "更衣室"),
     "只写「这事之后会怎样」：对球队、对同行、对他自己的位置各推一步，落点必须是可验证的推断。"),
    ("历史对比", ("上一次", "当年", "时隔", "历史", "追平", "比肩", "第一人", "回顾", "等了", "年后"),
     "只写时间轴：把这次放进历史序列里比较，给出具体年份与人物，别做抒情。"),
    # 「数据」刻意放最后：它的信号词（球/场/分/榜）几乎出现在任何足球标题里，
    # 放前面会把「金球奖引发争议」这类标题一律抢成数据角度（实测踩过）。
    ("数据", ("数据", "助攻", "纪录", "胜率", "排名", "榜", "进球", "球"),
     "只写数据本身：挑 2~3 个最有反差的具体数字，横向或纵向比一比，别复述结果。"),
)

ANGLES: tuple[str, ...] = tuple(a for a, _, _ in ANGLE_RULES)
_ANGLE_HINT = {a: h for a, _, h in ANGLE_RULES}


# ------------------------------------------------------------------ 角度判定
def classify_angle(text: str) -> str | None:
    """把标题/角度描述归入六类之一；都不命中返回 None（不硬归类）。"""
    t = (text or "").strip()
    if not t:
        return None
    if t in ANGLES:
        # 直接传角度名也算（plan_angles 的 used_titles 既接受标题，也接受角度名）
        return t
    for name, kws, _ in ANGLE_RULES:
        if any(k in t for k in kws):
            return name
    return None


def angle_hint(angle: str) -> str:
    return _ANGLE_HINT.get(angle, "围绕核心事实给一个明确切口，不铺开写全景。")


def angles_of(titles) -> list[str]:
    """一组标题覆盖了哪些角度（去重，按 ANGLES 顺序）。"""
    got: set[str] = set()
    for t in titles or ():
        a = classify_angle(t)
        if a:
            got.add(a)
    return [a for a in ANGLES if a in got]


def missing_angles(titles, max_angles: int = MAX_ANGLES_PER_EVENT) -> list[str]:
    """按优先级挑出「还没写过的角度」。"""
    used = set(angles_of(titles))
    out = [a for a in ANGLES if a not in used]
    return out[:max_angles]


# ------------------------------------------------------------------ 事件
def is_s_event(node: dict) -> bool:
    """S 级事件判定：必须显式写 ``level: S``。

    不接受推断——``awareness: A`` 是「认知度」，不是「事件量级」，
    两者混用会把每天的小节点都变成多切口日（10 月超发就是这么来的）。
    """
    return str((node or {}).get("level", "")).strip().upper() == "S"


def load_calendar(path: Path | None = None) -> dict:
    p = Path(path) if path else CALENDAR_PATH
    if not p.exists():
        return {"nodes": []}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"nodes": []}
    data.setdefault("nodes", [])
    return data


def _as_date(x) -> date | None:
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    try:
        return datetime.strptime(str(x), "%Y-%m-%d").date()
    except Exception:
        return None


def s_events(today: str | date | None = None, calendar: dict | None = None) -> list[dict]:
    """今天该做多切口编排的 S 级事件。

    入选条件（全部满足）：
      1. ``level: S``；
      2. 节点日就是今天（提前几天预生成的多角度是「猜」，结果都还没发生）；
      3. 有 ``facts``（没有已核实事实 → 三个角度只会说三遍空话）。

    返回 [{node_id, label, node_date, facts, column}]，按 node_date 排序。
    """
    d = _as_date(today) or datetime.now(CST).date()
    cal = calendar if calendar is not None else load_calendar()
    out: list[dict] = []
    for n in cal.get("nodes", []) or []:
        if not isinstance(n, dict) or not is_s_event(n):
            continue
        nd = _as_date(n.get("date"))
        if nd != d:
            continue
        facts = [str(f).strip() for f in (n.get("facts") or []) if str(f).strip()]
        if not facts:
            continue
        out.append({
            "node_id": n.get("id", ""),
            "event_key": f"node:{n.get('id', '')}",
            "label": n.get("label") or n.get("type") or "",
            "node_date": nd.isoformat(),
            "column": n.get("column") or "战术榜单",
            "facts": facts,
            "fact_source_url": n.get("fact_source_url") or "",
        })
    out.sort(key=lambda e: e.get("node_date", ""))
    return out


def event_key_of(topic: dict) -> str | None:
    """事件的唯一键：节点事件用 ``node:<id>``，结果稿沿用 ``event_result_key``。

    刻意**不做关键词聚类**：把两件不同的事并成一个事件，会产出一组互相矛盾的稿子，
    这种错误在发布后无法挽回。宁可少触发。
    """
    t = topic or {}
    if t.get("node_id"):
        return f"node:{t['node_id']}"
    if t.get("_event_result_key"):
        return str(t["_event_result_key"])
    return None


# ------------------------------------------------------------------ 角度计划
def plan_angles(event: dict, used_titles=None, *, max_angles: int = MAX_ANGLES_PER_EVENT) -> list[dict]:
    """为一个 S 级事件产出角度选题（只产出计划，不含正文）。

    used_titles：本事件此前已发的标题，用来避开已用角度。
    返回 [{event_key, angle, angle_hint, title_seed, column, facts, node_id}]。
    """
    ev = event or {}
    if not ev.get("facts"):
        return []
    picked = missing_angles(used_titles or [], max_angles=max_angles)
    out = []
    for a in picked:
        out.append({
            "event_key": ev.get("event_key") or event_key_of(ev) or "",
            "node_id": ev.get("node_id", ""),
            "angle": a,
            "angle_hint": angle_hint(a),
            "title_seed": f"{ev.get('label', '')}·{a}",
            "column": ev.get("column", "战术榜单"),
            "facts": list(ev.get("facts") or []),
            "fact_source_url": ev.get("fact_source_url", ""),
        })
    return out


def distinct_enough(titles) -> dict:
    """同事件多篇必须互不重复标题句式。

    返回 {"ok", "dups": [(sig, [title...])], "heads": [(前6字, 次数)]}。
    句式判定复用 ``template_gate.signature_of``（与全仓同一套口径），
    另外查一遍前 6 字——句式表覆盖不到的新句式，靠开头撞车也能挡住。
    """
    import template_gate

    lst = [t for t in (titles or ()) if (t or "").strip()]
    by_sig: dict[str, list[str]] = {}
    by_head: dict[str, int] = {}
    for t in lst:
        sig = template_gate.signature_of(t)
        if sig:
            by_sig.setdefault(sig, []).append(t)
        # 开头比较前先去掉标点空白：中文标题里「金球奖揭晓，凯恩…」与「金球奖揭晓凯恩…」
        # 读者眼里是同一个开头，按字符硬取前 6 位却差在第 6 个字符是逗号，会漏判。
        head = re.sub(r"[\s，。！？、：；,.!?:;\"'（）()《》]", "", t)[:6]
        by_head[head] = by_head.get(head, 0) + 1
    dups = [(s, ts) for s, ts in by_sig.items() if len(ts) > 1]
    heads = [(h, n) for h, n in by_head.items() if n > 1]
    return {"ok": not dups and not heads, "dups": dups, "heads": heads, "n": len(lst)}


def audit(payload: dict) -> Path | None:
    """把本次多切口决策落盘，便于事后核对「今天为什么出了 3 篇同一事件」。"""
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        p = AUDIT_DIR / "event_cluster_latest.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return p
    except Exception:
        return None


# ------------------------------------------------------------------ 自检
def selftest() -> list[tuple[str, bool]]:
    """用例全部来自我爱英超金球奖 15 条真实标题（2026-11-01 抓取）。"""
    cases: list[tuple[str, bool]] = []

    cases.append(("角度池 6 类", len(ANGLES) == 6))
    cases.append(("六类都有写作指令", all(angle_hint(a) for a in ANGLES)))

    real = {
        "凯恩拿下2026金球奖，第70届颁奖礼在伦敦落幕": "结果",
        "73球！凯恩这个金球奖的数据有多硬": "数据",
        "上一次英格兰人拿金球，还是2001年的欧文": "历史对比",
        "凯恩上台第一句谢的是穆里尼奥": "人物情绪",
        "这座奖杯之后，拜仁更衣室要变天了": "影响",
        "梅西不服？金球奖评选再次引发争议": "冲突",
        # 一条标题常同时具备两类信号（这条既有「吵」也有「等了25年」），
        # 按优先级取最外显的一个；只要同一事件的多条被分到不同角度就达到目的
        "英格兰等了25年，这次终于不用再吵该给谁": "冲突",
    }
    for title, want in real.items():
        got = classify_angle(title)
        cases.append((f"角度判定：{title[:16]}→{want}", got == want))

    cases.append(("空标题不归类", classify_angle("") is None))
    cases.append(("无信号标题不硬归类", classify_angle("今天的天气不错") is None))

    # S 级判定：A 不是 S（这是最容易搞混的一条）
    cases.append(("level=S 判为 S 级", is_s_event({"level": "S"}) is True))
    cases.append(("level=s 小写也认", is_s_event({"level": "s"}) is True))
    cases.append(("awareness=A 不等于 S 级（不得混用）",
                  is_s_event({"awareness": "A"}) is False))
    cases.append(("缺 level 不算 S 级", is_s_event({}) is False))

    # s_events：节点日必须是今天 + 必须有 facts
    cal = {"nodes": [
        {"id": "x1", "date": "2026-11-01", "level": "S", "label": "事件一",
         "facts": ["事实A", "事实B"]},
        {"id": "x2", "date": "2026-11-01", "level": "S", "label": "无事实事件"},
        {"id": "x3", "date": "2026-11-05", "level": "S", "label": "未来事件", "facts": ["f"]},
        {"id": "x4", "date": "2026-11-01", "level": "A", "label": "普通节点", "facts": ["f"]},
    ]}
    evs = s_events("2026-11-01", cal)
    cases.append(("s_events 只收今天+S 级+有事实", [e["node_id"] for e in evs] == ["x1"]))
    cases.append(("无事实的 S 级事件不入选", all(e["node_id"] != "x2" for e in evs)))
    cases.append(("未来 S 级事件不提前多切口", all(e["node_id"] != "x3" for e in evs)))

    # 角度计划
    ev = evs[0] if evs else {"event_key": "node:x1", "facts": ["f"], "label": "事件一"}
    plan = plan_angles(ev, used_titles=["凯恩拿下2026金球奖"])
    cases.append(("有事实 → 产出角度计划", len(plan) == MAX_ANGLES_PER_EVENT))
    cases.append(("已用角度不重复给", "结果" not in [p["angle"] for p in plan]))
    cases.append(("角度计划带写作指令", all(p["angle_hint"] for p in plan)))
    cases.append(("无事实 → 不给角度（不写空话）",
                  plan_angles({"facts": []}) == []))

    # 去重
    d1 = distinct_enough(["让3追4！拜仁大逆转", "第94分钟丢球，阿森纳遭绝杀"])
    cases.append(("不同标题去重通过", d1["ok"] is True))
    d2 = distinct_enough(["没想到罗马主帅这场压力比尤文还大",
                          "没想到拉齐奥主帅这场压力比米兰还大"])
    cases.append(("同句式被判重复", d2["ok"] is False))
    d3 = distinct_enough(["金球奖揭晓凯恩夺魁73球", "金球奖揭晓凯恩夺魁73球"])
    cases.append(("同开头被判重复", d3["ok"] is False and bool(d3["heads"])))

    # event_key
    cases.append(("节点键带 node: 前缀", event_key_of({"node_id": "x1"}) == "node:x1"))
    cases.append(("无键返回 None（不臆测聚类）", event_key_of({"title": "某标题"}) is None))

    return cases


def main() -> int:
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
