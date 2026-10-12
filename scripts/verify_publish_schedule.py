#!/usr/bin/env python3
"""发布时段纪律验收（完善计划 3.3 / 第十章）。

由来：2026-10-12 用头条后台 869 条真实图文回测发布时段，发现两个可量化的亏损：
  1) 晚间 17 点档点击率仅 0.92%（n=106），而 18 点档 2.27%（n=30）为全时段峰值；
  2) 凌晨 00:00-05:59 篇均阅读仅 60.5（全时段最差），9-10 月有 41 条凌晨误发，
     全部源于 GitHub cron 延迟（主调度 Cloudflare 准点，兜底 cron 会漂移）。

本脚本把「发布时刻」四处口径钉死，防止任一处单独漂移：
  constants.BATCH_CONFIG[].time  ←→  config/ratio_config.yaml「时段窗口」
                                 ←→  external-trigger/wrangler.toml cron（UTC）
                                 ←→  external-trigger/worker.js cron→批次映射
                                 ←→  .github/workflows/batch.yml 兜底 cron 与死区护栏

硬项（不达标退出码 1）
  A. 配置结构——ratio_config「时段窗口」三批目标齐备且含死区
  B. 目标时刻一致——constants 各批 time == 窗口目标
  C. 主调度 cron——wrangler.toml 的 UTC cron 与目标时刻（CST-8h）一致
  D. 主调度映射——worker.js 的 cron→批次映射与 wrangler crons 一致
  E. 兜底 cron——batch.yml 每批 2 个兜底 cron = 目标 +30/+50 分钟
  F. 死区护栏——batch.yml 存在死区跳过逻辑，且阈值与配置死区一致
  G. 死区不冲突——死区与任何批次的目标/容忍窗口不重叠

告警项（只提示）
  H. 近 14 天成稿发布时刻落点（有 publish_time 时统计，否则跳过）

用法：python3 scripts/verify_publish_schedule.py [--quiet]
产出：data/audit/publish_schedule_<date>.md + publish_schedule_latest.json
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from constants import BATCH_CONFIG  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))

RATIO_PATH = PROJECT_ROOT / "config" / "ratio_config.yaml"
WRANGLER_PATH = PROJECT_ROOT / "external-trigger" / "wrangler.toml"
WORKER_PATH = PROJECT_ROOT / "external-trigger" / "worker.js"
BATCH_YML_PATH = PROJECT_ROOT / ".github" / "workflows" / "batch.yml"

BATCH_CN = {"morning": "晨读", "noon": "午间", "evening": "晚间"}


def _hm(s: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(s))
    if not m:
        raise ValueError(f"非法时刻：{s}")
    return int(m.group(1)), int(m.group(2))


def _to_utc_hm(h: int, mi: int) -> tuple[int, int]:
    return (h - 8) % 24, mi


def windows() -> dict | None:
    try:
        import yaml
        d = yaml.safe_load(RATIO_PATH.read_text(encoding="utf-8"))
        w = d.get("时段窗口")
        return w if isinstance(w, dict) else None
    except Exception:
        return None


def check_config() -> dict:
    w = windows()
    if not w:
        return {"item": "时段窗口配置（ratio_config）", "ok": False, "advisory": False,
                "detail": ["❌ 缺 config/ratio_config.yaml 的「时段窗口」块"]}
    problems = []
    for cn in BATCH_CN.values():
        node = w.get(cn)
        if not isinstance(node, dict) or not node.get("目标"):
            problems.append(f"{cn} 缺目标时刻")
            continue
        try:
            _hm(node["目标"])
        except Exception as e:
            problems.append(f"{cn} 目标非法：{e}")
        rng = node.get("容忍")
        if not (isinstance(rng, list) and len(rng) == 2 and all(re.match(r"^\d{1,2}:\d{2}$", str(x)) for x in rng)):
            problems.append(f"{cn} 容忍窗口格式应为 [起,止]")
    dz = w.get("死区")
    if not (isinstance(dz, list) and len(dz) == 2):
        problems.append("死区应为 [起,止]")
    return {"item": "时段窗口配置（ratio_config）", "ok": not problems, "advisory": False,
            "windows": {k: (v.get("目标") if isinstance(v, dict) else v) for k, v in w.items()},
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"三批目标 {[w[c]['目标'] for c in BATCH_CN.values()]}；死区 {dz} ✅"])}


def check_targets_match_constants() -> dict:
    w = windows() or {}
    bad = []
    for en, cn in BATCH_CN.items():
        node = w.get(cn) or {}
        want = node.get("目标")
        have = (BATCH_CONFIG.get(en) or {}).get("time")
        if want and have and str(want) != str(have):
            bad.append(f"{cn}: constants={have} ≠ 窗口目标={want}")
        if not have:
            bad.append(f"{cn}: constants.BATCH_CONFIG 缺 time")
    return {"item": "目标时刻一致（constants ↔ 时段窗口）", "ok": not bad, "advisory": False,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad
                       else [f"constants 三班时刻与窗口目标一致："
                             f"{ {en: (BATCH_CONFIG.get(en) or {}).get('time') for en in BATCH_CN} } ✅"])}


def _wrangler_crons() -> list[tuple[int, int]]:
    try:
        txt = WRANGLER_PATH.read_text(encoding="utf-8")
    except Exception:
        return []
    block = re.search(r"crons\s*=\s*\[(.*?)\]", txt, re.S)
    if not block:
        return []
    out = []
    for m in re.finditer(r'"(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*"', block.group(1)):
        out.append((int(m.group(2)), int(m.group(1))))  # (UTC hour, UTC minute)
    return out


def check_wrangler() -> dict:
    crons = _wrangler_crons()
    w = windows() or {}
    want = []
    for cn in BATCH_CN.values():
        node = w.get(cn) or {}
        if node.get("目标"):
            h, mi = _hm(node["目标"])
            want.append(_to_utc_hm(h, mi))
    if not crons:
        return {"item": "主调度 cron（wrangler.toml）", "ok": False, "advisory": False,
                "detail": ["❌ 未能解析 wrangler.toml 的 crons"]}
    missing = [f"UTC {h:02d}:{mi:02d}" for h, mi in want if (h, mi) not in crons]
    extra = [f"UTC {h:02d}:{mi:02d}" for h, mi in crons if (h, mi) not in want]
    problems = []
    if missing:
        problems.append(f"缺主调度 cron：{missing}")
    if extra:
        problems.append(f"多余 cron（与目标不符）：{extra}")
    return {"item": "主调度 cron（wrangler.toml）", "ok": not problems, "advisory": False,
            "crons_utc": [f"{h:02d}:{mi:02d}" for h, mi in crons],
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"crons（UTC）{[f'{h:02d}:{mi:02d}' for h, mi in crons]} "
                             f"= 目标（CST）{[w[c]['目标'] for c in BATCH_CN.values()]} - 8h ✅"])}


def _worker_map() -> dict[int, str]:
    try:
        txt = WORKER_PATH.read_text(encoding="utf-8")
    except Exception:
        return {}
    m = re.search(r"function batchFromCron\(.*?\n\}", txt, re.S)
    body = m.group(0) if m else txt
    out = {}
    for mm in re.finditer(r"hour\s*===\s*(\d+)\)\s*return\s*'(\w+)'", body):
        out[int(mm.group(1))] = mm.group(2)
    return out


def check_worker() -> dict:
    wmap = _worker_map()
    crons = _wrangler_crons()
    w = windows() or {}
    problems = []
    if not wmap:
        problems.append("未能解析 worker.js 的 cron→批次映射")
    for h, mi in crons:
        want_batch = None
        for en, cn in BATCH_CN.items():
            node = w.get(cn) or {}
            if node.get("目标"):
                th, tm = _to_utc_hm(*_hm(node["目标"]))
                if (th, tm) == (h, mi):
                    want_batch = en
        if want_batch and wmap.get(h) != want_batch:
            problems.append(f"UTC {h:02d}:{mi:02d} 应映射 {want_batch}，worker.js 实为 {wmap.get(h)}")
    return {"item": "主调度映射（worker.js）", "ok": not problems, "advisory": False,
            "map": {f"UTC {k:02d}": v for k, v in sorted(wmap.items())},
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"worker.js 映射 {wmap} 与 wrangler crons 一致 ✅"])}


def _batch_yml_crons() -> list[tuple[int, int]]:
    try:
        txt = BATCH_YML_PATH.read_text(encoding="utf-8")
    except Exception:
        return []
    out = []
    for m in re.finditer(r"-\s*cron:\s*'(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*'", txt):
        out.append((int(m.group(2)), int(m.group(1))))  # (UTC hour, minute)
    return out


def check_backstop() -> dict:
    crons = _batch_yml_crons()
    w = windows() or {}
    want = []
    for cn in BATCH_CN.values():
        node = w.get(cn) or {}
        if not node.get("目标"):
            continue
        h, mi = _hm(node["目标"])
        for plus in (30, 50):
            tot = h * 60 + mi + plus
            ch, cm = (tot // 60) % 24, tot % 60
            want.append(_to_utc_hm(ch, cm))
    missing = [f"UTC {h:02d}:{m:02d}" for h, m in want if (h, m) not in crons]
    problems = []
    if missing:
        problems.append(f"缺兜底 cron（目标+30/+50）：{missing}")
    crons_str = ", ".join(f"{h:02d}:{m:02d}" for h, m in crons)
    return {"item": "兜底 cron（batch.yml）", "ok": not problems, "advisory": False,
            "crons_utc": [f"{h:02d}:{m:02d}" for h, m in crons],
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"兜底 crons（UTC）[{crons_str}] = 各批目标 +30/+50 分钟 ✅"])}


def check_deadzone_guard() -> dict:
    try:
        txt = BATCH_YML_PATH.read_text(encoding="utf-8")
    except Exception as e:
        return {"item": "死区护栏（batch.yml）", "ok": False, "advisory": False,
                "detail": [f"❌ 无法读取 batch.yml：{e}"]}
    w = windows() or {}
    dz = w.get("死区") or ["00:00", "06:00"]
    dz_h = _hm(dz[0])[0]
    has_guard = ("死区" in txt) and re.search(r"-lt\s+6\b", txt) is not None
    problems = []
    if not has_guard:
        problems.append("未找到死区跳过逻辑（应含 `-lt 6` 与「死区」字样）")
    if not re.search(r"dead_zone=true", txt):
        problems.append("未设置 dead_zone 输出标记")
    if dz_h != 0:
        problems.append(f"配置死区起点 {dz[0]} 非 00:00，护栏阈值需同步调整")
    return {"item": "死区护栏（batch.yml）", "ok": not problems, "advisory": False,
            "deadzone": dz,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"死区 {dz} 护栏就绪（CST < {dz_h + 6} 时跳过）✅"])}


def check_no_overlap() -> dict:
    w = windows() or {}
    dz = w.get("死区")
    if not dz:
        return {"item": "死区与时段不重叠", "ok": False, "advisory": False,
                "detail": ["❌ 缺死区定义"]}
    d0 = _hm(dz[0])[0] * 60 + _hm(dz[0])[1]
    d1 = _hm(dz[1])[0] * 60 + _hm(dz[1])[1]
    problems = []
    for cn in BATCH_CN.values():
        node = w.get(cn) or {}
        if node.get("目标"):
            t = _hm(node["目标"])
            tt = t[0] * 60 + t[1]
            if d0 <= tt < d1:
                problems.append(f"{cn} 目标 {node['目标']} 落在死区 {dz} 内")
        rng = node.get("容忍")
        if isinstance(rng, list) and len(rng) == 2:
            r0 = _hm(rng[0])[0] * 60 + _hm(rng[0])[1]
            r1 = _hm(rng[1])[0] * 60 + _hm(rng[1])[1]
            if r0 < d1 and d0 < r1:
                problems.append(f"{cn} 容忍窗口 {rng} 与死区 {dz} 重叠")
    return {"item": "死区与时段不重叠", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"死区 {dz} 与三批目标/容忍窗口均无重叠 ✅"])}


def check_recent_placement(days: int = 14) -> dict:
    """近 N 天成稿的写入时刻落点（告警项）。

    metadata 无逐篇时间戳，故用顶层 generated_at / last_updated（该日最后一次写入，
    近似当日末班执行时刻）按**天**统计——足以暴露「凌晨仍在写入」这类调度漂移。
    注意：generated_at 由 `datetime.now()` 生成，CI runner 时区为 UTC，
    故此处按 UTC 解析并换算为 CST 后再比对死区（勿直接当 CST 用）。
    """
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    days_seen = 0
    dead: list[str] = []
    hours: dict[int, int] = {}
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        d = Path(p).parent.name
        if len(d) != 10 or d[4] != "-" or d < floor:
            continue
        try:
            meta = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        ts = meta.get("generated_at") or meta.get("last_updated") or ""
        m = re.search(r"[ T](\d{2}):\d{2}", str(ts))
        if not m:
            continue
        days_seen += 1
        h = (int(m.group(1)) + 8) % 24          # UTC → CST
        hours[h] = hours.get(h, 0) + 1
        if h < 6:
            dead.append(f"{d} last_write(UTC)={ts} → CST {h:02d}:xx")
    if days_seen == 0:
        return {"item": f"近 {days} 天成稿写入时刻（告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": ["无可用时间戳，本项跳过"]}
    return {"item": f"近 {days} 天成稿写入时刻（告警）", "ok": not dead, "advisory": True,
            "days": days_seen, "by_hour_cst": dict(sorted(hours.items())), "dead": dead[:5],
            "detail": [f"近 {days} 天有 metadata 的 {days_seen} 天，末次写入 CST 小时分布 "
                       f"{dict(sorted(hours.items()))}（原始时间按 UTC 换算）",
                       ("无凌晨（死区）写入 ✅" if not dead else f"❌ 死区内写入 {len(dead)} 天：{dead[:3]}")]}


def verify(days: int = 14) -> dict:
    hard = [check_config(), check_targets_match_constants(), check_wrangler(),
            check_worker(), check_backstop(), check_deadzone_guard(), check_no_overlap()]
    soft = [check_recent_placement(days)]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 发布时段纪律验收报告（计划 3.3 / 第十章）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else "硬项"
        status = "跳过" if c.get("skipped") else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}（{tag}）：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    if rep.get("advisory_failed"):
        L += [f"> 告警项待关注：{'、'.join(rep['advisory_failed'])}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="发布时段纪律验收（计划 3.3 / 第十章）")
    ap.add_argument("--days", type=int, default=14, help="近 N 天成稿告警窗口（默认 14）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"publish_schedule_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "publish_schedule_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
