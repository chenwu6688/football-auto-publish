#!/usr/bin/env python3
"""封面覆盖率验收（2026-11-01 体系体检）。

病根来自体检数据（``docs/audit/2026-11-01-体系体检与14天观察窗.md``）：

    组       篇数   聚合点击率   篇均阅读
    无封面    50      0.52%       26
    有封面    26      2.07%       77

10 月已发布 143 篇里 **53 篇（37%）是无封面发出的**——头条在正文 0 图时会被主动设成
「无封面」模式，而这条路径的点击率只有有封面的 1/4。

链路根因（三处断点）：
  ① `WIKI_PLAYERS`(18) / `WIKI_TEAMS`(15) 两张手工表 → 只有 33 个实体有精确页；
  ② footyrenders 返回 50×50 占位图，下载端 800×600 闸门必拒；
  ③ Unsplash 无 Key、DuckDuckGo 在 CI 出口 IP 常被挡。
→ 「找不到图 → 不配图 → 无封面发布」，且**这件事只 print 不落台账**，黑了很久。

本脚本把「封面配得上没」变成可验收的硬项：

  A. 无封面发布占比（硬项）—— 兜底生效日后近 14 天已发布篇里零配图 ≤5%；
     样本 <5 篇跳过（改造首日就判＝必然假警）
  B. 图源链路（硬项）—— search_images 已接入「实体词典 → 维基搜索」，
     且 footyrenders 已退出候选（它只会贡献 50×50）；用 AST 查真实调用，
     不文本匹配（docstring 里一句说明就会误报）
  C. 尺寸闸门（硬项）—— 保存侧的下载器仍要求 ≥800×600，否则「占位图当封面」会回来
  D. 负向验证（硬项）—— 把阈值人为放松到 100%，判定必须变成「通过」，
     说明这个指标真的在参与判定，而不是恒真
  E. 零配图兜底（硬项，2026-11-04 起）—— 图源全断时 `orchestrator.save_articles_local`
     必须在「0 图」分支生成数据卡，且文件名要落在 `publisher.extract_images` 的正则里。
     **只加图源不加兜底是治不好的**：维基本机与 CI 出口都可能超时，Unsplash 无 Key，
     DuckDuckGo 挡出口 IP，footyrenders 已停用——四条全断时实体识别 100% 正确也是 0 图。

生效日两段：
  - ``COVER_SINCE``    图源改造（实体词典 / 维基搜索）生效日；
  - ``FALLBACK_SINCE`` 数据卡兜底生效日——**A 项按这个判**。11-02~11-04 的零配图篇
    发生在兜底接入之前，属旧链路样本：报告里单列留证，但不计判据
    （不靠删样本让指标变绿，也不让历史样本把新链路永远判红）。

产出 ``data/audit/cover_coverage_<date>.md`` + ``cover_coverage_latest.json``；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_cover_coverage.py [--days 14] [--quiet]
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
DATA_COLLECTOR = PROJECT_ROOT / "data_collector.py"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 封面链路改造生效日（此前的篇是旧图源产物，不进判定）
COVER_SINCE = "2026-11-02"
# 数据卡兜底（P0-3）生效日：从这天起「零配图」才算违规。
# 11-02~11-04 的零配图篇发生在兜底接入之前，单列留证、不计判据。
FALLBACK_SINCE = "2026-11-05"

# 已发布篇里「零配图」的允许占比。留 5% 是给「实体全不在词典、维基也搜不到」的极端篇，
# 不是给偷懒用的——基线是 37%。
MAX_NO_COVER_PCT = 5.0
# 样本下限：少于这个数不下结论
MIN_SAMPLE = 5
# 下载端最小尺寸（低于它等于把占位图当封面）
MIN_WIDTH = 800
MIN_HEIGHT = 600


def _today():
    return datetime.now(CST).date()


def _window(days, since_str=None):
    """返回 (since, until)；生效日未到返回 None（避免倒置窗口空转）。"""
    today = _today()
    start = today - timedelta(days=days - 1)
    since = max(datetime.strptime(since_str or COVER_SINCE, "%Y-%m-%d").date(), start)
    if since > today:
        return None
    return since.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def _load_published(since, until):
    """收集窗口内「已发布」（有 platform_id）的成稿配图数。"""
    rows = []
    d0 = datetime.strptime(since, "%Y-%m-%d").date()
    d1 = datetime.strptime(until, "%Y-%m-%d").date()
    for day in sorted(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else []:
        if not day.is_dir() or not day.name.startswith("20"):
            continue
        try:
            cur = datetime.strptime(day.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if cur < d0 or cur > d1:
            continue
        meta = day / "metadata.json"
        if not meta.exists():
            continue
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        for art in payload.get("articles") or []:
            if not art.get("platform_id"):
                continue          # 没发出去的不算「无封面发布」
            rows.append({"date": day.name,
                         "title": art.get("title") or "",
                         "images": len(art.get("images") or [])})
    return rows


def _pre_fallback_rows():
    """兜底接入之前（COVER_SINCE ~ 兜底生效日前一天）的已发布样本。

    只作说明、不参与判定：既不能让历史样本把新链路永远判红，
    也不能假装这些零配图篇不存在（那就是靠删样本让指标变绿）。
    """
    start = datetime.strptime(COVER_SINCE, "%Y-%m-%d").date()
    end = min(_today(), datetime.strptime(FALLBACK_SINCE, "%Y-%m-%d").date() - timedelta(days=1))
    if end < start:
        return []
    return _load_published(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


def check_no_cover(days):
    """A. 兜底生效日后已发布篇的零配图占比。"""
    pre = _pre_fallback_rows()
    pre_zero = [r for r in pre if r["images"] == 0]
    pre_note = (f"留证：兜底接入前（{COVER_SINCE} 起）已发布 {len(pre)} 篇，零配图 {len(pre_zero)} 篇"
                f"——旧链路样本，不计入判据，待兜底生效后在窗口内重新观察"
                if pre else "")
    win = _window(days, FALLBACK_SINCE)
    if win is None:
        detail = [f"⏸ 兜底生效日 {FALLBACK_SINCE} 尚未到，暂无生效后样本；"
                  f"上限 {MAX_NO_COVER_PCT:.0f}%（未判定 ≠ 通过）"]
        if pre_note:
            detail.append(pre_note)
        return {"item": "无封面发布占比", "ok": None, "advisory": True, "detail": detail}
    since, until = win
    rows = _load_published(since, until)
    if len(rows) < MIN_SAMPLE:
        detail = [f"⏸ {since}~{until} 已发布样本仅 {len(rows)} 篇（<{MIN_SAMPLE}），"
                  f"不下结论；上限 {MAX_NO_COVER_PCT:.0f}%（未判定 ≠ 通过）"]
        if pre_note:
            detail.append(pre_note)
        return {"item": "无封面发布占比", "ok": None, "advisory": True, "detail": detail}
    zero = [r for r in rows if r["images"] == 0]
    pct = 100.0 * len(zero) / len(rows)
    ok = pct <= MAX_NO_COVER_PCT
    detail = [f"{'✅' if ok else '❌'} {since}~{until} 已发布 {len(rows)} 篇，"
              f"其中零配图 {len(zero)} 篇（{pct:.1f}%，上限 {MAX_NO_COVER_PCT:.0f}%）",
              "口径：零配图 ⇒ publisher 会把封面模式设成「无封面」，"
              "该路径聚合点击率 0.52% vs 有封面 2.07%（2026-11-01 体检）"]
    if not ok:
        detail.append("❌ 零配图篇举例：" + "；".join(r["title"][:24] for r in zero[:3]))
    if pre_note:
        detail.append(pre_note)
    return {"item": "无封面发布占比", "ok": ok, "detail": detail}


def _calls_in(path):
    """AST 收集某文件里出现的被调函数名（只认真实 Call 节点）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            names.add(fn.id)
        elif isinstance(fn, ast.Attribute):
            names.add(fn.attr)
    return names


