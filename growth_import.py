#!/usr/bin/env python3
"""粉丝增长数据适配层（R11 内容诊断 2026-10-13「建议5」/ 计划 P2-9 涨粉 / 第十章验收指标）。

为什么需要它：
  头条「图文数据」导出**不含涨粉列**（2026-10-13 实测 16 列：标题/发布时间/ID/链接/展现量/
  粉丝展现量/阅读量/粉丝阅读量/点击率/平均阅读完成率/平均阅读时长/点赞量/评论量/转发量/
  分享量/收藏量，无涨粉、无粉丝总数）。所以「转粉」这条线此前完全没有数据，
  而 R11 判断它是账号权重上不去的根因（粉丝阅读占比中位 0%）。

本模块提供独立入参通道：读头条后台「粉丝分析」导出的 CSV/XLSX（或手抄两列），
以 `日期 + 涨粉数` 或 `日期 + 粉丝总数` 两种形态入表，落盘 data/growth/fans_daily.json。

纪律（与全仓一致）：
  - 占位符 **-1 / -1.00 = 「无数据」→ None（待接入）**，绝不折算成 0（`is_missing`）；
  - 只有「粉丝总数」时按**相邻已登记日**差分得日涨粉；缺失日**不插值、不猜**；
  - **跨度守卫**：相邻日间隔 >1 天 → 差额只记为 `interval_gain`（区间净增），
    绝不写进 `new_followers`（区间值冒充单日涨粉 = 编造）；
  - 合并写：不覆盖已有日期的真实值，不拿 None 覆盖真实值；
  - 本模块只搬运数字，不做任何业务口径换算（口径在看板）。

操作节奏（2026-10-13 补充，见 docs/转粉与互动数据接入.md）：
  导出本身是**按日一行的序列**，一次导出覆盖一段区间 → **不需要每天操作**。
  推荐**每周一次**（滞后阈值 7 天正是按周更设计）；导出粒度务必是「每天一行」，
  只给「月末汇总」会让跨度守卫拦下，看板拿不到日均涨粉。

命令行：
  python3 growth_import.py --from /path/to/粉丝分析.xlsx --dry-run
  python3 growth_import.py --from /path --write
  python3 growth_import.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
GROWTH_PATH = PROJECT_ROOT / "data" / "growth" / "fans_daily.json"
CST = timezone(timedelta(hours=8))

# 规范字段 ← 中文列名（**必须最长匹配**：否则「粉丝涨粉数」被「涨粉数」吃掉这类问题会重演）
CANONICAL_FIELDS = ("date", "total_followers", "new_followers")
ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("统计日期", "数据日期", "日期", "时间", "date"),
    "total_followers": ("粉丝总数", "总粉丝数", "累计粉丝数", "累计粉丝", "粉丝数", "关注总数"),
    "new_followers": ("粉丝涨粉数", "涨粉数", "涨粉量", "净增粉丝", "新增粉丝", "粉丝增长数", "涨粉"),
}
# 已知但刻意忽略的列（参与最长匹配，防止被短别名吃掉）
IGNORED_ALIASES = ("粉丝展现量", "粉丝展现数", "取关数", "掉粉数", "粉丝净增率")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def normalize_count(v) -> int | None:
    """把单元格洗成计数：`-1 / -1.00 / 空 / -- / abc` → None（待接入），真实的 0 才是 0。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if float(v) < 0 else int(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s or s in {"-", "--", "—", "N/A", "n/a", "null", "None", "无", "暂无数据"}:
        return None
    m = _NUM_RE.fullmatch(s)
    if not m:
        return None
    n = float(m.group(0))
    return None if n < 0 else int(n)


def map_header(h) -> tuple[str | None, bool]:
    """中文表头 → 规范字段。返回 (field|None, ignored)；最长匹配，忽略列参与遮挡。"""
    if h is None:
        return None, False
    s = str(h).strip()
    if not s:
        return None, False
    best = None
    for field, names in ALIASES.items():
        for n in names:
            if n in s and (best is None or len(n) > best[1]):
                best = (field, len(n))
    for n in IGNORED_ALIASES:
        if n in s and (best is None or len(n) > best[1]):
            return None, True
    return (best[0], False) if best else (None, False)


def normalize_date(v) -> str | None:
    """把单元格洗成 YYYY-MM-DD；识别不了返回 None（不猜）。"""
    s = str(v or "").strip()
    m = re.match(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if not m:
        m2 = re.match(r"(\d{4})(\d{2})(\d{2})$", s)
        if not m2:
            return None
        y, mo, d = m2.groups()
    else:
        y, mo, d = m.groups()
    try:
        return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def read_table(path: Path | str) -> list[list]:
    """读 xlsx/csv（与 toutiao_import.read_table 同能力，此处避免反向依赖）。"""
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:  # pragma: no cover
            return []
        try:
            wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
        except Exception:
            return []
        ws = wb.active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if p.suffix.lower() in (".csv", ".txt", ".tsv"):
        delim = "\t" if p.suffix.lower() == ".tsv" else ","
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                with p.open("r", encoding=enc, newline="") as f:
                    return [row for row in csv.reader(f, delimiter=delim)]
            except (UnicodeDecodeError, LookupError):
                continue
    return []


def parse_rows(rows: list[list]) -> list[dict]:
    """解析表格为 [{date, total_followers, new_followers, _src_row}]（严格 missing 语义）。"""
    if not rows:
        return []
    # 定位表头行：第一行里至少命中 1 个规范字段
    header_i = 0
    for i, row in enumerate(rows[:5]):
        hit = sum(1 for c in row if map_header(c)[0])
        if hit >= 1:
            header_i = i
            break
    header = rows[header_i]
    idx: dict[str, int] = {}
    for j, h in enumerate(header):
        f, ignored = map_header(h)
        if f and f not in idx:
            idx[f] = j
    if "date" not in idx:
        return []

    out: list[dict] = []
    for row in rows[header_i + 1:]:
        def cell(f):
            j = idx.get(f)
            return row[j] if (j is not None and j < len(row)) else None
        d = normalize_date(cell("date"))
        if not d:
            continue
        rec = {"date": d,
               "total_followers": normalize_count(cell("total_followers")),
               "new_followers": normalize_count(cell("new_followers"))}
        if rec["total_followers"] is None and rec["new_followers"] is None:
            continue          # 该行两个指标都无数据 → 不登记（待接入）
        out.append(rec)
    return out


def derive_deltas(days: dict) -> list[str]:
    """对只有「粉丝总数」的日期，用**相邻已登记日**差分补日涨粉；缺失日不插值。

    ⚠️ 跨度守卫（2026-10-13 加固，用户问「一个月导一次行不行」时暴露的真缺陷）：
      相邻已登记日**间隔 >1 天**时，差额是「区间净增」，**不是**某一天的涨粉。
      把它写进 new_followers 就是编造——月更导出 / 区间断档必然踩到。
      故此时只登记 `interval_gain` + `interval_span_days`，**不写 new_followers**，
      下游（看板日均涨粉 / 滞后监控）因此不会把区间值当单日值。
      代价：月更导出的账号拿不到「日均涨粉」——这是**正确的诚实**，
      解法是改回每日粒度导出（头条「粉丝分析」本身按日一行）。
    """
    filled: list[str] = []
    dated = sorted((d for d, v in days.items()
                    if isinstance(v, dict) and v.get("total_followers") is not None))
    for prev_d, cur_d in zip(dated, dated[1:]):
        cur = days[cur_d]
        if cur.get("new_followers") is not None:
            continue
        delta = int(cur["total_followers"]) - int(days[prev_d]["total_followers"])
        span = _day_span(prev_d, cur_d)
        if span == 1:
            cur["new_followers"] = delta
            cur["derived"] = "total_delta"
            cur["derived_from"] = prev_d
            for k in ("interval_gain", "interval_span_days", "interval_from"):
                cur.pop(k, None)      # 补上细粒度导出后，旧的区间标记必须撤掉
            filled.append(cur_d)
        elif span and span > 1:
            cur["interval_gain"] = delta
            cur["interval_span_days"] = span
            cur["interval_from"] = prev_d
    return filled


def _day_span(a: str, b: str) -> int | None:
    """两个 YYYY-MM-DD 的天数差；识别不了返回 None。"""
    try:
        da = datetime.strptime(str(a)[:10], "%Y-%m-%d").date()
        db = datetime.strptime(str(b)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return (db - da).days


def interval_gains(path: Path | str | None = None) -> list[dict]:
    """跨多天的区间净增（未能归到某一天，故不进日均）。用于提示「导出粒度太粗」。"""
    return interval_gains_from(load(path).get("days") or {})


def interval_gains_from(days: dict) -> list[dict]:
    """interval_gains 的纯函数版（不碰文件，便于自检与单测）。"""
    out = []
    for d, v in sorted((days or {}).items()):
        if isinstance(v, dict) and v.get("interval_span_days"):
            out.append({"to": d, "from": v.get("interval_from"),
                        "span_days": int(v["interval_span_days"]),
                        "gain": v.get("interval_gain")})
    return out


def load(path: Path | str | None = None) -> dict:
    p = Path(path or GROWTH_PATH)
    if not p.exists():
        return {"updated_at": None, "source_files": [], "days": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"updated_at": None, "source_files": [], "days": {}}
    if not isinstance(data, dict):
        return {"updated_at": None, "source_files": [], "days": {}}
    data.setdefault("days", {})
    data.setdefault("source_files", [])
    data.setdefault("updated_at", None)
    return data


def merge(data: dict, records: list[dict], *, source: str = "") -> dict:
    """合并写入：不覆盖已有真实值（None 不覆盖真实值）；同步补差分。"""
    days = data.setdefault("days", {})
    added = updated = 0
    for rec in records:
        d = rec["date"]
        cur = days.get(d)
        if cur is None:
            days[d] = {"total_followers": rec.get("total_followers"),
                       "new_followers": rec.get("new_followers")}
            added += 1
            continue
        for k in ("total_followers", "new_followers"):
            if rec.get(k) is not None and cur.get(k) != rec[k]:
                cur[k] = rec[k]
                updated += 1
    derive_deltas(days)
    if source and source not in data.get("source_files", []):
        data.setdefault("source_files", []).append(source)
    data["updated_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    data["stats"] = {"days": len(days), "added": added, "updated": updated}
    return data


def save(data: dict, path: Path | str | None = None) -> Path:
    p = Path(path or GROWTH_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def daily_new_followers(path: Path | str | None = None) -> dict[str, int]:
    """{date: 日涨粉}（只含真实值；None 不登记，绝不填 0）。"""
    data = load(path)
    out: dict[str, int] = {}
    for d, v in (data.get("days") or {}).items():
        if isinstance(v, dict) and v.get("new_followers") is not None:
            out[d] = int(v["new_followers"])
    return out


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []

    # 占位符 -1 = 无数据 → None（绝不折算 0）
    cases.append(("-1 / -1.00 视为无数据", normalize_count(-1) is None and normalize_count("-1.00") is None))
    cases.append(("真实 0 就是 0", normalize_count(0) == 0 and normalize_count("0") == 0))
    cases.append(("空/-- /abc → None",
                  normalize_count("") is None and normalize_count("--") is None
                  and normalize_count("abc") is None))

    # 最长匹配：粉丝总数 不被「粉丝数」吃掉、涨粉数 不被别的吃掉
    cases.append(("表头最长匹配",
                  map_header("粉丝总数")[0] == "total_followers"
                  and map_header("粉丝涨粉数")[0] == "new_followers"
                  and map_header("涨粉数")[0] == "new_followers"))
    cases.append(("忽略列被遮挡", map_header("粉丝展现量") == (None, True)))

    rows = [["统计日期", "粉丝总数", "涨粉数"],
            ["2026-10-01", 10000, 12],
            ["2026-10-02", 10025, None],
            ["2026-10-03", 10060, 35],
            ["2026-10-04", -1, -1]]       # 整行无数据 → 不登记
    recs = parse_rows(rows)
    cases.append(("解析行数（占位行不登记）", len(recs) == 3))
    cases.append(("差分补涨粉（相邻已登记日）",
                  recs[1]["new_followers"] is None))     # 解析阶段不猜

    days = {r["date"]: {"total_followers": r["total_followers"],
                        "new_followers": r["new_followers"]} for r in recs}
    filled = derive_deltas(days)
    cases.append(("差分填出 10-02 的 25",
                  filled == ["2026-10-02"] and days["2026-10-02"]["new_followers"] == 25))
    cases.append(("已有真实值的日期不被差分覆盖",
                  days["2026-10-03"]["new_followers"] == 35))

    # 跨度守卫：间隔 >1 天不得写 new_followers（区间净增 ≠ 单日涨粉）
    gaps = {"2026-10-01": {"total_followers": 100, "new_followers": None},
            "2026-10-05": {"total_followers": 140, "new_followers": None}}
    gf = derive_deltas(gaps)
    cases.append(("跨 4 天不写单日涨粉（跨度守卫）",
                  gf == [] and gaps["2026-10-05"]["new_followers"] is None))
    cases.append(("跨天差额转入 interval_gain",
                  gaps["2026-10-05"]["interval_gain"] == 40
                  and gaps["2026-10-05"]["interval_span_days"] == 4
                  and gaps["2026-10-05"]["interval_from"] == "2026-10-01"))
    cases.append(("跨度守卫下不产生单日涨粉记录",
                  interval_gains_from(gaps) == [{"to": "2026-10-05", "from": "2026-10-01",
                                                 "span_days": 4, "gain": 40}]))

    # 合并写：None 不覆盖真实值
    data = {"days": {"2026-10-01": {"total_followers": 10000, "new_followers": 12}}}
    merge(data, [{"date": "2026-10-01", "total_followers": None, "new_followers": None}])
    cases.append(("None 不覆盖真实值", data["days"]["2026-10-01"]["new_followers"] == 12))

    # 日期归一
    cases.append(("日期归一",
                  normalize_date("2026/10/03") == "2026-10-03"
                  and normalize_date("2026年10月3日") == "2026-10-03"
                  and normalize_date("20261003") == "2026-10-03"
                  and normalize_date("昨天") is None))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="粉丝增长数据适配层（转粉通道）")
    ap.add_argument("--from", dest="src", help="粉丝分析导出文件或目录（xlsx/csv）")
    ap.add_argument("--write", action="store_true", help="写入 data/growth/fans_daily.json")
    ap.add_argument("--dry-run", action="store_true", help="只解析不入库")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest or not args.src:
        print("冒烟自检：")
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
        return 1 if bad else 0

    p = Path(args.src)
    files = sorted(p.glob("*.xlsx")) + sorted(p.glob("*.csv")) if p.is_dir() else [p]
    all_recs: list[dict] = []
    for f in files:
        recs = parse_rows(read_table(f))
        print(f"  {f.name}: {len(recs)} 行")
        all_recs += recs
    if not all_recs:
        print("❌ 未解析到任何转粉数据（检查表头是否含 日期 + 粉丝总数/涨粉数）")
        return 1

    seen: dict[str, dict] = {}
    for r in all_recs:
        seen[r["date"]] = r
    data = load()
    merge(data, list(seen.values()), source=",".join(f.name for f in files))
    filled = [d for d, v in data["days"].items() if isinstance(v, dict) and v.get("derived")]
    gaps = interval_gains_from(data["days"])
    print(f"共 {len(seen)} 个日期；其中 {len(filled)} 个日期由「粉丝总数」差分补出日涨粉")
    if gaps:
        print(f"⚠️ 另有 {len(gaps)} 处**跨多天的区间净增**未计入单日涨粉（导出粒度太粗）：")
        for g in gaps[:5]:
            print(f"   {g['from']} → {g['to']}（{g['span_days']} 天，净增 {g['gain']}）")
        print("   如需「日均涨粉」，请在后台按**每天一行**导出（粉丝分析本身支持日粒度）。")
    if args.write and not args.dry_run:
        path = save(data)
        print(f"✅ 已写入 {path}")
    else:
        print("（dry-run，未入库；加 --write 落盘）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
