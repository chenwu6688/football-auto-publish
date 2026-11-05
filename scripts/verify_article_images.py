#!/usr/bin/env python3
"""「每篇至少 3 张相关配图」验收（2026-11-05 用户指令）。

背景：用户看到的实际效果是「光有文字，没有配图」。查下来的链条是——

    图库层（Pexels/Pixabay/Unsplash）在足球题材上系统性失效（球员肖像权不在图库手里）
    → Commons 本机不通、CI 也只有 1~2 张
    → 唯一能稳定给到 3 张的存量是「新闻站的报道正文图」，但项目从来没爬过
    → 结果每篇 0~1 张，靠数据卡兜底，观感就是「一篇长文配一张标题卡」

本脚本盯住四条，任何一条断了，3 张力就退回 0~1 张，而且**不会有人知道**：

  硬项 A. 爬取层接线——`search_images` 真的调用 `news_images.fetch`（只 import 不算）
  硬项 B. 补齐接线——`save_articles_local` 真的调用 `cover_card.fill`，且传 need/prefix
  硬项 C. 注入不丢图——3 张图 + 2 节正文时，3 张都必须进正文（旧实现静默丢第 3 张）
  硬项 D. 断网可兜底——把爬取层打到断网，仍能补出 3 张**不同版式**的信息图
  硬项 E. 尺寸分档——封面 800×600、正文 600×400；混用会把 720×480 的新闻图整批拒掉
  硬项 F. 真实产物——生效日（metadata.image_floor.since）之后的批次，每篇 images ≥ 3

  告警 G. 降级台账——`配图不足` 出现说明连信息图都补不上（无字体/无数字），要看原因

三态：没有对齐生效日的批次产物 → 「未判定」，退出码 2（不是通过）。

用法：python3 scripts/verify_article_images.py [--quiet] [--days 7]
产出：data/audit/article_images_<date>.md + data/audit/article_images_latest.json
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import image_ledger  # noqa: E402  配图台账判据（与 CI 合并脚本共用一份）

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
ORCH = PROJECT_ROOT / "orchestrator.py"
DC = PROJECT_ROOT / "data_collector.py"
CST = timezone(timedelta(hours=8))

SAMPLE_TITLE = "瓦伦西亚主帅先动还是姆巴佩先冷，皇马27分这趟客场不好踢"
SAMPLE_CONTENT = ("导语。\n\n## 一、27分不是稳\n领先只有两分。\n\n"
                  "## 二、球荒被放大\n四轮只进一个。\n\n## 三、换帅考题\n三场拿四分。")


# ——————————————————————————————————————————————
# 静态检查
# ——————————————————————————————————————————————

def _fn_node(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


def _imports(node) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def check_news_layer_wired() -> dict:
    fn = _fn_node(DC, "search_images")
    if fn is None:
        return {"item": "爬取层接线（search_images → news_images.fetch）",
                "ok": False, "detail": ["data_collector.search_images 不见了"]}
    imported = "news_images" in _imports(fn)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "fetch"]
    detail = [f"import：{'有' if imported else '无'}", f"fetch 调用：{len(calls)} 处"]
    return {"item": "爬取层接线（search_images → news_images.fetch）",
            "ok": bool(imported and calls), "detail": detail}


def check_fill_wired() -> dict:
    fn = _fn_node(ORCH, "save_articles_local")
    if fn is None:
        return {"item": "补齐接线（save_articles_local → cover_card.fill）",
                "ok": False, "detail": ["save_articles_local 不见了"]}
    fills = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "fill"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "cover_card"]
    detail, ok = [], bool(fills)
    if fills:
        kws = {k.arg for k in fills[0].keywords}
        miss = {"need", "prefix", "start_index"} - kws
        detail.append(f"参数：{sorted(kws)}")
        if miss:
            ok = False
            detail.append(f"缺参数：{sorted(miss)}")
    else:
        detail.append("没有调用 cover_card.fill → 断网时会退回 0~1 张")
    # 下限必须是常量，不能散落魔法数
    import orchestrator as _o
    floor = getattr(_o, "MIN_ARTICLE_IMAGES", None)
    detail.append(f"下限常量 MIN_ARTICLE_IMAGES={floor}")
    if floor != 3:
        ok = False
        detail.append("下限不是 3（改口径要同步本脚本与 metadata.image_floor）")
    return {"item": "补齐接线（save_articles_local → cover_card.fill）",
            "ok": ok, "detail": detail}


def check_download_gate_tier() -> dict:
    """封面与正文的尺寸闸门必须分档。"""
    fn = _fn_node(ORCH, "save_articles_local")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "download_image"] if fn else []
    detail, ok = [], bool(calls)
    if calls:
        kws = {k.arg for k in calls[0].keywords}
        detail.append(f"download_image 参数：{sorted(kws)}")
        if not {"min_width", "min_height"} <= kws:
            ok = False
            detail.append("没传分档闸门 → 720×480 的新闻图会被 800×600 整批拒掉")
        detail.append("封面 800×600 / 正文 600×400"
                      if {"min_width", "min_height"} <= kws else "闸门未分档")
    return {"item": "尺寸闸门分档（封面 800×600 / 正文 600×400）",
            "ok": ok, "detail": detail}


# ——————————————————————————————————————————————
# 行为检查
# ——————————————————————————————————————————————

def check_inject_keeps_all() -> dict:
    import orchestrator as o
    imgs = [{"filename": f"article-1-img-{i:03d}.jpg", "description": f"配图{i}"} for i in (1, 2, 3)]
    body_2sec = "开头\n\n## 一、第一节\n\n内容\n\n## 二、第二节\n\n内容"
    out = o._inject_images(body_2sec, imgs)
    missing = [i["filename"] for i in imgs if i["filename"] not in out]
    return {"item": "注入不丢图（2 节 + 3 张 → 3 张都在正文）",
            "ok": not missing,
            "detail": [f"缺失：{missing}" if missing else "3 张全部落到正文"]}


def check_offline_floor() -> dict:
    """断网（爬取层返回 None）时仍能补出 3 张不同版式。"""
    import cover_card
    import news_images
    with mock.patch.object(news_images, "_get", lambda *a, **k: None):
        with tempfile.TemporaryDirectory() as d:
            cards = cover_card.fill(
                {"title": SAMPLE_TITLE, "content": SAMPLE_CONTENT},
                out_dir=d, prefix="article-1-img", start_index=1, need=3)
    ok = len(cards) == 3 and len({c["md5"] for c in cards}) == 3
    detail = [f"补出 {len(cards)} 张，去重后 {len({c['md5'] for c in cards})} 张"]
    if not ok:
        detail.append(f"原因：{cover_card.LAST_ERROR or '信息图生成失败（字体/PIL？）'}")
    return {"item": "断网可兜底（爬取层失败仍补到 3 张）", "ok": ok, "detail": detail}


def check_gate_behaviour() -> dict:
    """真跑一次下载：720×480 必须被 800×600 拒、被 600×400 放行。"""
    import os
    try:
        from PIL import Image
    except Exception:
        return {"item": "闸门行为（720×480 拒于封面档、放于正文档）",
                "ok": None, "detail": ["PIL 不可用，未判定"]}
    img = Image.frombytes("RGB", (720, 480), os.urandom(720 * 480 * 3))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    data = buf.getvalue()

    class _R:
        headers = {"content-length": str(len(data))}
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=8192):
            yield data

    from image_service import ImageService
    svc = ImageService({"images": {"min_width": 800, "min_height": 600}})
    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(svc.session, "get", return_value=_R()):
        cover = svc.download_image("https://x/a.jpg", Path(d), "p", 1, set())
        body = svc.download_image("https://x/a.jpg", Path(d), "p", 2, set(),
                                  min_width=600, min_height=400)
    ok = cover is None and body is not None
    return {"item": "闸门行为（720×480 拒于封面档、放于正文档）",
            "ok": ok,
            "detail": [f"封面档：{'拒绝 ✅' if cover is None else '放行 ❌'}",
                       f"正文档：{'放行 ✅' if body else '拒绝 ❌'}"]}


# ——————————————————————————————————————————————
# 产物检查
# ——————————————————————————————————————————————

def _batches(days: int) -> list[tuple[str, dict]]:
    out = []
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    for p in sorted(OUTPUT_DIR.glob("2026-*/metadata.json")):
        day = p.parent.name
        if day < cutoff:
            continue
        try:
            out.append((day, json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def check_products(days: int) -> dict:
    batches = _batches(days)
    item = "真实产物（生效日后每篇 ≥ 3 张配图）"
    judged, bad, detail, legacy = [], [], [], []
    for day, meta in batches:
        floor = meta.get("image_floor") or {}
        since = str(floor.get("since") or "")
        if not since or day < since:
            detail.append(f"{day}：早于生效日 {since or '（未标注）'} → 未判定")
            continue
        want = int(floor.get("min_per_article") or 3)
        for a in meta.get("articles") or []:
            if not isinstance(a, dict):
                continue
            n = len(a.get("images") or a.get("downloaded_images") or [])
            if not image_ledger.has_image_ledger(a):
                # 无配图台账 = 改造前生成的老产物，不参与判定（未判定 ≠ 通过）
                legacy.append(f"{day}《{a.get('title', '')[:22]}》{n} 张")
                continue
            judged.append((day, a.get("title", ""), n))
            if n < want:
                bad.append(f"{day}《{a.get('title', '')[:22]}》{n} 张")
    if legacy:
        detail.append(f"另有 {len(legacy)} 篇为改造前产物（无配图台账），不计入判定："
                      f"{'、'.join(legacy[:3])}")
    if not judged:
        return {"item": item, "ok": None,
                "detail": detail + ["窗口内没有对齐生效日的批次产物 → 未判定"]}
    detail.append(f"判定 {len(judged)} 篇，达标 {len(judged) - len(bad)} 篇")
    detail.extend(bad[:10])
    return {"item": item, "ok": not bad, "detail": detail}


def check_variant_variety() -> dict:
    """极端稿也必须能补到 3 张 —— 直接跑版式库，不看产物。

    为什么这项是硬项：`MIN_ARTICLE_IMAGES` 写成 3 很容易，但**能不能真的出 3 张**
    取决于正文长什么样。实测存在「正文只有一句话、标题没有数字」的稿子，老版三
    版式（要点/数字/标题）在它身上只能出 1 张：要点卡要 `## ` 分节、数字卡要数字。
    这项拿四类最难的稿子跑一遍 `cover_card.fill(need=3)`，出不满 3 张就报红。
    """
    import cover_card
    cases = [
        ("正文仅一句", {"title": "这支球队的更衣室到底出了什么问题", "column": "中超",
                        "content": "其实问题不在球场上，而在训练场之外的那些事。"}),
        ("正文为空", {"title": "当年那个追风少年如今发福谢顶，你的青春还能找回吗？",
                      "column": "人物", "content": ""}),
        ("多段无小标题", {"title": "曼城客场碰维拉，切尔西热刺谁先掉链子", "column": "英超",
                          "content": "曼城这轮要去维拉公园。\n\n维拉主场七轮不败。\n\n热刺那场更像互相拆台。"}),
        # 11-05 补：正文正好两句、标题没有数字、没有小标题。这是金句卡
        # `nth=1` 必然拿到空串的那一类稿子（非导语句只有一句，第一张就用光了）。
        # 之前三类极端稿里没有这一类，所以「金句卡第二张永不出图」这个缺陷
        # 只被单测抓住、没被这项验收抓住——验收的样本集本身就是漏网的原因。
        ("正文仅两句", {"title": "换人顺序才是这支球队的真正问题", "column": "中超",
                        "content": "真正的问题不在体能，而在换人顺序。\n"
                                   "但是中场的第一道屏障在下半场就没了。"}),
    ]
    ok, detail = True, []
    with tempfile.TemporaryDirectory() as d:
        for label, art in cases:
            got = cover_card.fill(art, out_dir=d, prefix=f"v-{abs(hash(label)) % 9999}",
                                  start_index=1, need=3)
            # 视觉唯一性看 (版式, 配色)：极端稿允许两张标题卡换配色，但不允许一模一样
            uniq = len({(c["source"], c.get("theme")) for c in got})
            if len(got) < 3 or uniq < 3:
                ok = False
                detail.append(f"❌ {label}：{len(got)} 张 / {uniq} 种视觉"
                              f"（{cover_card.LAST_ERROR or '—'}）")
            else:
                detail.append(f"✅ {label}：3 张 / {uniq} 种视觉")
    return {"item": "极端稿保底（正文只有一句 / 为空 / 仅两句 / 多段无小标题，都必须补到 3 张）",
            "ok": ok, "detail": detail}


def check_variant_diversity(days: int) -> dict:
    """产物里的版式多样性：一篇的 3 张不能是同一个版式换配色。

    `check_variant_variety` 保证「能出 3 张」，这一项保证「出得像 3 张不同的图」。
    判定口径：一篇里信息图 ≥2 张时，版式种类必须 ≥2。极端稿（正文为空）允许
    全部退化为标题卡，但那种稿子会在 `below` 之外单独计入 detail，不会静默放过。
    """
    item = "版式多样性（同一篇的 3 张不能只是换了个配色）"
    judged, bad, detail = 0, [], []
    for day, meta in _batches(days):
        floor = meta.get("image_floor") or {}
        since = str(floor.get("since") or "")
        if not since or day < since:
            continue
        for a in meta.get("articles") or []:
            if not isinstance(a, dict):
                continue
            cards = a.get("image_cards") or []
            if len(cards) < 2:
                continue
            variants = sorted({c.get("variant") for c in cards if c.get("variant")})
            judged += 1
            if len(variants) < 2:
                bad.append(f"{day}《{a.get('title', '')[:20]}》"
                           f"{len(cards)} 张信息图全是「{'/'.join(variants)}」")
    if not judged:
        return {"item": item, "ok": None,
                "detail": ["窗口内没有含 ≥2 张信息图的批次产物 → 未判定"]}
    detail.append(f"判定 {judged} 篇，版式单调 {len(bad)} 篇")
    detail.extend(bad[:8])
    return {"item": item, "ok": not bad, "detail": detail}


def check_negative() -> dict:
    """负向自检：把「生效日后 + 只有 1 张图」的批次喂进去，产物判定**必须**报红。

    没有这一步，`check_products` 就有「恒返回 True」的风险（阈值写死、日期比较写反、
    glob 路径不对都能让它永远绿灯），而它是本脚本唯一看真实产物的项——
    它一旦恒真，整套验收就只剩「代码接上了」这半句。
    """
    day = datetime.now(CST).strftime("%Y-%m-%d")
    root = Path(tempfile.mkdtemp(prefix="imgfloor-"))
    try:
        for art_images, _label in ((["a.jpg"], "不足"), (["a.jpg", "b.jpg", "c.jpg"], "达标")):
            d = root / day
            d.mkdir(parents=True, exist_ok=True)
            (d / "metadata.json").write_text(json.dumps(
                {"image_floor": {"since": day, "min_per_article": 3},
                 "articles": [{"title": f"{_label}样本", "images": art_images}]},
                ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(sys.modules[__name__], "OUTPUT_DIR", root):
                rep = check_products(7)
            if _label == "不足":
                bad_ok = rep["ok"] is False
            else:
                good_ok = rep["ok"] is True
            (d / "metadata.json").unlink()
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    return {"item": "负向自检（1 张的批次必须报红、3 张的必须达标）",
            "ok": bool(bad_ok and good_ok),
            "detail": [f"1 张批次判红：{'✅' if bad_ok else '❌ 恒真风险'}",
                       f"3 张批次判绿：{'✅' if good_ok else '❌ 误杀'}"]}


def check_degrade_ledger(days: int) -> dict:
    hits, total = [], 0
    for day, meta in _batches(days):
        for ev in meta.get("degrade_events") or []:
            if not isinstance(ev, dict):
                continue
            if ev.get("环节") == "配图不足":
                total += int(ev.get("数量") or 1)
                hits.append(f"{day}：{ev.get('原因', '')}")
    return {"item": "降级台账（配图不足告警）", "advisory": True,
            "ok": total == 0,
            "detail": hits[:8] or ["窗口内没有「配图不足」事件"]}


# ——————————————————————————————————————————————

def verify(days: int = 7) -> dict:
    products = check_products(days)
    diversity = check_variant_diversity(days)
    hard = [check_news_layer_wired(), check_fill_wired(), check_download_gate_tier(),
            check_inject_keeps_all(), check_offline_floor(), check_gate_behaviour(),
            check_variant_variety(), products, diversity]
    soft = [check_degrade_ledger(days)]
    bad = [c["item"] for c in hard if c["ok"] is False]
    # 硬项：False 即未通过。静态/自检项全绿但**产物项未判定**时，结论仍是「未判定」——
    # 前面几项只能证明「代码接上了」，只有产物能证明「读者真的看到 3 张图」。
    if bad:
        ok = False
    elif products["ok"] is None:
        ok = None
    else:
        ok = True                      # 告警项不阻断（项目契约：告警只提示）
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "days": days,
            "ok": ok,
            "undetermined": [c["item"] for c in hard + soft if c["ok"] is None],
            "hard_failed": bad,
            "advisory_failed": [c["item"] for c in soft if c["ok"] is False],
            "checks": hard + soft}


def render_md(rep: dict) -> str:
    verdict = {True: "通过 ✅", False: "未通过 ❌", None: "未判定 ⚠️"}[rep["ok"]]
    L = ["# 配图下限验收（每篇 ≥ 3 张相关配图）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 统计窗口：近 {rep['days']} 天",
         f"- 验收结论：**{verdict}**", ""]
    if rep["hard_failed"]:
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    if rep.get("undetermined"):
        L += [f"> 未判定项：{'、'.join(rep['undetermined'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else "硬项"
        mark = {True: "✅", False: "❌", None: "⚠️"}[c["ok"]]
        L.append(f"## {c['item']}（{tag}）：{mark}")
        L.extend(f"- {d}" for d in c.get("detail", []))
        L.append("")
    if rep["advisory_failed"]:
        L += [f"> 告警项待关注：{'、'.join(rep['advisory_failed'])}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="配图下限验收（每篇 ≥ 3 张）")
    ap.add_argument("--days", type=int, default=7, help="产物统计窗口天数（默认 7）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="只跑负向自检")
    args = ap.parse_args()

    if args.selftest:
        rep = check_negative()
        print(("✅ " if rep["ok"] else "❌ ") + rep["item"])
        for d in rep["detail"]:
            print("   " + d)
        return 0 if rep["ok"] else 1

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"article_images_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "article_images_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    # 三态：硬项都没判定 = 无产物对齐生效日 → 2（未判定 ≠ 通过）
    if rep["ok"] is None:
        return 2
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
