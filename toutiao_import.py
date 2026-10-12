#!/usr/bin/env python3
"""头条号后台导出 → performance_log 规范化适配层（计划第十章）。

为什么需要这一层
----------------
头条号后台导出的表格（「数据 → 作品数据 → 导出 Excel」，或「数据中心 → 数据导出」）
有三个特点，正好与我们的内部结构错位：

  1. **列名是中文**，且随平台改版漂移（展现量/曝光量、完读率/阅读完成率…）；
  2. **只有「标题」，没有我们内部的「日期 / 序号」**；
  3. 表头往往不在第一行（前面有标题行、说明行、空行）。

因此导入要解决两件事：

  A. 中文列名 → 规范字段：别名表 + **最长匹配**。
     必须最长匹配，否则「粉丝阅读量」会被「阅读量」吃掉、
     「粉丝展现量」会被「展现量」吃掉，指标直接算错。
  B. 标题 → (日期, slug)：对齐 output/<日期>/metadata.json。
     注意 **slug 才是唯一键** —— index 是「每批从 1 开始」的批内序号，
     同一天多批次会重复（2026-10-12 实测 index 2/3 各出现两次）。

本模块只做纯逻辑（读取 / 映射 / 匹配 / 清洗），I/O 编排与命令行在
scripts/import_performance.py。

诚实原则（计划第十章）：本模块只做「搬运与对齐」，**不做插值、估算或补零**；
导出里没有的字段留空由下游标「待接入」，绝不用 0 冒充真实数据。
"""

from __future__ import annotations

import csv as _csv
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# ============================================================
# A. 列名映射
# ============================================================

# 规范字段（与 performance_logger.log_article_performance 的入参一致）
CANONICAL_FIELDS = (
    "title", "date", "index", "reads", "impressions", "follower_reads",
    "retention_rate", "likes", "comments", "shares", "new_followers",
)

# 字段 → 别名。匹配时按「别名长度从长到短」逐个试，
# 保证「粉丝阅读量」压过「阅读量」、「粉丝展现量」压过「展现量」。
ALIASES = {
    "title": ("标题", "作品标题", "内容标题", "文章标题", "作品名称", "内容名称", "文章名称"),
    "date": ("发布时间", "发布日期", "发表时间", "发文时间", "发布日", "日期"),
    "index": ("序号", "编号"),
    "reads": ("阅读量", "阅读数", "阅读次数", "阅读"),
    "impressions": ("展现量", "展现数", "曝光量", "曝光数", "推荐量", "展现", "曝光"),
    "follower_reads": ("粉丝阅读量", "粉丝阅读数", "粉丝阅读/播放量", "粉丝阅读"),
    "retention_rate": ("阅读完成率", "阅读/播放完成率", "读完率", "完读率", "完成率"),
    "likes": ("点赞量", "点赞数", "点赞"),
    "comments": ("评论量", "评论数", "评论"),
    "shares": ("分享量", "分享数", "转发量", "转发数", "分享", "转发"),
    "new_followers": ("涨粉数", "涨粉量", "粉丝变化数", "净增粉丝", "新增粉丝", "粉丝增长数", "涨粉"),
}

# 用于筛选：导出里混有微头条/视频/问答，只有图文才可能对上我们的 metadata
FILTER_ALIASES = {
    "content_type": ("内容类型", "作品类型", "体裁", "类型"),
}
ARTICLE_TYPES = ("图文", "文章")

# 认识但用不上的列：参与最长匹配，用来「挡」住更短的别名，避免误读。
# 例如「粉丝展现量」若不被挡住，会命中「展现量」被当成 impressions。
IGNORED_ALIASES = (
    "粉丝展现量", "粉丝展现数",
    "点击率", "互动率", "跳出率",
    "平均阅读时长", "平均阅读/播放时长", "平均阅读速度",
    "阅读完成率明细", "读完量",
    "收藏量", "收藏数", "收藏",
    "收益", "预计收益", "千次阅读单价",
    "主页访问量", "推荐频道展现量",
    "审核状态", "发布状态",
)

MIN_HEADER_HITS = 2


def _strip_brackets(s: str) -> str:
    """去掉括号内单位，如「阅读量(次)」「展现量（次）」→「阅读量」。"""
    return re.sub(r"[（(][^）)]*[）)]", "", s).strip()


def normalize_header(h) -> str:
    """列名规范化：全半角统一 → 去括号单位 → 只保留字母/数字/汉字 → 小写。"""
    s = unicodedata.normalize("NFKC", str(h or ""))
    s = _strip_brackets(s)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in ("L", "N")).lower()


