#!/usr/bin/env python3
"""热点选题通道 + 赛程去重的验收（2026-11-06 新增）。

## 补的缺口

11-06 用户两条反馈指向同一个洞：**选题供给与"今天大家在谈什么"完全脱钩**。

* 「昨晚 C 罗国家队退役，我文章里没有任何体现」——微博/百度榜上
  「C罗退出国家队」就在前列，但选题素材只有赛程池 + 15 条直播吧新闻，
  热搜从来没进过生产链路（`signal_sources` 的输出只喂 ranker 做排序，
  **产生不了新候选**；而且 `batch.yml` 从未运行过 `refresh_signals.py`）。
* 「补发的好像也是过期的内容」——同一场 Lazio vs Napoli 在晨读与午间
  各写了一篇：已发侧 keywords 是英文（lazio/napoli），候选侧是中文
  （拉齐奥/那不勒斯），`"napoli" in "那不勒斯"` 恒为 False。

## 判据

  A. 热点通道在位（硬项）——`hot_topics` 模块具备「榜单→足球切片→候选」
     三段，且 orchestrator 真的调了它（`_hot_topic_cards` 出现在 main 流程里）。
     少了这段，「紧跟热点」就只是句口号。
  B. 热点事件键落盘（硬项）——`get_cross_batch_covered` 收集 `hot_event_keys`、
     保存侧写 `hot_event_key`。少任一处，同一件热搜会每批重写一篇。
  C. 赛程去重按对阵（硬项）——`collect_fixture_topics` 必须用 `fixture_pairs`
     且必须经 `team_names.canonical_team` 归一；不得退回到
     「`kw in home or home in kw`」那种跨语言裸比对（那正是本次事故的写法）。
  D. 事件键跨措辞一致（硬项）——`event_key("C罗退役")` 必须等于
     `event_key("C罗退出国家队")`。负向自检：改回「按命中词集合拼接」必红。
  E. 热点稿覆盖（告警项）——近 7 天 metadata 里带 `hot_event_key` 的篇数。
     0 篇不是失败（可能确实没热点、或榜单不可用），但要看得见：
     连续多天 0 篇说明通道又断线了。

用法：
  python3 scripts/verify_hot_freshness.py
  python3 scripts/verify_hot_freshness.py --quiet
"""

import argparse
import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))


def _ok(item, ok, detail=None, advisory=False):
    return {"item": item, "ok": ok, "advisory": advisory, "detail": detail or []}