def check_source_chain():
    """B. 图源链路：实体词典→维基搜索已接入；footyrenders 已退出候选。"""
    detail, ok = [], True
    calls = _calls_in(DATA_COLLECTOR)
    src = DATA_COLLECTOR.read_text(encoding="utf-8")

    if "search_wikipedia_entity" not in src:
        ok = False
        detail.append("❌ data_collector 里没有 search_wikipedia_entity（实体扩展层缺失）")
    elif "search_wikipedia_entity" not in calls:
        ok = False
        detail.append("❌ search_wikipedia_entity 只有定义、没有被调用（接了等于没接）")
    else:
        detail.append("✅ 实体词典 → 维基搜索已接入 search_images")

    # footyrenders 只返回 50×50 占位图，留在候选链里就是白跑一趟网络
    fn = None
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "search_images":
                fn = node
    except Exception:
        pass
    if fn is None:
        ok = False
        detail.append("❌ 找不到 search_images 函数")
    else:
        used = {n.func.id for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "search_footyrenders" in used:
            ok = False
            detail.append("❌ search_images 仍在调用 search_footyrenders"
                          "（该源只返回 50×50 占位图，必然被尺寸闸门拒掉）")
        else:
            detail.append("✅ footyrenders 已退出候选链")
        if "search_wikipedia" not in used and "search_wikipedia_entity" not in used:
            ok = False
            detail.append("❌ search_images 没有任何维基图源调用")
    return {"item": "图源链路", "ok": ok, "detail": detail}


def check_news_source():
    """B2. 新闻源正文图：零成本、精度最高的那一层，必须真的被消费。

    功能级验证而不是文本匹配：直接喂一张假图给 `search_images`，看它有没有被当成
    候选返回。2026-11-04 之前 `media_scraper` 已经把战报正文 `<img>` 抽成
    `source_images`，但**只有一条路径用过它**，抓了等于没抓；文本匹配查不出这种事。
    """
    detail = []
    try:
        import data_collector as dc
        got = dc.search_images(
            {"title": "覆盖验收", "source_images": [{"url": "https://example.invalid/a.jpg"}]},
            count=1)
    except Exception as e:
        return {"item": "新闻源正文图", "ok": False,
                "detail": [f"❌ 调用 search_images 失败：{type(e).__name__}: {e}"]}
    used = [g for g in got if isinstance(g, dict) and g.get("source") == "news_source"]
    ok = bool(used)
    detail.append(("✅ search_images 会消费 topic['source_images']（喂 1 张假图返回了 "
                   f"{len(used)} 张）") if ok else
                  "❌ search_images 没有消费 topic['source_images']（战报正文图抓了等于没抓）")
    # 图库层（Pixabay / Pexels）也要在候选链里，且失败不再静默
    src = DATA_COLLECTOR.read_text(encoding="utf-8")
    for name in ("_gallery_pixabay", "_gallery_pexels"):
        if name not in src:
            ok = False
            detail.append(f"❌ data_collector 缺 {name}（图库备选层缺失）")
        else:
            detail.append(f"✅ {name} 已定义")
    return {"item": "新闻源正文图", "ok": ok, "detail": detail}


def check_size_gate():
    """C. 保存侧下载器仍要求 ≥800×600，避免占位图混进来。"""
    detail, ok = [], True
    src = ORCHESTRATOR.read_text(encoding="utf-8")
    if "min_width" not in src or "min_height" not in src:
        ok = False
        detail.append("❌ orchestrator 里找不到 min_width/min_height 尺寸闸门配置")
    else:
        import re
        mw = re.search(r'"min_width":\s*(\d+)', src)
        mh = re.search(r'"min_height":\s*(\d+)', src)
        if not mw or not mh:
            ok = False
            detail.append("❌ 尺寸闸门参数解析失败（配置结构变了？）")
        else:
            w, h = int(mw.group(1)), int(mh.group(1))
            if w < MIN_WIDTH or h < MIN_HEIGHT:
                ok = False
                detail.append(f"❌ 尺寸闸门被放松到 {w}×{h}（应 ≥{MIN_WIDTH}×{MIN_HEIGHT}）"
                              "——50×50 占位图会重新变成封面")
            else:
                detail.append(f"✅ 下载尺寸闸门 {w}×{h}（≥{MIN_WIDTH}×{MIN_HEIGHT}）")
    return {"item": "尺寸闸门", "ok": ok, "detail": detail}


def check_negative(rows_pct=100.0):
    """D. 负向验证：把上限放到 100%，判定必须翻成「通过」。

    否则这个指标可能是恒真（比如分母永远为空），看着绿但什么都没拦。
    """
    detail, ok = [], True
    fake = [{"images": 0}] * 10
    pct = 100.0 * sum(1 for r in fake if r["images"] == 0) / len(fake)
    strict = pct <= MAX_NO_COVER_PCT
    loose = pct <= 100.0
    if strict:
        ok = False
        detail.append(f"❌ 负向验证失败：10 篇全零配图竟被判定通过（阈值 {MAX_NO_COVER_PCT}%）")
    if not loose:
        ok = False
        detail.append("❌ 负向验证失败：上限放松到 100% 仍不通过，说明判定不依赖阈值")
    if ok:
        detail.append(f"✅ 负向验证：10 篇全零配图 → 严格阈值({MAX_NO_COVER_PCT:.0f}%)判 FAIL、"
                      f"放松阈值(100%)判 PASS，指标确实在起作用")
    return {"item": "负向验证", "ok": ok, "detail": detail}


def check_fallback_wired():
    """E. 零配图兜底：0 图分支必须真的生成数据卡，且名字能被发布端抓到。"""
    detail, ok = [], True
    try:
        tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    except Exception as e:
        return {"item": "零配图兜底", "ok": False,
                "detail": [f"❌ orchestrator 解析失败：{e}"]}
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "save_articles_local"), None)
    if fn is None:
        ok = False
        detail.append("❌ 找不到 save_articles_local")
    else:
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "render"
                 and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == "cover_card"]
        if not calls:
            ok = False
            detail.append("❌ 零配图分支没接 cover_card.render —— "
                          "图源一断仍然走「无封面」（CTR 0.52%）")
        else:
            kws = {k.arg for k in calls[0].keywords}
            if "filename" not in kws:
                ok = False
                detail.append("❌ 调了 render 但没传 filename → 生成 card-*.jpg，"
                              "publisher.extract_images 抓不到，台账照样记 0 图")
            else:
                detail.append("✅ 零配图分支已生成数据卡，且用发布端可识别的文件名")
    try:
        import cover_card
        import degrade
    except Exception as e:
        return {"item": "零配图兜底", "ok": False,
                "detail": detail + [f"❌ 导入失败：{e}"]}
    if cover_card.W < MIN_WIDTH or cover_card.H < MIN_HEIGHT:
        ok = False
        detail.append(f"❌ 数据卡尺寸 {cover_card.W}×{cover_card.H} 不过闸门")
    else:
        detail.append(f"✅ 数据卡尺寸 {cover_card.W}×{cover_card.H}"
                      f"（≥{MIN_WIDTH}×{MIN_HEIGHT}）")
    if "封面兜底" not in degrade.DEGRADE_POLICY:
        ok = False
        detail.append("❌ degrade 策略表缺「封面兜底」—— 出问题时没有留痕口径")
    else:
        detail.append("✅ 降级策略含「封面兜底」（不阻塞发布，但必须留痕）")
    return {"item": "零配图兜底", "ok": ok, "detail": detail}