def _build_alias_table() -> dict:
    table: dict = {}
    for field, names in ALIASES.items():
        for n in names:
            table.setdefault(normalize_header(n), field)
    for field, names in FILTER_ALIASES.items():
        for n in names:
            table.setdefault(normalize_header(n), field)
    for n in IGNORED_ALIASES:
        table.setdefault(normalize_header(n), None)
    return table


_ALIAS_TABLE = _build_alias_table()
_ALIAS_KEYS_BY_LEN = sorted(_ALIAS_TABLE, key=len, reverse=True)


def map_header(h) -> tuple:
    """列名 → (规范字段, 是否已知但忽略)。

    返回 (None, False) = 完全不认识；(None, True) = 认识但不用（如粉丝展现量）。
    """
    key = normalize_header(h)
    if not key:
        return None, False
    if key in _ALIAS_TABLE:
        f = _ALIAS_TABLE[key]
        return f, (f is None)
    for ak in _ALIAS_KEYS_BY_LEN:
        if ak and ak in key:
            f = _ALIAS_TABLE[ak]
            return f, (f is None)
    return None, False


# ============================================================
# B. 数值清洗（中文导出的单位/百分号/千分位）
# ============================================================

_UNIT_MULT = (("亿", 100000000.0), ("万", 10000.0), ("千", 1000.0))
_BLANK_TOKENS = {"", "-", "--", "—", "–", "/", "n/a", "na", "无", "暂无", "null", "none"}


