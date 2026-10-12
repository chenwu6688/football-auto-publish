"""头条后台导出适配层单测（计划第十章）。

覆盖：中文列名最长匹配、忽略列遮挡、数值清洗、表头定位、
无依赖 xlsx 解析、标题匹配（命中/未匹配/歧义/非图文）、未接入不冒充。

纪律：全部在临时目录内构造合成数据，**不读也不写真实 output/**。
"""

import json
import sys
import tempfile
import unittest
import zipfile
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import toutiao_import as ti  # noqa: E402


def _write_min_xlsx(path: Path, rows: list) -> None:
    """最小合法 xlsx（sharedStrings + sheet1），仅用于验解析路径。"""
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
        body.append(f'<row r="{r}">' + cells + "</row>")
    sst = ('<?xml version="1.0" encoding="UTF-8"?><sst ' + ns +
           f' count="{len(strings)}" uniqueCount="{len(strings)}">' +
           "".join("<si><t xml:space=\"preserve\">%s</t></si>" % escape(s)
                   for s in strings) + "</sst>")
    ws = ('<?xml version="1.0" encoding="UTF-8"?><worksheet ' + ns +
          "><sheetData>" + "".join(body) + "</sheetData></worksheet>")
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
          "</sheets></workbook>")
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


ROWS = [
    ["头条号 · 作品数据导出"],
    [""],
    ["标题", "内容类型", "发布时间", "展现量", "阅读量", "粉丝阅读量", "粉丝展现量",
     "阅读完成率", "点赞量", "评论量", "分享量", "涨粉数", "点击率"],
    ["A队三球逆转，老六怎么看？", "图文", "2026-10-12 08:05", "18.2万", "5,120",
     "218", "16.1万", "41.2%", "88", "26", "19", "12", "2.8%"],
    ["B队崩盘，教练该不该下课", "图文", "2026-10-12 12:05", "9.6万", "3,044",
     "163", "8.4万", "38.7%", "51", "14", "11", "7", "3.2%"],
    ["随手一条微头条", "微头条", "2026-10-12 14:20", "2.1万", "900", "30", "1.9万",
     "20%", "12", "3", "1", "0", "4%"],
    ["一篇没收录过的旧文", "图文", "2026-09-01 10:00", "1万", "100", "5", "0.9万",
     "30%", "1", "0", "0", "0", "1%"],
]