def check_img_topic_wiring():
    """B3. 六处生成路径必须**真能拿到**新闻源正文图（功能级 + 接线级双查）。

    2026-11-04 的教训分两层，缺一层都查不出来：

    ① **接线层**：`_img_topic` 存在且被 6 处调用点使用，但它是「哑透传」——
       找的是 `c["source_images"]`，而字段实际长在 `c["fixture"]["source_images"]`
       （`_find_source_article` 读的就是这一层）。函数在、调用在、测试也绿，
       结果一张图都没取到。所以这里**不数调用点**，而是直接喂一个嵌套结构看它出不出图。
    ② **数据层**：事件稿/赛程稿/预测稿手里只有选题，身上没有图。它们在结构上要
       按队名二元组去 `match_data` 里找那场比赛的战报图，且**两支队缺一不可**
       （只命中一支就会把同城对手的图配上来）。

    负向同样必查：没有图时必须返回空，而不是硬塞一张占位图。
    """
    detail, ok = [], True
    try:
        import orchestrator as o
    except Exception as e:
        return {"item": "六处路径真能取到正文图", "ok": False,
                "detail": [f"❌ 无法载入 orchestrator：{type(e).__name__}: {e}"]}

    # ① 嵌套字段
    nested = o._img_topic({"title": "验收"}, {"fixture": {
        "source_images": [{"url": "https://example.invalid/n.jpg"}]}})
    if nested.get("source_images"):
        detail.append("✅ _img_topic 能取到 fixture.source_images（此前漏的就是这一层）")
    else:
        ok = False
        detail.append("❌ _img_topic 取不到 fixture.source_images —— 又变回哑透传了")

    # ② 按队名二元组关联
    md = {"all_fixtures": [{"home_team": "皇家马德里", "away_team": "巴塞罗那",
                            "source_images": [{"url": "https://example.invalid/m.jpg"}]}]}
    hit = o._img_topic({"title": "皇马3-1巴萨，第70分钟换人后连丢2球",
                        "keywords_cn": ["皇马", "巴萨"]}, {}, match_data=md)
    if hit.get("source_images"):
        detail.append("✅ 只在选题里的稿件也能按队名二元组找回那场比赛的正文图")
    else:
        ok = False
        detail.append("❌ 事件稿/赛程稿拿不到本批已抓到的战报图（关联没接上）")

    # ③ 负向：只命中一支队 → 必须不配图（配错图是事实错误，无图有数据卡兜底）
    miss = o._img_topic({"title": "曼联又输了", "keywords_cn": ["曼联"]}, {},
                        match_data=md)
    if miss.get("source_images"):
        ok = False
        detail.append("❌ 只命中一支队就配了图 —— 会把同城/无关比赛的图配上来")
    else:
        detail.append("✅ 负向：只命中一支队时不配图")

    # ④ 负向：纯占位图/相对路径必须被丢（否则等于没抓）
    dirty = o._img_topic({"title": "验收"}, {
        "source_images": ["/img/a.jpg", "data:image/png;base64,xx",
                          "https://x.invalid/loading.gif"]})
    if dirty.get("source_images"):
        ok = False
        detail.append("❌ 相对路径/占位图被当成可用图源传下去了")
    else:
        detail.append("✅ 负向：相对路径 / data: / loading 占位图都被丢弃")

    # ⑤ 接线层：orchestrator 里所有 search_images 调用都必须吃 _img_topic 的产出，
    #    否则又会出现「这条路径自己拼 topic、拿不到 source_images」的老问题。
    #
    #    判据是**作用域内的数据流**，不是字符串匹配：先在同一个函数里找出
    #    `xxx = _img_topic(...)` 的变量名，再看 `search_images(xxx, ...)` 的实参是不是它。
    #    只按「实参是不是 Call」判会全判失败——真实写法是先把结果赋给 img_topic 再传，
    #    这一点是本次实现时踩到的（首版报 8/8 处绕过，实际全部合规）。
    try:
        tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    except Exception as e:      # pragma: no cover
        return {"item": "六处路径真能取到正文图", "ok": False,
                "detail": detail + [f"❌ orchestrator 解析失败：{e}"]}

    def _fed_names(scope) -> set:
        names = set()
        for n in ast.walk(scope):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                    and getattr(n.value.func, "id", None) == "_img_topic":
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
        return names

    global_fed = _fed_names(tree)
    total = wired = 0
    loose = []

    def _scan(node, fed: set):
        nonlocal total, wired
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.FunctionDef):
                _scan(ch, _fed_names(ch))
                continue
            if isinstance(ch, ast.Call) and getattr(ch.func, "id", None) == "search_images":
                total += 1
                args = list(ch.args)
                ok_arg = bool(args) and (
                    (isinstance(args[0], ast.Call)
                     and getattr(args[0].func, "id", None) == "_img_topic")
                    or (isinstance(args[0], ast.Name) and args[0].id in fed))
                # `a or search_images(b, ...)` 这种写法里 b 也在同一作用域，照常判
                if ok_arg:
                    wired += 1
                else:
                    loose.append(getattr(ch, "lineno", 0))
            _scan(ch, fed)

    _scan(tree, global_fed)
    if total and wired == total:
        detail.append(f"✅ {total}/{total} 处 search_images 都由 _img_topic 供料")
    else:
        ok = False
        detail.append(f"❌ {total - wired}/{total} 处 search_images 绕过 _img_topic"
                      f"（行号 {loose[:5]}）—— 这些路径拿不到 source_images")
    return {"item": "六处路径真能取到正文图", "ok": ok, "detail": detail}


