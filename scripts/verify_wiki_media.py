#!/usr/bin/env python3
"""维基共享图源验收（2026-11-04）。

背景：足球领域**没有**「免费 + 有球员真人照 + 可商用 + 有 API」的通用图库——
球员肖像权在俱乐部/经纪/联赛手里，新闻图由 Getty/Reuters 垄断。
Pexels/Pixabay 实测：中文 query 返回酱油饺子、球员名返回奥地利村庄、
16 支球队只有 10 支有主场照且错法很像真的（国米→诺坎普）。
所以图源换成 Wikimedia Commons（社区上传、逐张 CC/PD、1.5 亿文件、无 Key）。

这条链路的验收判据**不是配图率**，而是合规与误配：
  配图率涨了而配错图/违规，比没有图更糟（无图有数据卡兜底，错图是事实错误）。

  A. 模块自检（硬）      —— 许可白名单 / 署名 fail-closed / 消歧 / 两种 formatversion
  B. 许可 fail-closed（硬）—— 不认识的许可、无作者的图，必须一张都进不来
  C. 接线（硬）          —— search_images 真的调 Commons；Pixabay/DDG 两条死链路已摘
  D. 署名落正文（硬）    —— attribution 必须能进正文（CC BY 要求署名，只在 metadata 记=没署名）
  E. 熔断语义（硬）      —— 「连不上」要熔断、「真没图」不能熔断
  F. 联网探活（软）      —— 本机 *.wikimedia.org 全超时，不通判「未验证」，
                            脚本返回 2，**不是通过**（在 CI 上才有意义）

退出码：硬项 FAIL → 1｜全通过 → 0｜联网项无法判定 → 2
产物：``data/audit/wiki_media_latest.json``
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wiki_media as wm                                    # noqa: E402
import data_collector as dc                                 # noqa: E402

AUDIT_DIR = ROOT / "data" / "audit"
JOURNAL = ROOT / "orchestrator.py"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def check_selftest() -> dict:
    cases = wm._selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "模块自检（许可/署名/消歧/解析）", "ok": not bad,
            "detail": [f"{len(cases) - len(bad)}/{len(cases)} 项通过"]
                      + [f"❌ {b}" for b in bad]}


def check_license_fail_closed() -> dict:
    """负向：不认识/不合规的许可必须进不来。"""
    detail, ok = [], True
    must_reject = ["Fair use", "Non-free", "GFDL", "All rights reserved", "",
                   "CC BY-NC 4.0", "Some custom permission"]
    for lic in must_reject:
        if wm.license_ok(lic):
            ok = False
            detail.append(f"❌ {lic!r} 被放行（应拒绝）")
    if not any(wm.license_ok(x) for x in ("CC0", "Public domain", "CC BY-SA 4.0")):
        ok = False
        detail.append("❌ 连 CC0/PD/CC BY-SA 都判成不可用（尺子做反了）")
    # 无作者必须拒：CC BY 要求署名，署不了名就不能用
    if wm.attribution_of({"LicenseShortName": {"value": "CC BY-SA 4.0"}}) is not None:
        ok = False
        detail.append("❌ 无作者信息的图被放行（无法署名＝违规）")
    detail.append(f"负向用例 {len(must_reject) + 1} 条（含无作者）")
    detail.append("许可白名单：CC0 / Public domain / CC BY / CC BY-SA")
    return {"item": "许可与署名 fail-closed", "ok": ok, "detail": detail}


def check_wiring() -> dict:
    """接线：Commons 真被调用；两条死链路真的摘了。"""
    import inspect
    src = inspect.getsource(dc.search_images)
    detail, ok = [], True
    if "_wm.search_for_entity" not in src:
        ok = False
        detail.append("❌ search_images 没有调用 Commons 层（模块在、没接线）")
    else:
        detail.append("✅ search_images 已接 Commons 层")
    if "_wiki_available()" not in src:
        ok = False
        detail.append("❌ Commons 层没有走 _wiki_available() 熔断（本机不通时会白等）")
    else:
        detail.append("✅ 走 _wiki_available() 熔断")
    for dead, why in (("_gallery_pixabay(gal", "Pixabay（元数据只有 tags，accept 校验必挂）"),
                      ("duckduckgo.com/i.js", "DuckDuckGo（代理挡 + 零许可校验）")):
        if dead in src:
            ok = False
            detail.append(f"❌ 已停用的图源仍在调用：{why}")
        else:
            detail.append(f"✅ 已摘除：{why}")
    # cover_binding 必须认这个源，否则取到的图会被「未绑定」二次拦掉
    import cover_binding as cb
    if cb.image_is_bound({"source": "wikimedia", "query": "", "alt": "x"}, []):
        detail.append("✅ cover_binding 白名单已含 wikimedia")
    else:
        ok = False
        detail.append("❌ cover_binding 不认 wikimedia → 图会被二次拦掉")
    return {"item": "接线（调用 + 熔断 + 死链路摘除）", "ok": ok, "detail": detail}


def check_credit_in_content() -> dict:
    """署名必须能进正文：这是 CC BY / BY-SA 的硬要求。"""
    detail, ok = [], True
    line = wm.credit_line({"artist": "Jane Doe", "license": "CC BY-SA 4.0"})
    body = wm.append_credit("正文内容", [line])
    if "Jane Doe" in body and "CC BY-SA 4.0" in body:
        detail.append("✅ append_credit 能把作者与许可写进正文")
    else:
        ok = False
        detail.append("❌ append_credit 没把署名写进正文")
    if wm.append_credit(body, [line]) != body:
        ok = False
        detail.append("❌ 重复追加（同一篇会出现两遍署名）")
    else:
        detail.append("✅ 幂等：重复调用不会叠加")
    # 生成侧是否真的把 attribution 透传到 downloaded 并落到正文
    try:
        tree = ast.parse(JOURNAL.read_text(encoding="utf-8"))
    except Exception as e:
        return {"item": "署名落正文（CC BY 硬要求）", "ok": False,
                "detail": [f"❌ orchestrator 解析失败：{e}"]}
    has_attr = has_credit = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and node.slice.value == "attribution":
            has_attr = True
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "append_credit":
            has_credit = True
    if has_attr and has_credit:
        detail.append("✅ orchestrator 把 attribution 透传到下载结果并写正文")
    else:
        ok = False
        detail.append(f"❌ orchestrator 缺署名链路（attribution={has_attr}, "
                      f"append_credit={has_credit}）")
    return {"item": "署名落正文（CC BY 硬要求）", "ok": ok, "detail": detail}


def check_breaker_semantics() -> dict:
    """「连不上」与「真没图」必须分开，否则一个冷门实体就把整个图源关掉。"""
    class _Boom:
        def get(self, *a, **k):
            raise TimeoutError("timed out")

    class _Empty:
        status_code = 200
        content = b"{}"

        def get(self, *a, **k):
            return self

        def json(self):
            return {"query": {"pages": []}}

    detail, ok = [], True
    wm.search("Test Entity", 1, session=_Boom())
    if not wm.LAST["network"]:
        ok = False
        detail.append("❌ 超时没有标记 network（永远不熔断 → 每篇白等）")
    else:
        detail.append("✅ 超时 → network=True（可熔断）")
    wm.search("Test Entity", 1, session=_Empty())
    if wm.LAST["network"]:
        ok = False
        detail.append("❌ 取空被当成网络故障（会把图源误关）")
    else:
        detail.append("✅ 取空 → network=False（不熔断）")
    return {"item": "熔断语义（连不上 vs 真没图）", "ok": ok, "detail": detail}


def check_live() -> dict:
    """联网探活。本机不通属预期，判「未验证」而不是通过。"""
    import requests
    try:
        r = requests.get(wm.COMMONS_API,
                         params={"action": "query", "format": "json", "meta": "siteinfo"},
                         headers={"User-Agent": wm.UA}, timeout=8)
    except Exception as e:
        return {"item": "联网实测（Commons API）", "ok": None, "skipped": True,
                "detail": [f"⏸ 连不上 commons.wikimedia.org（{type(e).__name__}）",
                           "本机/国内网络访问 *.wikimedia.org 全超时，属已知现象；",
                           "在 GitHub Actions 上才可判定。**本项不是通过。**"]}
    if r.status_code != 200:
        return {"item": "联网实测（Commons API）", "ok": None, "skipped": True,
                "detail": [f"⏸ HTTP {r.status_code}", "**本项不是通过。**"]}
    got = wm.search("Kylian Mbappe", 2)
    ok = len(got) >= 1 and all(g.get("attribution") for g in got)
    return {"item": "联网实测（Commons API）", "ok": ok, "skipped": False,
            "detail": [f"取回 {len(got)} 张，全部带 attribution：{ok}"]
                      + [f"· {g['alt'][:40]} / {g['attribution']['license']}" for g in got[:2]]}


def verify() -> dict:
    checks = [check_selftest(), check_license_fail_closed(), check_wiring(),
              check_credit_in_content(), check_breaker_semantics(), check_live()]
    hard_failed = [c["item"] for c in checks if c.get("ok") is False]
    pending = [c["item"] for c in checks if c.get("ok") is None or c.get("skipped")]
    return {"generated_at": _now(), "ok": not hard_failed, "checks": checks,
            "hard_failed": hard_failed, "pending": pending}


def render_md(rep: dict) -> str:
    if not rep["ok"]:
        verdict = "未通过 ❌"
    elif rep["pending"]:
        # 硬项绿但联网项没判定：这不是「通过」，别让读者误以为取图被验证过
        verdict = f"硬项通过，但 {len(rep['pending'])} 项**未判定**（不算通过）⏸"
    else:
        verdict = "通过 ✅"
    L = ["# 维基共享图源验收", "", f"- 生成时间：{rep['generated_at']}",
         f"- 结论：**{verdict}**"
         + (f"（未判定：{'、'.join(rep['pending'])}）" if rep["pending"] else ""), ""]
    for c in rep["checks"]:
        status = "⏸" if c.get("ok") is None else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}：{status}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="维基共享图源验收")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / "wiki_media_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.quiet:
        print(f"wiki_media: {'ok' if rep['ok'] else 'FAIL ' + '、'.join(rep['hard_failed'])}"
              + (f" pending={rep['pending']}" if rep["pending"] else ""))
    else:
        print(render_md(rep))
    if rep["pending"]:
        print(f"::warning::wiki_media 有 {len(rep['pending'])} 项未判定，不是通过")
    if not rep["ok"]:
        return 1
    return 2 if rep["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
