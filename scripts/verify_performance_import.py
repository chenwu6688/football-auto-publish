#!/usr/bin/env python3
"""头条后台数据接入验收（计划第十章）。

背景：计划第十章的六项验收指标里，四项增长指标（篇均阅读 / 点击率 / 完成率 /
粉丝阅读占比）依赖头条后台真实数据。此前只有「规范 CSV + 手动查批次序号」这一条路，
而头条后台实际导出的是**中文列名、且只有标题**的 Excel —— 两条口径对不上就等于没接入。

本次接入做了两件事，本脚本就是把它们钉死：
  1. 中文列名 → 规范字段（**最长匹配**，防止「粉丝阅读量」被「阅读量」吃掉等误读）；
  2. 标题 → (日期, slug)（**slug 才是唯一键**：index 是批内序号，同一天多批次会重复）。

硬项（不达标退出码 1）
  A. 主键唯一性——真实 metadata 里 slug 每天唯一（index 会重复，故不可作键）
  B. 列名映射——中文列名与最长匹配、已知但忽略列（粉丝展现量/点击率）不被误映射
  C. 表头定位——表头不在首行也能探测
  D. 无依赖解析——.xlsx 用标准库解出（不依赖 openpyxl/pandas）
  E. 数值清洗——万/亿、千分位、百分号、占位符（--/—/无）
  F. 标题匹配——命中得到 (日期, slug)，**未匹配与歧义行一律不写库**
  G. 未接入不冒充——表头无法识别时明确报错，不产出 0 值记录
  H. 不串号——同名 index 的两篇不同文章写入互不覆盖
  J. 绩效源回落——无本地台账时从 metadata.articles[].performance 取数（CI 可用）

告警项（只提示）
  I. 未识别列可见——导出里不认识的列会出现在报告里，不静默丢弃

用法：python3 scripts/verify_performance_import.py [--quiet]
产出：data/audit/performance_import_<date>.md + data/audit/performance_import_latest.json
"""

from __future__ import annotations

import argparse
import contextlib
import html
import io
import json
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import performance_logger as pl   # noqa: E402
import toutiao_import as tti      # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))


# ------------------------------------------------------------
# 合成夹具（明确是合成的，不是真实数据）
# ------------------------------------------------------------

def _synthetic_article_index() -> dict:
    """两篇「同一天 index 都是 2、slug 不同」的合成文章，用来暴露串号问题。"""
    return {
        tti.normalize_title("A队三球逆转，老六怎么看？"): [
            {"date": "2026-10-12", "slug": "a-dui-sann-qiu-abc123",
             "index": 2, "title": "A队三球逆转，老六怎么看？"}],
        tti.normalize_title("B队崩盘，教练该不该下课"): [
            {"date": "2026-10-12", "slug": "b-dui-beng-pan-def456",
             "index": 2, "title": "B队崩盘，教练该不该下课"}],
    }


