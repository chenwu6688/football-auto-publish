#!/usr/bin/env python3
"""图库实体白名单验收（2026-11-04）。

背景：Pexels Key 到手后第一次真跑，暴露两件事——
  ① `search_images` 把 `build_bound_query()` 的**中文实体**直接丢给英文索引图库，
     query="皇家马德里" 返回酱油饺子、query="欧冠" 返回欧元纸币；
  ② 图库对球队的召回只有一半能对上，且错法很"像真的"（查国米返回诺坎普、
     查马竞返回阿根廷的球场）。

所以图库这条链路不能按"提高覆盖率"来验收，只能按**误配率**来验收：

  A. 白名单配置自检（硬）—— query 全英文非空、accept 非空、声明的 verified ≥2、
     每个 key 都是 entity_map 里真实存在的 club（写错名字 = 静默失效）
  B. 运行时解析（硬）—— 球员不查、未登记球队不查、别名能解析到标准名、
     白名单每一项都能 resolve 出自己
  C. 接线（硬）—— `data_collector` 里所有 `_gallery_*` 调用**必须带 `accept=`**，
     且首参不得是 `build_bound_query` 的中文结果（这正是本次修的 bug，回归用例）
  D. 重音折叠（硬）—— `Bernabéu` 必须能被 `bernabeu` 命中（否则皇马会被判 0 张）
  E. 联网实测（软）—— 有 `PEXELS_KEY` 时每项重跑一次 per_page=30，
     与配置里声明的 verified 比对，偏差过大只告警（图库结果本身会漂）
     无 Key → 本项 skip，脚本整体返回 2（=「没做判定」，不等于通过）

退出码：硬项 FAIL → 1｜全部通过 → 0｜无 Key 无法实测 → 2
产物：``data/audit/gallery_entities_latest.json``

用法：
  python3 scripts/verify_gallery_entities.py [--quiet]
  python3 scripts/verify_gallery_entities.py --measure 皇马 巴萨   # 只测这几支队，给配置填数
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import gallery_query as gq  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
DATA_COLLECTOR = PROJECT_ROOT / "data_collector.py"
CST = timezone(timedelta(hours=8))

MIN_VERIFIED = 2          # 实测至少 2 张才允许登记（1 张等于没有冗余）
DRIFT_TOLERANCE = 0.6     # 联网实测与声明值偏差超过 60% 只告警


def _now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def check_config() -> dict:
    """A. 白名单配置自检（硬）。"""
    bad = gq.invalid()
    if bad:
        return {"item": "白名单配置自检", "ok": False,
                "detail": [f"❌ 有 {len(bad)} 条被丢弃的非法条目（写错必须有人知道，"
                           "不能静默失效）："]
                          + [f"  · {b['name']}：{b['reason']}" for b in bad]}

    wl = gq.whitelist()
    detail, ok = [], True
    if not wl:
        return {"item": "白名单配置自检", "ok": False,
                "detail": ["❌ 白名单为空或无法解析（config/gallery_entities.yaml）"]}
    detail.append(f"登记 {len(wl)} 个实体：{'、'.join(wl)}")

    alias = gq._alias_map()
    bad_name, bad_q, bad_acc = [], [], []
    for name, item in wl.items():
        if alias.get(name) != name and alias.get(name.lower()) != name:
            bad_name.append(name)          # 不是 entity_map 里的 club → 永远解析不到
        if not item["query"] or gq._has_cjk(item["query"]):
            bad_q.append(f"{name}:{item['query']}")
        if not item["accept"]:
            bad_acc.append(name)
    if bad_name:
        ok = False
        detail.append(f"❌ 不在 entity_map 的 club 名单里（写错名=静默失效）：{'、'.join(bad_name)}")
    else:
        detail.append("✅ 全部能对上 entity_map 的 club")
    if bad_q:
        ok = False
        detail.append(f"❌ query 为空或含中文（英文索引图库收到中文必配错图）：{'、'.join(bad_q)}")
    else:
        detail.append("✅ query 全英文")
    if bad_acc:
        ok = False
        detail.append(f"❌ accept 为空（无法校验＝不可信）：{'、'.join(bad_acc)}")
    else:
        detail.append("✅ accept 全非空")
    return {"item": "白名单配置自检", "ok": ok, "detail": detail}


def check_declared_yield() -> dict:
    """A2. 配置里声明的 verified ≥ MIN_VERIFIED（硬）。"""
    try:
        import yaml
        raw = yaml.safe_load((PROJECT_ROOT / "config" / "gallery_entities.yaml")
                             .read_text(encoding="utf-8")) or {}
        clubs = raw.get("clubs") or {}
    except Exception as e:
        return {"item": "声明实测张数 ≥2", "ok": False, "detail": [f"❌ 读配置失败：{e}"]}
    low = [f"{n}:{c.get('verified')}" for n, c in clubs.items()
           if isinstance(c, dict) and int(c.get("verified") or 0) < MIN_VERIFIED]
    missing = [n for n, c in clubs.items() if isinstance(c, dict) and "verified" not in c]
    ok = not low and not missing
    detail = [("✅ " if ok else "❌ ") + f"{len(clubs)} 项都声明了实测张数且 ≥{MIN_VERIFIED}"]
    if low:
        detail.append(f"❌ 张数不足仍登记（会让「无图」变「错图」）：{'、'.join(low)}")
    if missing:
        detail.append(f"❌ 未写 verified 字段（无可复核依据）：{'、'.join(missing)}")
    return {"item": "声明实测张数 ≥2", "ok": ok, "detail": detail}


def check_resolve() -> dict:
    """B. 运行时解析（硬）。"""
    detail, ok = [], True
    cases = [
        ("球员不查图库", gq.resolve(["姆巴佩"]) is None),
        ("未登记球队不查图库", gq.resolve(["尤文"]) is None),
        ("无实体不查图库", gq.resolve([]) is None and gq.resolve(None) is None),
        ("别名 皇家马德里→皇马", (gq.resolve(["皇家马德里"]) or {}).get("entity") == "皇马"),
        ("混合实体取球队", (gq.resolve(["姆巴佩", "皇马"]) or {}).get("entity") == "皇马"),
        ("球员优先于球队时不误判", gq.resolve(["贝林厄姆"]) is None),
    ]
    for name, passed in cases:
        detail.append(("✅ " if passed else "❌ ") + name)
        ok = ok and passed
    # 白名单每一项都要能 resolve 出自己（否则登记了也永远命中不到）
    miss = []
    for name in gq.whitelist():
        got = gq.resolve([name])
        if not got or got["entity"] != name:
            miss.append(name)
    if miss:
        ok = False
        detail.append(f"❌ 登记但 resolve 不到自己：{'、'.join(miss)}")
    else:
        detail.append(f"✅ {len(gq.whitelist())} 项都能 resolve 出自己")
    # 解析出的 query 绝不能是中文
    cjk = [n for n in gq.whitelist() if gq._has_cjk((gq.resolve([n]) or {}).get("query", ""))]
    if cjk:
        ok = False
        detail.append(f"❌ 解析出中文 query：{'、'.join(cjk)}")
    return {"item": "运行时解析", "ok": ok, "detail": detail}


def check_wiring() -> dict:
    """C. 接线（硬）：所有 _gallery_* 调用都带 accept，且首参不是 build_bound_query。"""
    try:
        tree = ast.parse(DATA_COLLECTOR.read_text(encoding="utf-8"))
    except Exception as e:                       # pragma: no cover
        return {"item": "接线：图库调用必须带 accept", "ok": False,
                "detail": [f"❌ 解析 data_collector 失败：{e}"]}
    total = with_accept = 0
    no_accept, chinese_q = [], []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") in ("_gallery_pexels", "_gallery_pixabay")):
            continue
        total += 1
        if any(k.arg == "accept" for k in node.keywords):
            with_accept += 1
        else:
            no_accept.append(node.lineno)
        for a in node.args[:1]:
            if isinstance(a, ast.Call) and getattr(a.func, "id", None) == "build_bound_query":
                chinese_q.append(node.lineno)
    detail = [f"{total} 处 _gallery_* 调用"]
    ok = True
    if total and with_accept == total:
        detail.append(f"✅ {with_accept}/{total} 都带 accept 校验")
    else:
        ok = False
        detail.append(f"❌ {total - with_accept}/{total} 处未带 accept（行号 {no_accept[:5]}）"
                      "—— 等于放行「看起来合理但错」的图")
    if chinese_q:
        ok = False
        detail.append(f"❌ {len(chinese_q)} 处首参是 build_bound_query（中文 query，"
                      f"行号 {chinese_q[:5]}）—— 这正是 11-04 查出的 bug")
    else:
        detail.append("✅ 没有把 build_bound_query 的中文结果喂给英文图库")
    return {"item": "接线：图库调用必须带 accept", "ok": ok, "detail": detail}


def check_fold() -> dict:
    """D. 重音折叠回归（硬）。"""
    cases = [
        ("Bernabéu → bernabeu", gq.fold("Santiago Bernabéu") == "santiago bernabeu"),
        ("命中带重音的 alt", gq.is_accepted("Discover the iconic Santiago Bernabéu Stadium",
                                            ("bernabeu",))),
        ("负向：别家球场不通过", not gq.is_accepted("Wide shot of Camp Nou", ("bernabeu",))),
        ("负向：空 alt 不通过", not gq.is_accepted("", ("bernabeu",))),
        ("负向：空 accept 不通过", not gq.is_accepted("Bernabeu stadium", ())),
    ]
    ok = all(c[1] for c in cases)
    return {"item": "alt 重音折叠与负向校验", "ok": ok,
            "detail": [("✅ " if c[1] else "❌ ") + c[0] for c in cases]}


def measure(entities=None, *, timeout: int = 25) -> dict:
    """联网实测：每项按配置的 query 检索，统计 alt 命中主场名的张数。"""
    key = os.environ.get("PEXELS_KEY", "")
    if not key:
        return {"skipped": True, "reason": "未配置 PEXELS_KEY"}
    import requests
    wl = gq.whitelist()
    names = [e for e in (entities or list(wl)) if e in wl]
    out = {}
    for name in names:
        item = wl[name]
        try:
            r = requests.get("https://api.pexels.com/v1/search",
                             params={"query": item["query"], "per_page": 30,
                                     "orientation": "landscape"},
                             headers={"Authorization": key}, timeout=timeout)
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
            continue
        if r.status_code != 200:
            out[name] = {"error": f"HTTP {r.status_code}: {r.text[:80]}"}
            continue
        photos = (r.json() or {}).get("photos") or []
        hit = [p for p in photos if gq.is_accepted(p.get("alt"), item["accept"])]
        out[name] = {"query": item["query"], "got": len(photos), "verified": len(hit),
                     "sample": (hit[0].get("alt") or "")[:60] if hit else ""}
    return {"skipped": False, "measured": out}


def check_measure() -> dict:
    """E. 联网实测（软，三态）。"""
    rep = measure()
    if rep.get("skipped"):
        return {"item": "联网实测白名单命中", "ok": None, "skipped": True,
                "detail": [f"⏸ {rep['reason']}——本项未判定（不等于通过）"]}
    detail, drift = [], []
    detail.append(f"Pexels 实测（per_page=30，只算 alt 命中主场名的）：")
    for name, m in rep["measured"].items():
        if m.get("error"):
            detail.append(f"  · {name}: 取数失败 {m['error']}")
            continue
        detail.append(f"  · {name}: {m['verified']}/{m['got']} 张命中"
                      + (f"「{m['sample']}」" if m["sample"] else "（本次一张都没命中）"))
        try:
            import yaml
            declared = ((yaml.safe_load((PROJECT_ROOT / "config" / "gallery_entities.yaml")
                                        .read_text(encoding="utf-8")) or {})
                        .get("clubs", {}).get(name, {}).get("verified"))
        except Exception:
            declared = None
        if declared and abs(m["verified"] - int(declared)) > max(2, int(declared) * DRIFT_TOLERANCE):
            drift.append(f"{name}: 声明 {declared} → 实测 {m['verified']}")
    ok = not drift
    if drift:
        detail.append(f"⚠️ 命中数与登记值偏差过大（图库结果会漂，建议复测后更新 verified）："
                      f"{'；'.join(drift)}")
    else:
        detail.append("✅ 与登记值一致（偏差在容差内）")
    return {"item": "联网实测白名单命中", "ok": ok, "advisory": True, "detail": detail}


def verify() -> dict:
    checks = [check_config(), check_declared_yield(), check_resolve(),
              check_wiring(), check_fold(), check_measure()]
    hard = [c for c in checks if not c.get("advisory") and not c.get("skipped")]
    failed = [c["item"] for c in hard if c["ok"] is False]
    skipped = [c["item"] for c in checks if c.get("skipped") or c.get("ok") is None]
    return {"generated_at": _now(), "ok": not failed, "checks": checks,
            "hard_failed": failed, "pending": skipped}


def render_md(rep: dict) -> str:
    L = ["# 图库实体白名单验收", "", f"- 生成时间：{rep['generated_at']}",
         f"- 结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**"
         + (f"（未判定：{'、'.join(rep['pending'])}）" if rep["pending"] else ""), ""]
    for c in rep["checks"]:
        status = "⏸" if (c.get("ok") is None or c.get("skipped")) else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}：{status}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="图库实体白名单验收")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--measure", nargs="*", default=None,
                    help="只实测这些球队（给配置填 verified 用）")
    args = ap.parse_args(argv)

    if args.measure is not None:
        print(json.dumps(measure(args.measure), ensure_ascii=False, indent=1))
        return 0

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / "gallery_entities_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.quiet:
        print(f"gallery_entities: {'ok' if rep['ok'] else 'FAIL ' + '、'.join(rep['hard_failed'])}"
              + (f" pending={rep['pending']}" if rep["pending"] else ""))
    else:
        print(render_md(rep))
    if rep["pending"]:
        print(f"::warning::gallery_entities 有 {len(rep['pending'])} 项未判定，不是通过")
    if not rep["ok"]:
        return 1
    return 2 if rep["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
