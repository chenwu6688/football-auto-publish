#!/usr/bin/env python3
"""六个差异化方向（计划 13.5）。

计划 13.5 的六个方向：速度 / 地域深耕 / 怀旧与情感 / 数据再加工 / 评论区运营 / 观点增量。
本模块把 config/differentiation.yaml 读为**可校验配置**，并核对每个方向是否已有对应代码落地
（``files`` 中至少一处出现 ``marker``）。

职责边界：
  - ``directions()``  读取六方向（依据 + 落地要点）；
  - ``coverage()``    核对每方向的代码 hook 是否就绪（供验收脚本）；
  - ``selftest()``    校验配置结构（六方向齐备、字段完整）。

绝不越界：本模块只做「方向是否落地」的核对，不生成任何内容、不介入选题。

命令行：
  python3 differentiation.py --coverage
  python3 differentiation.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "differentiation.yaml"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

EXPECTED = ["速度", "地域深耕", "怀旧与情感", "数据再加工", "评论区运营", "观点增量"]

_cfg_cache: dict | None = None


def load_config(path: Path | str | None = None) -> dict:
    if yaml is None:
        return {"方向": []}
    p = Path(path) if path else CONFIG_PATH
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"方向": []}
    dirs = data.get("方向")
    return {"方向": dirs if isinstance(dirs, list) else []}


def config(path: Path | str | None = None) -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = load_config(path)
    return _cfg_cache


def reset_cache() -> None:
    global _cfg_cache
    _cfg_cache = None


def directions(path: Path | str | None = None) -> list:
    return list(config(path).get("方向") or [])


def _file_has(rel: str, marker: str) -> bool:
    try:
        return marker in (PROJECT_ROOT / rel).read_text(encoding="utf-8")
    except Exception:
        return False


def coverage(path: Path | str | None = None) -> list:
    """逐方向核对代码 hook：返回 [{key, ok, hit, marker, files}]。"""
    out = []
    for d in directions(path):
        files = d.get("files") or []
        marker = d.get("marker") or ""
        hit = [f for f in files if marker and _file_has(f, marker)]
        out.append({"key": d.get("key"), "ok": bool(hit), "hit": hit,
                    "marker": marker, "files": files})
    return out


def missing(path: Path | str | None = None) -> list:
    """未落地的方向名。"""
    return [c["key"] for c in coverage(path) if not c["ok"]]


def selftest(path: Path | str | None = None) -> list:
    """校验配置结构：六方向齐备、字段完整。返回不通过项（空=通过）。"""
    dirs = directions(path)
    bad = []
    keys = [d.get("key") for d in dirs]
    for want in EXPECTED:
        if want not in keys:
            bad.append({"type": "missing_direction", "key": want})
    for d in dirs:
        for field in ("key", "依据", "落地要点", "files", "marker"):
            if not d.get(field):
                bad.append({"type": "missing_field", "key": d.get("key"), "field": field})
    return bad


def render_selftest(bad=None) -> str:
    bad = selftest() if bad is None else bad
    cov = coverage()
    L = ["# 六个差异化方向（计划 13.5）", "",
         f"- 方向数：{len(directions())}　结构不通过：{len(bad)}",
         f"- 结论：**{'通过 ✅' if not bad else '未通过 ❌'}**", ""]
    for c in cov:
        L.append(f"- {'✅' if c['ok'] else '❌'} {c['key']}　hook=`{c['marker']}`　"
                 f"命中={c['hit'] or '无'}")
    if bad:
        L += ["", "结构问题："] + [f"- ❌ {b}" for b in bad]
    return "\n".join(L)


def _cli() -> int:
    ap = argparse.ArgumentParser(description="六个差异化方向（计划 13.5）")
    ap.add_argument("--coverage", action="store_true", help="核对代码 hook 落地")
    ap.add_argument("--selftest", action="store_true", help="校验配置结构")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        bad = selftest()
        print(render_selftest(bad))
        return 0 if not bad else 1
    if args.coverage:
        cov = coverage()
        if args.json:
            print(json.dumps(cov, ensure_ascii=False, indent=2))
        else:
            for c in cov:
                print(f"{'✅' if c['ok'] else '❌'} {c['key']}  hook={c['marker']}  hit={c['hit']}")
        return 0 if all(c["ok"] for c in cov) else 1
    print(render_selftest())
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