def _synthetic_metadata_dir(tmp: Path) -> Path:
    """写一份合成 metadata.json（含重复 index），返回 output 根目录。"""
    d = tmp / "2026-10-12"
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps({
        "date": "2026-10-12",
        "articles": [
            {"index": 2, "slug": "a-dui-sann-qiu-abc123", "title": "A队三球逆转，老六怎么看？"},
            {"index": 2, "slug": "b-dui-beng-pan-def456", "title": "B队崩盘，教练该不该下课"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp


def _write_min_xlsx(path: Path, rows: list) -> None:
    """写一个最小合法 xlsx（sharedStrings + sheet1），用来验无依赖解析。"""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    strings, sidx = [], {}

    def sid(v):
        v = str(v)
        if v not in sidx:
            sidx[v] = len(strings)
            strings.append(v)
        return sidx[v]

    body = []
    for r, row in enumerate(rows, 1):
        cells = "".join(f'<c r="{chr(65 + c)}{r}" t="s"><v>{sid(v)}</v></c>'
                        for c, v in enumerate(row) if v != "")
        body.append(f'<row r="{r}">' + cells + '</row>')
    sst = ('<?xml version="1.0" encoding="UTF-8"?><sst ' + ns +
           f' count="{len(strings)}" uniqueCount="{len(strings)}">' +
           "".join('<si><t xml:space="preserve">%s</t></si>' % html.escape(s)
                   for s in strings) + '</sst>')
    ws = ('<?xml version="1.0" encoding="UTF-8"?><worksheet ' + ns +
          '><sheetData>' + "".join(body) + '</sheetData></worksheet>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.'
          'openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
          'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
          'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
          'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.'
          'openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
    rl = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
          'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
          'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
          'officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8"?><workbook ' + ns +
          '><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" xmlns:r="http://'
          'schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
          '</sheets></workbook>')
    wr = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
          'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
          'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
          'worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" '
          'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
          'sharedStrings" Target="sharedStrings.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rl)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wr)
        z.writestr("xl/worksheets/sheet1.xml", ws)
        z.writestr("xl/sharedStrings.xml", sst)


_EXPORT_ROWS = [
    ["头条号 · 作品数据导出"],
    [""],
    ["标题", "内容类型", "发布时间", "展现量", "阅读量", "粉丝阅读量", "粉丝展现量",
     "阅读完成率", "点赞量", "评论量", "分享量", "涨粉数", "点击率"],
    ["A队三球逆转，老六怎么看？", "图文", "2026-10-12 08:05", "18.2万", "5,120",
     "218", "16.1万", "41.2%", "88", "26", "19", "12", "2.8%"],
    ["B队崩盘，教练该不该下课", "图文", "2026-10-12 12:05", "9.6万", "3,044",
     "163", "8.4万", "38.7%", "51", "14", "11", "7", "3.2%"],
    ["随手一条微头条", "微头条", "2026-10-12 14:20", "2.1万", "900",
     "30", "1.9万", "20%", "12", "3", "1", "0", "4%"],
    ["一篇没收录过的旧文", "图文", "2026-09-01 10:00", "1万", "100",
     "5", "0.9万", "30%", "1", "0", "0", "0", "1%"],
]


# ------------------------------------------------------------
# 硬项
# ------------------------------------------------------------

def check_slug_uniqueness() -> dict:
    """真实 metadata：slug 每天必须唯一；并报告 index 是否重复（说明换键的必要）。"""
    dup_slug, dup_index, days = [], [], 0
    for mp in sorted(OUTPUT_DIR.glob("*/metadata.json")):
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        days += 1
        seen_s, seen_i = set(), set()
        for a in meta.get("articles", []) or []:
            s, i = a.get("slug"), a.get("index")
            if not s:
                dup_slug.append(f"{mp.parent.name}#{i} 缺 slug")
                continue
            if s in seen_s:
                dup_slug.append(f"{mp.parent.name}/{s}")
            seen_s.add(s)
            if i in seen_i:
                dup_index.append(f"{mp.parent.name}#{i}")
            seen_i.add(i)
    ok = not dup_slug
    return {"item": "主键唯一性（slug 每天唯一；index 会重复故不可作键）", "ok": ok,
            "advisory": False, "days": days,
            "dup_slug": dup_slug[:8], "dup_index_count": len(dup_index),
            "detail": ([f"❌ slug 重复/缺失：{dup_slug[:5]}"] if not ok else
                       [f"扫描 {days} 天：slug 全部唯一 ✅",
                        f"同期 index 重复 {len(dup_index)} 处"
                        + ("（正是本次把主键从 index 换成 slug 的原因）"
                           if dup_index else "（本次无重复，但键仍以 slug 为准）")])}


def check_header_mapping() -> dict:
    cases = [
        ("标题", "title"), ("发布时间", "date"), ("展现量", "impressions"),
        ("阅读量", "reads"), ("阅读量(次)", "reads"), ("粉丝阅读量", "follower_reads"),
        ("阅读完成率", "retention_rate"), ("完读率", "retention_rate"),
        ("点赞量", "likes"), ("评论量", "comments"), ("涨粉数", "new_followers"),
        ("分享量", "shares"),
    ]
    bad = [f"{h} → {tti.map_header(h)[0]}（期望 {f}）"
           for h, f in cases if tti.map_header(h)[0] != f]
    # 最长匹配：这些列必须被「已知但忽略」，绝不落到更短的别名上
    shadow = ["粉丝展现量", "点击率", "平均阅读时长", "收藏量"]
    for h in shadow:
        f, ignored = tti.map_header(h)
        if f is not None or not ignored:
            bad.append(f"{h} 未被正确忽略（落到 {f}）")
    unknown = tti.map_header("某某乱列")
    if unknown[0] is not None:
        bad.append(f"未知列被误映射为 {unknown[0]}")
    return {"item": "列名映射（含最长匹配与忽略列遮挡）", "ok": not bad, "advisory": False,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else
                       [f"{len(cases)} 个中文列名全部映射正确",
                        f"「粉丝展现量/点击率/平均阅读时长/收藏量」被识别为忽略列，"
                        "未污染 impressions/reads ✅"])}


def check_header_row_detect() -> dict:
    idx, mapping = tti.detect_header(tti.pad_rows(_EXPORT_ROWS))
    ok = idx == 2 and "title" in mapping and len(mapping) >= 8
    return {"item": "表头定位（非首行可探测）", "ok": ok, "advisory": False,
            "header_row": idx, "mapped": len(mapping),
            "detail": [f"表头在第 {idx + 1 if idx is not None else '?'} 行、识别 "
                       f"{len(mapping)} 列" + ("✅" if ok else " ❌（期望第 3 行）")]}


def check_xlsx_nodep() -> dict:
    try:
        import openpyxl  # noqa: F401
        has = True
    except Exception:
        has = False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.xlsx"
        _write_min_xlsx(p, _EXPORT_ROWS)
        try:
            table = tti.read_table(p)
        except Exception as e:
            return {"item": "无依赖解析 .xlsx", "ok": False, "advisory": False,
                    "detail": [f"❌ 解析失败：{type(e).__name__}: {e}"]}
    idx, mapping = tti.detect_header(tti.pad_rows(table))
    ok = idx == 2 and mapping.get("title") == 0
    return {"item": "无依赖解析 .xlsx（标准库；不依赖 openpyxl/pandas）", "ok": ok,
            "advisory": False, "openpyxl_present": has,
            "detail": [f"标准库解出 {len(table)} 行 × {max(len(r) for r in table)} 列"
                       + ("✅" if ok else " ❌"),
                       f"本机 openpyxl 存在={has}（不影响本项目路径）"]}


def check_number_cleaning() -> dict:
    cases = [("1.8万", 18000.0), ("5,120", 5120.0), ("2.1亿", 210000000.0),
             ("4,120", 4120.0), ("0.412", 0.412), ("41.2", 41.2)]
    bad = []
    for raw, want in cases:
        got = tti.clean_number(raw)
        if got is None or abs(got - want) > 1e-6:
            bad.append(f"{raw} → {got}（期望 {want}）")
    for blank in ("", "--", "—", "无", "N/A"):
        if tti.clean_number(blank) is not None:
            bad.append(f"{blank!r} 应视为缺数据（None），不该得到数值")
    # 百分号 → 小数 是完读率专属换算，不属于通用数值清洗
    for raw, want in (("41.2%", 0.412), ("0.387", 0.387), ("38.7%", 0.387)):
        if tti.normalize_retention(raw) != want:
            bad.append(f"完读率 {raw} → {tti.normalize_retention(raw)}（期望 {want}）")
    # 头条用 -1 / -1.00 表示「该作品无完读率数据」，必须判为 None（待接入），
    # 绝不能折算成 0%（那是把「无数据」冒充成「真实 0」）。
    for raw in ("-1", "-1.00", "-1.0"):
        if tti.normalize_retention(raw) is not None:
            bad.append(f"完读率占位 {raw} → {tti.normalize_retention(raw)}"
                       f"（应为 None，不能当 0）")
    # 计数类指标（阅读量/粉丝阅读量/展现量…）同样用 -1 表示「无数据」→ None
    # （实测 2026-10-12 导出：801-869 段有 69 行 阅读量/粉丝阅读量 = -1）
    rec = {}
    tti._fill_metrics(rec, lambda f: "-1")
    for f in ("reads", "follower_reads", "impressions"):
        if rec[f] is not None:
            bad.append(f"计数指标占位 {f} → {rec[f]}（应为 None，不能当 0）")
    rec0 = {}
    tti._fill_metrics(rec0, lambda f: "0")
    if rec0["reads"] != 0:
        bad.append(f"真实 0 被误伤：reads → {rec0['reads']}（应保留 0）")
    return {"item": "数值清洗（万/亿、千分位、占位符）与完读率换算", "ok": not bad,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else
                       ["单位与千分位换算正确；「--/—/无」判为缺数据而非 0 ✅",
                        "完读率 41.2% → 0.412（百分号只在完读率上换算）✅",
                        "完读率占位 -1 / -1.00 → None（待接入），不冒充 0 ✅",
                        "计数指标占位 -1 → None；真实 0 仍为 0（不误伤）✅"])}


def check_title_match_and_no_write() -> dict:
    """命中得到 (日期, slug)；未匹配与歧义行不得进入 records。"""
    tmp = Path(tempfile.mkdtemp())
    outdir = _synthetic_metadata_dir(tmp)
    res = tti.adapt_rows(_EXPORT_ROWS, output_dir=outdir)
    rep = res["report"]
    got = {(r["slug"], r["reads"]) for r in res["records"]}
    problems = []
    if got != {("a-dui-sann-qiu-abc123", 5120), ("b-dui-beng-pan-def456", 3044)}:
        problems.append(f"命中记录不符：{sorted(got)}")
    if len(rep["unmatched"]) != 1:
        problems.append(f"未匹配应为 1 行，实为 {len(rep['unmatched'])}")
    if len(rep["non_article"]) != 1:
        problems.append(f"非图文应为 1 行，实为 {len(rep['non_article'])}")
    titles = {r["source_title"] for r in res["records"]}
    if any("没收录" in t or "微头条" in t for t in titles):
        problems.append("未匹配/非图文行被写进了 records")

    # 歧义 1：同名标题跨两天、导出无发布时间可消歧 → 必须判 ambiguous 且不写库
    amb_index = {
        tti.normalize_title("同名标题"): [
            {"date": "2026-10-12", "slug": "same-a", "index": 1, "title": "同名标题"},
            {"date": "2026-10-13", "slug": "same-b", "index": 1, "title": "同名标题"},
        ]
    }
    amb = tti.adapt_rows([["标题", "阅读量"], ["同名标题", "123"]], title_index=amb_index)
    if len(amb["report"]["ambiguous"]) != 1 or amb["records"]:
        problems.append(
            f"跨天同名应判歧义 1 行且不写库，实为歧义 {len(amb['report']['ambiguous'])}、"
            f"记录 {len(amb['records'])}")

    # 歧义 2：导出带发布时间 → 应用日期消歧为唯一命中（而非报歧义）
    dis = tti.adapt_rows([["标题", "发布时间", "阅读量"],
                          ["同名标题", "2026-10-13 09:00", "123"]], title_index=amb_index)
    if len(dis["records"]) != 1 or dis["records"][0]["slug"] != "same-b":
        problems.append(
            f"带发布时间应消歧到 same-b，实为 {[r['slug'] for r in dis['records']]}")

    return {"item": "标题匹配得 (日期, slug)；未匹配/歧义不写库，有时间则消歧", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems else
                       [f"命中 {len(rep['matched'])}、未匹配 {len(rep['unmatched'])}、"
                        f"非图文 {len(rep['non_article'])}（均未写库）✅",
                        "跨天同名无时间 → 判歧义不写库；带发布时间 → 消歧为唯一命中 ✅",
                        "标题按规范化比较，标点/破折号差异不影响匹配"])}


def check_no_fake_zero() -> dict:
    """表头无法识别 → 明确报错且不产出任何记录（不冒充 0）。"""
    bad_rows = [["完全无关的表格"], ["甲", "乙", "丙"], ["1", "2", "3"]]
    res = tti.adapt_rows(bad_rows, output_dir=Path(tempfile.mkdtemp()))
    rep = res["report"]
    ok = (rep.get("ok") is False and not res["records"] and bool(rep.get("error")))
    return {"item": "未接入不冒充（表头无法识别即报错，不产出 0 值）", "ok": ok,
            "advisory": False,
            "detail": ([f"❌ ok={rep.get('ok')} records={len(res['records'])}"] if not ok else
                       [f"明确报错且 records=0：{rep['error'][:60]}… ✅"])}


def check_key_no_collision() -> dict:
    """同名 index 的两篇不同 slug 写入 performance_log 必须互不覆盖。"""
    tmp = Path(tempfile.mkdtemp())
    saved = (pl.OUTPUT_DIR, pl.PERF_LOG_PATH)
    pl.OUTPUT_DIR = tmp
    pl.PERF_LOG_PATH = tmp / "performance_log.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pl.log_article_performance("2026-10-12", 2, reads=5120, impressions=182000,
                                       slug="a-dui-sann-qiu-abc123")
            pl.log_article_performance("2026-10-12", 2, reads=3044, impressions=96000,
                                       slug="b-dui-beng-pan-def456")
        arts = json.loads((tmp / "performance_log.json").read_text(encoding="utf-8"))["articles"]
        a = arts.get("2026-10-12/a-dui-sann-qiu-abc123", {})
        b = arts.get("2026-10-12/b-dui-beng-pan-def456", {})
        ok = (len(arts) == 2 and a.get("reads") == 5120 and b.get("reads") == 3044)
        # 旧键兜底：无 slug 时仍写老键，保证历史数据可读
        with contextlib.redirect_stdout(io.StringIO()):
            pl.log_article_performance("2026-10-12", 7, reads=1)
        arts2 = json.loads((tmp / "performance_log.json").read_text(encoding="utf-8"))["articles"]
        legacy_ok = "2026-10-12/article-7" in arts2
    finally:
        pl.OUTPUT_DIR, pl.PERF_LOG_PATH = saved
    return {"item": "不串号（同 index 不同 slug 互不覆盖；旧键仍可写）", "ok": ok and legacy_ok,
            "advisory": False,
            "detail": ([f"❌ 记录数={len(arts)} a={a.get('reads')} b={b.get('reads')}"] if not (ok and legacy_ok)
                       else [f"同 index 的两篇写入 2 条独立记录（5120 / 3044）✅",
                             "无 slug 时回退旧键 article-<index>，历史数据仍可读 ✅"])}