def _synth_output_dir(tmp: Path) -> Path:
    d = tmp / "2026-10-12"
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps({
        "date": "2026-10-12",
        "articles": [
            {"index": 2, "slug": "a-dui-abc123", "title": "A队三球逆转，老六怎么看？"},
            {"index": 2, "slug": "b-dui-def456", "title": "B队崩盘，教练该不该下课"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp


class TestHeaderMapping(unittest.TestCase):
    def test_chinese_aliases(self):
        for h, want in (("标题", "title"), ("发布时间", "date"), ("展现量", "impressions"),
                        ("阅读量", "reads"), ("阅读完成率", "retention_rate"),
                        ("完读率", "retention_rate"), ("点赞量", "likes"),
                        ("评论量", "comments"), ("分享量", "shares"),
                        ("涨粉数", "new_followers"), ("内容类型", "content_type")):
            self.assertEqual(ti.map_header(h)[0], want, h)

    def test_bracket_unit_stripped(self):
        self.assertEqual(ti.map_header("阅读量(次)")[0], "reads")
        self.assertEqual(ti.map_header("展现量（次）")[0], "impressions")

    def test_longest_match_wins(self):
        # 若不做最长匹配，「粉丝阅读量」会被「阅读量」吃掉
        self.assertEqual(ti.map_header("粉丝阅读量")[0], "follower_reads")
        self.assertEqual(ti.map_header("粉丝阅读量")[0] != "reads", True)

    def test_ignored_columns_shadow(self):
        for h in ("粉丝展现量", "点击率", "平均阅读时长", "收藏量", "收益"):
            f, ignored = ti.map_header(h)
            self.assertIsNone(f, f"{h} 不该映射到字段")
            self.assertTrue(ignored, f"{h} 应被识别为已知忽略列")

    def test_unknown_column(self):
        f, ignored = ti.map_header("某某乱列")
        self.assertIsNone(f)
        self.assertFalse(ignored)


class TestNumberCleaning(unittest.TestCase):
    def test_units(self):
        self.assertEqual(ti.clean_number("1.8万"), 18000.0)
        self.assertEqual(ti.clean_number("2.1亿"), 210000000.0)
        self.assertEqual(ti.clean_number("3千"), 3000.0)

    def test_thousand_separator(self):
        self.assertEqual(ti.clean_number("5,120"), 5120.0)

    def test_placeholders_are_none_not_zero(self):
        for blank in ("", "   ", "--", "—", "无", "N/A", None):
            self.assertIsNone(ti.clean_number(blank), repr(blank))

    def test_retention_percent(self):
        self.assertEqual(ti.normalize_retention("41.2%"), 0.412)
        self.assertEqual(ti.normalize_retention("41.2"), 0.412)
        self.assertEqual(ti.normalize_retention("0.387"), 0.387)
        self.assertIsNone(ti.normalize_retention("--"))

    def test_retention_negative_placeholder_is_none(self):
        """头条用 -1 / -1.00 表示「无完读率数据」，必须判缺数据，不能折算成 0%。"""
        for raw in ("-1", "-1.00", "-1.0", -1.0):
            self.assertIsNone(ti.normalize_retention(raw), repr(raw))
        # 真实的 0% 完读率（有人看但没读完）仍是 0.0，两者不可混淆
        self.assertEqual(ti.normalize_retention("0.00"), 0.0)
        self.assertEqual(ti.normalize_retention("0"), 0.0)

    def test_int_metric_negative_placeholder_is_none(self):
        """计数类指标（阅读量/粉丝阅读量/展现量…）同样用 -1 表示「无数据」→ None，不冒充 0。
        （实测 2026-10-12 导出：801-869 段有 69 行 阅读量/粉丝阅读量 = -1）"""
        rec = {}
        ti._fill_metrics(rec, lambda f: "-1")
        self.assertIsNone(rec["reads"])
        self.assertIsNone(rec["follower_reads"])
        self.assertIsNone(rec["impressions"])
        # 真实的 0（有人看但没互动）仍保留为 0，两者不可混淆
        rec2 = {}
        ti._fill_metrics(rec2, lambda f: "0")
        self.assertEqual(rec2["reads"], 0)

    def test_parse_date_like(self):
        self.assertEqual(ti.parse_date_like("2026-10-12 08:05"), "2026-10-12")
        self.assertEqual(ti.parse_date_like("2026/10/3"), "2026-10-03")
        self.assertIsNone(ti.parse_date_like("今天"))
        self.assertIsNone(ti.parse_date_like(""))


class TestReadTable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_csv(self):
        p = self.tmp / "a.csv"
        p.write_text("标题,阅读量\n甲,100\n", encoding="utf-8")
        self.assertEqual(ti.read_table(p)[1], ["甲", "100"])

    def test_tsv_autodetect(self):
        p = self.tmp / "a.txt"
        p.write_text("标题\t阅读量\n甲\t100\n", encoding="utf-8")
        self.assertEqual(ti.read_table(p)[0], ["标题", "阅读量"])

    def test_json_object_array(self):
        p = self.tmp / "a.json"
        p.write_text(json.dumps([{"标题": "甲", "阅读量": 100}]), encoding="utf-8")
        table = ti.read_table(p)
        self.assertEqual(table[0], ["标题", "阅读量"])
        self.assertEqual(table[1], ["甲", "100"])

    def test_xlsx_without_openpyxl(self):
        p = self.tmp / "a.xlsx"
        _write_min_xlsx(p, ROWS)
        table = ti.read_table(p)
        self.assertGreaterEqual(len(table), 5)
        idx, mapping = ti.detect_header(ti.pad_rows(table))
        self.assertEqual(idx, 2)
        self.assertEqual(mapping.get("title"), 0)

    def test_xls_raises_with_hint(self):
        p = self.tmp / "a.xls"
        p.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            ti.read_table(p)
        self.assertIn("xlsx", str(cm.exception))


class TestDetectHeader(unittest.TestCase):
    def test_header_not_first_row(self):
        idx, mapping = ti.detect_header(ti.pad_rows(ROWS))
        self.assertEqual(idx, 2)
        self.assertIn("title", mapping)
        self.assertIn("reads", mapping)

    def test_no_header_found(self):
        idx, mapping = ti.detect_header([["甲", "乙"], ["1", "2"]])
        self.assertIsNone(idx)
        self.assertEqual(mapping, {})


class TestAdaptRows(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.outdir = _synth_output_dir(self.tmp)

    def test_matched_records(self):
        res = ti.adapt_rows(ROWS, output_dir=self.outdir)
        got = {r["slug"]: r for r in res["records"]}
        self.assertEqual(set(got), {"a-dui-abc123", "b-dui-def456"})
        a = got["a-dui-abc123"]
        self.assertEqual(a["date"], "2026-10-12")
        self.assertEqual(a["reads"], 5120)
        self.assertEqual(a["impressions"], 182000)
        self.assertEqual(a["follower_reads"], 218)
        self.assertAlmostEqual(a["retention_rate"], 0.412)
        self.assertEqual(a["new_followers"], 12)

    def test_unmatched_and_non_article_not_written(self):
        res = ti.adapt_rows(ROWS, output_dir=self.outdir)
        rep = res["report"]
        self.assertEqual(len(rep["unmatched"]), 1)
        self.assertEqual(len(rep["non_article"]), 1)
        titles = {r["source_title"] for r in res["records"]}
        self.assertNotIn("一篇没收录过的旧文", titles)
        self.assertNotIn("随手一条微头条", titles)

    def test_ambiguous_not_written(self):
        idx = {ti.normalize_title("同名"): [
            {"date": "2026-10-12", "slug": "s1", "index": 1, "title": "同名"},
            {"date": "2026-10-13", "slug": "s2", "index": 1, "title": "同名"}]}
        res = ti.adapt_rows([["标题", "阅读量"], ["同名", "9"]], title_index=idx)
        self.assertEqual(res["records"], [])
        self.assertEqual(len(res["report"]["ambiguous"]), 1)

    def test_date_hint_disambiguates(self):
        idx = {ti.normalize_title("同名"): [
            {"date": "2026-10-12", "slug": "s1", "index": 1, "title": "同名"},
            {"date": "2026-10-13", "slug": "s2", "index": 1, "title": "同名"}]}
        res = ti.adapt_rows([["标题", "发布时间", "阅读量"],
                             ["同名", "2026-10-13 09:00", "9"]], title_index=idx)
        self.assertEqual([r["slug"] for r in res["records"]], ["s2"])

    def test_no_header_no_records(self):
        res = ti.adapt_rows([["甲", "乙"], ["1", "2"]], output_dir=self.outdir)
        self.assertFalse(res["report"]["ok"])
        self.assertEqual(res["records"], [])
        self.assertTrue(res["report"]["error"])

    def test_unmapped_columns_reported(self):
        rows = [r[:] for r in ROWS]
        rows[2] = rows[2] + ["新指标"]
        for r in rows[3:]:
            r.append("42")
        rep = ti.adapt_rows(rows, output_dir=self.outdir)["report"]
        self.assertEqual(rep["unmapped"], ["新指标"])

    def test_missing_metrics_default_zero_not_fabricated(self):
        # 导出里没有粉丝阅读量列 → 该字段为 0（我们的 schema 用 0 表示未提供），
        # 但**不会**凭空造出别的值；有列但单元格为「--」也判 0。
        res = ti.adapt_rows([["标题", "阅读量"], ["A队三球逆转，老六怎么看？", "100"]],
                            output_dir=self.outdir)
        rec = res["records"][0]
        self.assertEqual(rec["reads"], 100)
        self.assertEqual(rec["follower_reads"], 0)
        self.assertEqual(rec["impressions"], 0)


class TestSelftest(unittest.TestCase):
    def test_selftest_passes(self):
        self.assertEqual(ti.selftest(), 0)

    def test_normalize_title_ignores_punctuation(self):
        self.assertEqual(ti.normalize_title("森林1-3崩盘！"), ti.normalize_title("森林13崩盘"))
        self.assertEqual(ti.normalize_title("A队 vs B队"), ti.normalize_title("A队VSB队"))


if __name__ == "__main__":
    unittest.main()