def _func_src(tree, path: Path, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            try:
                return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
            except Exception:
                return ""
    return ""


def _check_module() -> dict:
    """A. 热点通道在位。"""
    p = PROJECT_ROOT / "hot_topics.py"
    if not p.exists():
        return _ok("A 热点通道在位", False, ["hot_topics.py 不存在"])
    src = p.read_text(encoding="utf-8")
    need = {"fetch_board": "抓榜", "football_hits": "足球切片",
            "build_candidates": "榜→候选", "build_from_news": "同题聚合降级",
            "event_key": "事件键"}
    missing = [f"{k}（{v}）" for k, v in need.items() if f"def {k}(" not in src]
    if missing:
        return _ok("A 热点通道在位", False, [f"缺函数：{'、'.join(missing)}"])

    orch = (PROJECT_ROOT / "orchestrator.py").read_text(encoding="utf-8")
    detail = [f"hot_topics 五段齐备（{len(need)} 个入口）"]
    ok = True
    if "_hot_topic_cards(" not in orch:
        ok = False
        detail.append("orchestrator 未调用 _hot_topic_cards —— 热点仍在链路外")
    else:
        detail.append("orchestrator 已调用 _hot_topic_cards")
    if "HOT_TOPIC_PER_BATCH" not in orch:
        ok = False
        detail.append("缺 HOT_TOPIC_PER_BATCH：热点可无限占批，会变成热榜搬运")
    else:
        detail.append("热点限流常量在位（每批上限）")
    # 位置判据：热点必须在 ranker 之后插入。放在之前会被重排回队尾。
    # 必须找**调用点**，不能找定义点：`def _hot_topic_cards(` 在文件里排在
    # `_rank_topics` 定义之前，按定义位置判先后会永远误报「插在 ranker 之前」。
    i_rank = orch.find("_rank_res = _rank_topics(")
    i_hot = orch.find("_hot_cards = _hot_topic_cards(")
    if i_rank == -1 or i_hot == -1:
        ok = False
        detail.append("找不到 ranker 或热点插入点，无法判定先后")
    elif i_hot < i_rank:
        ok = False
        detail.append("热点插在 ranker 之前 —— 会被按「信号强度」重排回队尾，等于没接")
    else:
        detail.append("插入点在 ranker 之后（不会被重排掉）")
    return _ok("A 热点通道在位", ok, detail)


def _check_ledger() -> dict:
    """B. 热点事件键落盘与回读。"""
    orch_path = PROJECT_ROOT / "orchestrator.py"
    src = orch_path.read_text(encoding="utf-8")
    detail = []
    ok = True

    if 'covered["hot_event_keys"].add' not in src:
        ok = False
        detail.append("get_cross_batch_covered 未收集 hot_event_keys —— 同一热搜每批都会重写")
    else:
        detail.append("已发侧收集 hot_event_keys")
    if '_rec["hot_event_key"]' not in src:
        ok = False
        detail.append("保存侧未写 hot_event_key —— 只留在文章对象上，随保存层挑字段丢失")
    else:
        detail.append("保存侧写 hot_event_key")

    # 台账：热点榜切片必须留档，否则「C罗为什么没发」只能靠猜
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        import hot_topics
        has_writer = hasattr(hot_topics, "write_ledger")
    except Exception as exc:
        has_writer = False
        detail.append(f"hot_topics 导入失败：{exc}")
    if not has_writer:
        ok = False
        detail.append("缺 write_ledger：榜单切片与落选原因无法留证")
    else:
        detail.append("榜单切片台账在位（data/audit/hot_board_*.json）")
    return _ok("B 事件键落盘与回读", ok, detail)


def _check_fixture_dedup() -> dict:
    """C. 赛程去重按对阵（跨语言）。"""
    p = PROJECT_ROOT / "data_collector.py"
    tree = ast.parse(p.read_text(encoding="utf-8"))
    fn = _func_src(tree, p, "collect_fixture_topics")
    if not fn:
        return _ok("C 赛程去重按对阵", False, ["找不到 collect_fixture_topics"])

    detail = []
    ok = True
    if "fixture_pairs" not in fn:
        ok = False
        detail.append("未按 fixture_pairs（对阵）去重")
    else:
        detail.append("按对阵去重（与语言/措辞无关）")
    if "canonical_team" not in fn:
        ok = False
        detail.append("未经 team_names.canonical_team 归一 —— 中文队名比英文关键词必不相等")
    else:
        detail.append("已用 canonical_team 跨语言归一")
    # 负向：退回裸字符串比对就报红
    if "kw in home or home in kw" in fn:
        ok = False
        detail.append("检测到裸字符串比对（kw in home …）—— 这正是 11-06 重复发同一场的写法")
    return _ok("C 赛程去重按对阵", ok, detail)


def _check_event_key() -> dict:
    """D. 事件键跨措辞一致（真跑一遍）。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        import hot_topics as h
    except Exception as exc:
        return _ok("D 事件键跨措辞一致", False, [f"导入失败：{exc}"])
    variants = ["C罗退役", "C罗宣布退出国家队", "C罗告别国家队"]
    keys = {h.event_key(w) for w in variants}
    detail = [f"{w} → {h.event_key(w)}" for w in variants]
    if len(keys) != 1:
        return _ok("D 事件键跨措辞一致", False,
                   detail + [f"同一事件得到 {len(keys)} 个键 —— 会被写成 {len(keys)} 篇"])
    if not list(keys)[0].endswith("#retire"):
        return _ok("D 事件键跨措辞一致", False, detail + ["未落到 retire 事件族"])
    # 反向：不同事件不能混成一个键
    if h.event_key("C罗转会皇马") == list(keys)[0]:
        return _ok("D 事件键跨措辞一致", False, detail + ["退役与转会撞成同一个键"])
    return _ok("D 事件键跨措辞一致", True, detail + ["退役/转会分属两族（不误合并）"])


def _check_coverage(days: int = 7) -> dict:
    """E. 近 N 天热点稿覆盖（告警项）。"""
    now = datetime.now(CST)
    total = hot = 0
    detail = []
    for i in range(days):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        mp = OUTPUT_DIR / d / "metadata.json"
        if not mp.exists():
            continue
        try:
            arts = json.loads(mp.read_text(encoding="utf-8")).get("articles") or []
        except Exception:
            continue
        n_hot = sum(1 for a in arts if isinstance(a, dict) and a.get("hot_event_key"))
        total += len(arts)
        hot += n_hot
        detail.append(f"{d}：{len(arts)} 篇，其中热点稿 {n_hot} 篇")
    if total == 0:
        return _ok(f"E 近 {days} 天热点覆盖", True, ["窗口内无产物，未判定"], advisory=True)
    detail.append(f"合计 {hot}/{total} 篇来自热点通道")
    if hot == 0:
        detail.append("⚠️ 连续多天 0 篇：通道可能又断线（榜单境外不可达 / 无同题报道）")
    return _ok(f"E 近 {days} 天热点覆盖", True, detail, advisory=True)


def verify() -> dict:
    checks = [_check_module(), _check_ledger(), _check_fixture_dedup(),
              _check_event_key(), _check_coverage()]
    hard_failed = [c["item"] for c in checks if c["ok"] is False and not c.get("advisory")]
    return {
        "version": 1,
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "ok": not hard_failed,
        "hard_failed": hard_failed,
        "checks": checks,
    }


def render_md(rep: dict) -> str:
    L = ["# 热点选题通道验收（2026-11-06 新增）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         "- 补的缺口：选题供给与「今天大家在谈什么」脱钩 —— 热搜进不了候选，"
         "同一场比赛换个措辞就重复发。", ""]
    if rep["hard_failed"]:
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        mark = "✅" if c["ok"] else "❌"
        if c.get("advisory"):
            mark = "ℹ️"
        L.append(f"## {c['item']}：{mark}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def selftest() -> int:
    """负向自检：把事件键退回「按命中词集合拼接」，D 项必须报红。

    没有这一步，D 项就是「永远绿」的摆设 —— 2026-11-05 的教训是
    「写了阈值 ≠ 达成阈值」，验收器同理：没验过会红的验收器不算验收。
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    import hot_topics as h

    orig = h.event_key

    def _old_event_key(text, ents=None):
        ents = ents if ents is not None else h.entities_in(text)
        if not ents:
            return ""
        return "|".join(sorted(ents)) + "#" + ",".join(h._event_terms(text))

    h.event_key = _old_event_key
    try:
        rep = _check_event_key()
    finally:
        h.event_key = orig

    if rep["ok"]:
        print("❌ 负向自检失败：退回旧事件键后 D 项仍然绿 —— 这个判据抓不到回归")
        return 1
    print("✅ 负向自检通过：退回旧事件键后 D 项按预期报红")
    print(f"   报红原因：{rep['detail'][-1]}")

    # 正向复核：恢复后必须重新变绿，否则说明状态没复位
    rep2 = _check_event_key()
    if not rep2["ok"]:
        print("❌ 复位失败：恢复 event_key 后 D 项仍红")
        return 1
    print("✅ 复位正常：恢复后 D 项重新通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="热点选题通道验收")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="负向自检（必须报红）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"hot_freshness_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "hot_freshness_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