def check_selftest() -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = tti.selftest()
    return {"item": "适配层自检（toutiao_import.selftest）", "ok": rc == 0,
            "advisory": False,
            "detail": ["自检通过 ✅" if rc == 0 else f"自检未通过 ❌ {buf.getvalue()[:120]}"]}


def check_ratio_tuner_fallback() -> dict:
    """绩效源回落（硬项）——无本地台账时也能取到真实观测。

    output/performance_log.json 在 .gitignore 内，而自动调参（ratio-tuner.yml）跑在
    干净的 CI 检出上，若只认台账则该任务**永远无样本**（结构性缺口）。
    故 ratio_tuner 必须能回落读 metadata 的 articles[].performance（该字段随仓库提交）。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ratio_tuner_probe", PROJECT_ROOT / "scripts" / "ratio_tuner.py")
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    tmp = Path(tempfile.mkdtemp())
    d = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp / d).mkdir(parents=True)
    (tmp / d / "metadata.json").write_text(json.dumps({"articles": [
        {"index": 1, "slug": "probe-1", "content_type": "转会资讯", "column_name": "转会雷达",
         "performance": {"reads": 1234, "retention_rate": 0.4}}]}), encoding="utf-8")
    old = rt.OUTPUT_DIR
    rt.OUTPUT_DIR = tmp
    try:
        obs = rt.collect_observations(14)
    finally:
        rt.OUTPUT_DIR = old
    ok = len(obs) == 1 and obs[0]["reads"] == 1234 and obs[0]["section"] == "转会动态"
    return {"item": "绩效源回落（无台账时从 metadata 取数）", "ok": ok, "advisory": False,
            "detail": [f"无台账时取到 {len(obs)} 条观测（期望 1 条、reads=1234）"
                       + (" ✅" if ok else " ❌")]}


# ------------------------------------------------------------
# 告警项
# ------------------------------------------------------------

def check_unmapped_visible() -> dict:
    tmp = Path(tempfile.mkdtemp())
    outdir = _synthetic_metadata_dir(tmp)
    rows = [r[:] for r in _EXPORT_ROWS]
    rows[2] = rows[2] + ["某个新指标"]
    for r in rows[3:]:
        r.append("42")
    rep = tti.adapt_rows(rows, output_dir=outdir)["report"]
    ok = rep["unmapped"] == ["某个新指标"]
    return {"item": "未识别列可见（不静默丢弃）", "ok": ok, "advisory": True,
            "unmapped": rep["unmapped"],
            "detail": [f"未识别列：{rep['unmapped']}" + ("✅" if ok else " ❌")]}


# ------------------------------------------------------------

def verify() -> dict:
    hard = [check_slug_uniqueness(), check_header_mapping(), check_header_row_detect(),
            check_xlsx_nodep(), check_number_cleaning(),
            check_title_match_and_no_write(), check_no_fake_zero(),
            check_key_no_collision(), check_selftest(), check_ratio_tuner_fallback()]
    soft = [check_unmapped_visible()]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 头条后台数据接入验收报告（计划第十章）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else "硬项"
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    if rep.get("advisory_failed"):
        L += [f"> 告警项待关注：{'、'.join(rep['advisory_failed'])}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="头条后台数据接入验收（计划第十章）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"performance_import_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "performance_import_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