def verify(days=14):
    checks = [check_no_cover(days), check_source_chain(), check_news_source(),
              check_img_topic_wiring(), check_size_gate(), check_negative(),
              check_fallback_wired()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "window_days": days, "since": COVER_SINCE, "fallback_since": FALLBACK_SINCE,
            # 三态：ok=None 表示「未判定」，既不算通过也不算失败（样本不足）
            "ok": all(c["ok"] is not False for c in hard),
            "checks": checks,
            "hard_failed": [c["item"] for c in hard if c["ok"] is False],
            "undetermined": [c["item"] for c in checks if c["ok"] is None]}


def render_md(rep):
    # 有未判定项时不能笼统写「通过」：样本不足既不是绿也不是红
    _verdict = "通过 ✅" if rep["ok"] else "未通过 ❌"
    if rep["ok"] and rep.get("undetermined"):
        _verdict = f"部分通过 ⏸（{len(rep['undetermined'])} 项样本不足未判定，不等于通过）"
    L = ["# 封面覆盖率验收（2026-11-01 体系体检）", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['window_days']} 天"
         f"｜图源改造生效 {rep['since']}｜兜底生效 {rep.get('fallback_since', '-')}",
         f"- 验收结论：**{_verdict}**",
         "- 口径出处：docs/audit/2026-11-01-体系体检与14天观察窗.md（无封面 0.52% vs 有封面 2.07%）",
         "- A 项按**兜底生效日**判：图源改造后仍零配图的篇（11-02~11-04）发生在兜底接入前，"
         "单列留证、不计判据",
         ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    if rep.get("undetermined"):
        L += [f"> 未判定（样本不足，不等于通过）：{'、'.join(rep['undetermined'])}", ""]
    for c in rep["checks"]:
        _mark = {True: "✅", False: "❌", None: "⏸"}[c["ok"]]
        L.append(f"## {c['item']}：{_mark}"
                 + ("（跳过/告警）" if c.get("advisory") else ""))
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="封面覆盖率验收")
    ap.add_argument("--days", type=int, default=14, help="统计窗口（默认 14）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"cover_coverage_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "cover_coverage_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    for item in rep.get("undetermined") or []:
        print(f"::warning::封面验收「{item}」样本不足，本次未判定（不等于通过）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