def clean_number(v):
    """中文导出数值 → float；空/「--」「—」「无」等占位一律返回 None（不冒充 0）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if s.lower() in _BLANK_TOKENS:
        return None
    s = s.replace(",", "").replace(" ", "").replace("%", "").replace("次", "")
    mult = 1.0
    for unit, m in _UNIT_MULT:
        if unit in s:
            mult = m
            s = s.replace(unit, "")
            break
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    return float(m.group(0)) * mult


def clean_int(v):
    x = clean_number(v)
    if x is None:
        return None
    return int(round(x))


def normalize_retention(v):
    """完读率 → 0-1 小数；>1 视为百分数自动换算；无法解析返回 None。

    **负数一律返回 None**：头条后台用 `-1`（及 `-1.00`）表示「该作品无完读率数据」，
    它不是「0% 完读率」。若折算成 0.0，就是把「无数据」冒充成「真实 0」，
    违反第十章诚实原则（实测 2026-10-10 导出有 4 行 -1.00）。
    """
    x = clean_number(v)
    if x is None or x < 0:
        return None
    if x > 1.0:
        x = x / 100.0
    return round(min(max(x, 0.0), 1.0), 4)


# ============================================================
# C. 读表（无依赖：xlsx 自己解 zip + XML）
# ============================================================

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_to_index(ref) -> int | None:
    m = re.match(r"([A-Za-z]+)", str(ref or ""))
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(path: Path) -> list:
    """读取 .xlsx（zip + XML），不依赖 openpyxl / pandas。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared: list = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
        sheets = sorted(n for n in names
                        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise ValueError("xlsx 中未找到工作表（xl/worksheets/sheetN.xml）")
        root = ET.fromstring(z.read(sheets[0]))
        out: list = []
        for row in root.iter(_NS + "row"):
            cells: dict = {}
            for c in row.findall(_NS + "c"):
                ci = _col_to_index(c.get("r"))
                if ci is None:
                    continue
                ctype = c.get("t")
                v = c.find(_NS + "v")
                if ctype == "s" and v is not None and v.text not in (None, ""):
                    try:
                        idx = int(v.text)
                        val = shared[idx] if 0 <= idx < len(shared) else ""
                    except Exception:
                        val = ""
                elif ctype == "inlineStr":
                    is_el = c.find(_NS + "is")
                    val = ("".join(t.text or "" for t in is_el.iter(_NS + "t"))
                           if is_el is not None else "")
                else:
                    val = (v.text or "") if v is not None else ""
                cells[ci] = str(val)
            width = (max(cells) + 1) if cells else 0
            out.append([cells.get(i, "") for i in range(width)])
        return out


def _json_to_table(path: Path) -> list:
    """JSON → 二维表：支持对象数组（中文键也行）、二维数组、{"articles": [...]}。"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("articles") or data.get("rows") or data.get("data") or []
    if not isinstance(data, list) or not data:
        return []
    if all(isinstance(r, list) for r in data):
        return [[str(c) for c in r] for r in data]
    if all(isinstance(r, dict) for r in data):
        keys: list = []
        for r in data:
            for k in r:
                if k not in keys:
                    keys.append(k)
        table = [list(keys)]
        for r in data:
            table.append(["" if r.get(k) is None else str(r.get(k)) for k in keys])
        return table
    return []


def read_table(path) -> list:
    """读取导出文件为二维字符串表。

    支持 .xlsx / .csv / .tsv / .txt / .json；.xls（旧二进制）明确报错不静默失败。
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix == ".xls":
        raise ValueError(
            "旧版 .xls 二进制无法在无依赖下解析；请在头条后台导出为 .xlsx 或 .csv")
    if suffix == ".json":
        return _json_to_table(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delim = "\t" if (suffix == ".tsv" or first.count("\t") > first.count(",")) else ","
    return [[str(c) for c in row] for row in _csv.reader(text.splitlines(), delimiter=delim)]


def pad_rows(rows: list) -> list:
    """把参差不齐的二维表补齐成等宽，方便按列索引取数。"""
    width = max((len(r) for r in rows), default=0)
    return [list(r) + [""] * (width - len(r)) for r in rows]


def detect_header(rows: list, max_scan: int = 12) -> tuple:
    """在前若干行里找表头行，返回 (行号, {字段: 列号})；找不到返回 (None, {})。

    头条导出的表头常常不在第一行，故不能只看第 0 行。
    """
    best = (None, {}, 0)
    for i, row in enumerate(rows[:max_scan]):
        mapping: dict = {}
        for j, cell in enumerate(row):
            f, _ignored = map_header(cell)
            if not f or f in mapping:
                continue
            mapping[f] = j
        hits = len([f for f in mapping if f in CANONICAL_FIELDS])
        if hits > best[2]:
            best = (i, mapping, hits)
    if best[2] < MIN_HEADER_HITS:
        return None, {}
    return best[0], best[1]


# ============================================================
# D. 标题 → (日期, slug) 匹配
# ============================================================

def normalize_title(s) -> str:
    """标题规范化：NFKC + 只保留字母/数字/汉字 + 小写。

    这样「森林1-3崩盘！」与「森林13崩盘」视为同一篇（与 slug 的生成口径一致）。
    """
    s = unicodedata.normalize("NFKC", str(s or ""))
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in ("L", "N")).lower()


def build_article_index(output_dir) -> dict:
    """扫描 output/<日期>/metadata.json，建 {规范化标题: [候选…]}。

    候选含 date / slug / index / title；**slug 是唯一键**。
    """
    index: dict = {}
    for mp in sorted(Path(output_dir).glob("*/metadata.json")):
        date_str = mp.parent.name
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in meta.get("articles", []) or []:
            title = a.get("title") or ""
            k = normalize_title(title)
            if not k:
                continue
            index.setdefault(k, []).append({
                "date": date_str, "slug": a.get("slug"),
                "index": a.get("index"), "title": title,
            })
    return index


_DATE_PAT = re.compile(r"(\d{4})\D(\d{1,2})\D(\d{1,2})")


def parse_date_like(v) -> str | None:
    """从「2026-10-10 08:03」「2026/10/10」里取日期，返回 YYYY-MM-DD。"""
    m = _DATE_PAT.search(unicodedata.normalize("NFKC", str(v or "")))
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def match_article(title, index_map: dict, date_hint=None) -> tuple:
    """标题 → 候选文章列表 + 结论。

    返回 ([候选…], reason)，reason ∈ matched / unmatched / ambiguous。
    有 date_hint 时先用它缩候选，解决跨天同名标题的歧义。
    """
    k = normalize_title(title)
    if not k:
        return [], "unmatched"
    cands = list(index_map.get(k, []))
    if not cands:
        return [], "unmatched"
    if date_hint:
        same = [c for c in cands if c["date"] == date_hint]
        if same:
            cands = same
    if len(cands) == 1:
        return cands, "matched"
    return cands, "ambiguous"


_INT_METRICS = ("reads", "impressions", "follower_reads",
                "likes", "comments", "shares", "new_followers")


def _fill_metrics(rec: dict, cell) -> None:
    for f in _INT_METRICS:
        v = clean_int(cell(f))
        if v is None:
            rec[f] = 0            # 该列未提供 → schema 约定用 0（不凭空造值）
        elif v < 0:
            rec[f] = None         # 负占位（头条用 -1）= 该指标无数据 → 「待接入」
        else:
            rec[f] = v
    # 完读率：负占位（-1）与空值都保持 None = 「待接入」，绝不冒充 0
    rec["retention_rate"] = normalize_retention(cell("retention_rate"))


def adapt_rows(rows: list, *, output_dir=None, title_index: dict | None = None,
               header_row: int | None = None) -> dict:
    """头条导出二维表 → {records, report}。

    records 是可直接喂给 validate_row / import_rows 的规范记录；
    report 记录表头位置、列映射、未识别列，以及 matched / unmatched /
    ambiguous / non_article 逐行明细（**未匹配的行不写库**）。
    """
    rows = pad_rows(rows)
    if header_row is None:
        header_row, mapping = detect_header(rows)
    else:
        mapping = {}
        for j, cell in enumerate(rows[header_row] if header_row < len(rows) else []):
            f, _ig = map_header(cell)
            if f and f not in mapping:
                mapping[f] = j

    report = {"header_row": header_row, "mapping": {}, "unmapped": [],
              "matched": [], "unmatched": [], "ambiguous": [], "non_article": []}

    if header_row is None or "title" not in mapping:
        report["error"] = (
            "未能识别表头（要求至少含「标题」与另一个指标列）。"
            "请确认导出的是头条后台「数据 → 作品数据」表格，"
            "或改用规范 CSV/JSON 通道（date,index 形式）。")
        report["ok"] = False
        return {"records": [], "report": report}

    headers = rows[header_row]
    report["mapping"] = {f: headers[j] for f, j in sorted(mapping.items())}
    for j, h in enumerate(headers):
        f, known_ignored = map_header(h)
        if not str(h).strip():
            continue
        if f is None and not known_ignored:
            report["unmapped"].append(str(h).strip())

    if title_index is None:
        title_index = build_article_index(output_dir)

    records: list = []
    for r_i in range(header_row + 1, len(rows)):
        row = rows[r_i]

        def cell(field, _row=row):
            j = mapping.get(field)
            return _row[j] if (j is not None and j < len(_row)) else ""

        raw_title = str(cell("title") or "").strip()
        if not raw_title:
            continue

        ctype = str(cell("content_type") or "").strip()
        if ctype and not any(t in ctype for t in ARTICLE_TYPES):
            report["non_article"].append(
                {"row": r_i + 1, "title": raw_title, "type": ctype})
            continue

        date_hint = parse_date_like(cell("date"))
        cands, reason = match_article(raw_title, title_index, date_hint)
        if reason != "matched":
            report[reason].append({
                "row": r_i + 1, "title": raw_title, "date": date_hint,
                "candidates": [f"{c['date']}/{c['slug']}" for c in cands],
            })
            continue

        c = cands[0]
        rec = {
            "date": c["date"], "slug": c["slug"], "index": c["index"],
            "title": c["title"], "source_title": raw_title, "__lineno": r_i + 1,
        }
        _fill_metrics(rec, cell)
        records.append(rec)
        report["matched"].append({
            "row": r_i + 1, "title": raw_title,
            "target": f"{c['date']}/{c['slug']}",
        })

    report["ok"] = True
    return {"records": records, "report": report}


def selftest() -> int:
    """最小自检：列名映射的两个易错点（最长匹配）与数值清洗。"""
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    check(map_header("标题")[0] == "title", "标题应映射到 title")
    check(map_header("展现量")[0] == "impressions", "展现量应映射到 impressions")
    check(map_header("粉丝阅读量")[0] == "follower_reads",
          "粉丝阅读量必须映射到 follower_reads（不能被「阅读量」吃掉）")
    check(map_header("粉丝展现量")[1] is True,
          "粉丝展现量应被识别为「已知但忽略」，不能落到 impressions")
    check(map_header("阅读完成率")[0] == "retention_rate",
          "阅读完成率应映射到 retention_rate")
    check(map_header("阅读量(次)")[0] == "reads", "去括号单位后应映射到 reads")
    check(map_header("某某乱列")[0] is None, "未知列应返回 None")
    check(clean_number("1.8万") == 18000.0, "1.8万 应换算为 18000")
    check(clean_number("--") is None, "-- 应视为缺数据（None，不冒充 0）")
    check(clean_number("4,120") == 4120.0, "千分位应被剥离")
    check(normalize_retention("41.2%") == 0.412, "41.2% 应换算为 0.412")
    check(normalize_retention("0.412") == 0.412, "0.412 应保持")
    check(normalize_title("森林1-3崩盘！") == normalize_title("森林13崩盘"),
          "标点差异不应影响标题匹配")

    for f in fails:
        print(f"  ❌ {f}")
    print(f"toutiao_import 自检：{'通过' if not fails else str(len(fails)) + ' 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
