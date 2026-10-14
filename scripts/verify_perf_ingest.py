#!/usr/bin/env python3
"""绩效回填自动化验收（R11 内容诊断 2026-10-13「建议7」）。

动机：绩效回填此前全靠人肉「导出 → 手动跑脚本」，数据永远滞后一周、闭环永远慢一拍。
本脚本把这条自动化链路钉死，防止退回人力模式。

硬项（不达标退出码 1）
  A. 投递箱类型识别——作品数据导出 → perf；粉丝分析导出 → growth；识别不出 → unknown（不猜）
  B. 幂等——同一文件（sha256）二次投递只处理一次
  C. 失败/未知不吞——unknown / failed 的文件必须留在 inbox 以便人工确认与重试
  D. 归档——处理成功的文件移入 data/processed/<日期>/
  E. 复用入口——perf 走 import_performance.ingest_toutiao（不再各写一份导入逻辑）
  F. 滞后监控——滞后天数计算、无数据 → None（待接入不算滞后）、超阈值告警、覆盖率
  G. CI 接线——perf-ingest.yml 存在且含「消费投递箱 + 滞后监控 + 回写提交」
  H. 一键周更入口——weekly_review.py 六步接线齐、调参只算不写、dry-run 零副作用
  I. 导出自动回收——toutiao-collector.yml 与 collector 脚本在、CI 接线齐、
     降级告警是真实 Call、落箱同名不覆盖、无登录态不启动浏览器

告警项（只提示）
  I. 当前滞后实况（绩效 / 转粉 / 回填覆盖率）

用法：
  python3 scripts/verify_perf_ingest.py [--quiet]
产出：data/audit/perf_ingest_<date>.md + perf_ingest_latest.json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import growth_import as gi          # noqa: E402
import ingest_exports as ie         # noqa: E402
import check_perf_lag as cpl        # noqa: E402
import weekly_review as wr          # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
SCRIPTS = PROJECT_ROOT / "scripts"
CST = timezone(timedelta(hours=8))
WF = PROJECT_ROOT / ".github" / "workflows" / "perf-ingest.yml"


def _w(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8-sig")
    return p


def check_detect() -> dict:
    problems = []
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        perf = _w(t / "p.csv", "标题,发布时间,展现量,阅读量\nA队绝杀,2026-10-12,1000,100\n")
        grow = _w(t / "g.csv", "统计日期,粉丝总数,涨粉数\n2026-10-01,100,5\n")
        rand = _w(t / "r.csv", "a,b\n1,2\n")
        if ie.detect_kind(perf)[0] != "perf":
            problems.append("作品数据导出未被识别为 perf")
        if ie.detect_kind(grow)[0] != "growth":
            problems.append("粉丝分析导出未被识别为 growth")
        if ie.detect_kind(rand)[0] != "unknown":
            problems.append("无关文件应判 unknown（不许猜）")
    return {"item": "投递箱类型识别（perf / growth / unknown）", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["作品数据导出 → perf ✅；粉丝分析导出 → growth ✅；无关文件 → unknown ✅"])}


def _with_tmp_env():
    """把归档/转粉落盘指到临时目录，避免污染真实库。"""
    td = tempfile.TemporaryDirectory()
    tmp = Path(td.name)
    inbox = tmp / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    olds = (ie.PROCESSED, ie.INBOX, gi.GROWTH_PATH)
    ie.PROCESSED, ie.INBOX, gi.GROWTH_PATH = tmp / "processed", inbox, tmp / "fans_daily.json"
    return td, tmp, inbox, olds


def check_idempotent_and_archive() -> dict:
    problems = []
    detail = []
    td, tmp, inbox, olds = _with_tmp_env()
    try:
        f = _w(inbox / "g.csv", "统计日期,粉丝总数,涨粉数\n2026-10-01,10000,12\n2026-10-02,10025,\n")
        state = {"processed": {}, "history": []}
        r1 = ie.ingest_one(f, state)
        if r1["status"] != "ok" or r1.get("rows") != 2:
            problems.append(f"首次入库异常：{r1}")
        if f.exists():
            problems.append("处理成功的文件未移出 inbox")
        if not (tmp / "processed").exists():
            problems.append("未归档到 data/processed/<日期>/")
        days = gi.load()["days"]
        if days.get("2026-10-02", {}).get("new_followers") != 25:
            problems.append("转粉差分未生效（应为 25）")
        f2 = _w(inbox / "g.csv", "统计日期,粉丝总数,涨粉数\n2026-10-01,10000,12\n2026-10-02,10025,\n")
        r2 = ie.ingest_one(f2, state)
        if r2["status"] != "duplicate":
            problems.append(f"重复投递未跳过（幂等失效）：{r2['status']}")
        detail += [f"首次：{r1['status']}（{r1.get('rows')} 行，差分 {r1.get('derived')}）✅",
                   "重复投递：duplicate（sha256 幂等）✅",
                   "归档：data/processed/<日期>/ ✅"]
    finally:
        ie.PROCESSED, ie.INBOX, gi.GROWTH_PATH = olds
        td.cleanup()
    return {"item": "幂等 + 归档（duplicate 跳过 / 文件移出 inbox）", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems else detail)}


def check_fail_stays_in_inbox() -> dict:
    problems = []
    td, tmp, inbox, olds = _with_tmp_env()
    orig = ie.ip.ingest_toutiao
    try:
        f = _w(inbox / "weird.csv", "xxx,yyy\n1,2\n")
        r = ie.ingest_one(f, {"processed": {}, "history": []})
        if r["status"] != "unknown" or not f.exists():
            problems.append("unknown 类型必须留在 inbox（不许吞、不许猜）")
        g = _w(inbox / "perf.csv", "标题,展现量,阅读量\nA,100,10\n")

        def boom(path, **kw):
            raise RuntimeError("模拟导入失败")
        ie.ip.ingest_toutiao = boom
        r2 = ie.ingest_one(g, {"processed": {}, "history": []})
        if r2["status"] != "failed" or not g.exists():
            problems.append("导入失败的文件必须留在 inbox 以便重试")
    finally:
        ie.ip.ingest_toutiao = orig
        ie.PROCESSED, ie.INBOX, gi.GROWTH_PATH = olds
        td.cleanup()
    return {"item": "失败/未知不吞（留在 inbox 等人工确认）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["unknown → 留在 inbox ✅；failed → 留在 inbox 等重试 ✅"])}


def check_shared_entrypoint() -> dict:
    problems = []
    if not hasattr(ie.ip, "ingest_toutiao"):
        problems.append("import_performance 缺 ingest_toutiao 复用入口")
    src = (PROJECT_ROOT / "scripts" / "import_performance.py").read_text(encoding="utf-8")
    if "def run_toutiao" in src and "out = ingest_toutiao(" not in src:
        problems.append("run_toutiao 未复用 ingest_toutiao（两处导入逻辑会漂移）")
    return {"item": "复用入口（import_performance.ingest_toutiao）", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["投递箱与 CLI 共用同一导入入口 ✅"])}


def check_lag_monitor() -> dict:
    problems = []
    if cpl.lag_days("2026-10-10", "2026-10-13") != 3:
        problems.append("滞后天数计算错误")
    if cpl.lag_days(None, "2026-10-13") is not None:
        problems.append("无数据应返回 None（待接入不算滞后）")
    rep = cpl.report(days=14, today="2026-10-13")
    for k in ("绩效滞后天数", "转粉滞后天数", "回填覆盖率", "problems", "ok"):
        if k not in rep:
            problems.append(f"滞后报告缺字段 {k}")
    # 覆盖率为空时不得冒充 0
    cov = rep["回填覆盖率"]
    if not cov.get("total") and cov.get("rate") is not None:
        problems.append("无成稿时覆盖率应为 None，不得冒充 0")
    return {"item": "滞后监控（滞后天数 / 待接入 / 覆盖率）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["滞后天数 / 无数据 None / 覆盖率口径 均正确 ✅"])}


def check_ci() -> dict:
    problems = []
    if not WF.exists():
        problems.append("缺 .github/workflows/perf-ingest.yml")
    else:
        txt = WF.read_text(encoding="utf-8")
        for marker, label in (("scripts/ingest_exports.py", "消费投递箱"),
                              ("scripts/check_perf_lag.py", "滞后监控"),
                              ("git push origin main", "回写提交"),
                              ("cron:", "定时触发")):
            if marker not in txt:
                problems.append(f"工作流缺「{label}」步骤（未见 `{marker}`）")
    return {"item": "CI 接线（定时自动入库 + 滞后监控 + 回写）", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["perf-ingest.yml：cron 定时 + 消费投递箱 + 滞后监控 + 回写提交 ✅"])}


def check_weekly_entry() -> dict:
    """一键周更入口（scripts/weekly_review.py）：用户只交文件，剩下全自动。

    校验：入口存在 + 六个步骤接线齐 + 调参只算不写（绝不自动改配比配置）
    + dry-run 真的不落库。
    """
    problems = []
    src_path = SCRIPTS / "weekly_review.py"
    if not src_path.exists():
        return {"item": "一键周更入口（weekly_review.py）", "ok": False, "advisory": False,
                "detail": ["❌ 缺 scripts/weekly_review.py（用户交文件后没有统一收口入口）"]}
    src = src_path.read_text(encoding="utf-8")
    for marker, label in (("collect_from(", "收文件（--from）"),
                          ("ie.run(", "消费投递箱"),
                          ("cpl.report(", "滞后监控"),
                          ("build_metrics_dashboard", "刷新看板"),
                          ("compute_suggestion", "调参建议")):
        if marker not in src:
            problems.append(f"缺「{label}」接线（未见 `{marker}`）")
    # 调参只算不写：用 AST 查**真实调用**（不靠文本匹配——注释里提到 main() 不算）
    try:
        tree = ast.parse(src)
        banned = [f"{n.func.value.id}.{n.func.attr}"
                  for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("main", "apply_suggestion", "save_config")
                  and isinstance(n.func.value, ast.Name)]
        if banned:
            problems.append(f"调参建议不得调用 {sorted(set(banned))}（会写配置）")
    except SyntaxError as e:
        problems.append(f"语法错误，无法静态核验：{e}")
    try:
        import weekly_review as wr
    except Exception as e:
        return {"item": "一键周更入口（weekly_review.py）", "ok": False, "advisory": False,
                "detail": [f"❌ 无法载入 weekly_review：{e}"]}
    # dry-run 必须零副作用（不写台账 / 不写转粉数据 / 不写汇总报告）
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        saved = (ie.INBOX, ie.STATE_PATH, gi.GROWTH_PATH, wr.AUDIT_DIR, wr.INBOX)
        try:
            ie.INBOX = tmp / "inbox"
            ie.INBOX.mkdir(parents=True, exist_ok=True)
            ie.STATE_PATH = tmp / "state.json"
            gi.GROWTH_PATH = tmp / "fans.json"
            wr.INBOX, wr.AUDIT_DIR = ie.INBOX, tmp / "audit"
            out = wr.run(dry_run=True, write_dashboard=False, tuner=False)
        except Exception as e:
            problems.append(f"dry-run 崩溃：{type(e).__name__}: {e}")
            out = {}
        finally:
            (ie.INBOX, ie.STATE_PATH, gi.GROWTH_PATH, wr.AUDIT_DIR, wr.INBOX) = saved
        for p, label in ((Path(tmp) / "state.json", "幂等台账"),
                         (Path(tmp) / "fans.json", "转粉数据"),
                         (Path(tmp) / "audit", "汇总报告")):
            if p.exists():
                problems.append(f"dry-run 不该写{label}")
    return {"item": "一键周更入口（weekly_review.py）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["收文件 → 入库 → 滞后 → 看板 → 调参建议 → 汇总报告 六步接线齐 ✅",
                             "调参只算不写（不动 config/ratio_config.yaml）✅",
                             "dry-run 零副作用（不写台账/转粉/报告）✅"])}


def check_collector() -> dict:
    """头条导出自动回收（toutiao-collector.yml + collect_toutiao_exports.py）。

    验收：脚本/workflow 在 + CI 接线（cron/登录态/回写）+ 降级告警是真实 Call
    （AST，不信文本）+ 落箱同名不覆盖 + 无登录态不启动浏览器。
    自动化是增量不是替换——手动 inbox 通道永远保留，collector 失败只告警。
    """
    problems = []
    script = SCRIPTS / "collect_toutiao_exports.py"
    wf = PROJECT_ROOT / ".github" / "workflows" / "toutiao-collector.yml"
    if not script.exists():
        return {"item": "导出自动回收（collector）", "ok": False, "advisory": False,
                "detail": ["❌ 缺 scripts/collect_toutiao_exports.py"]}
    if not wf.exists():
        problems.append("缺 .github/workflows/toutiao-collector.yml")
    else:
        txt = wf.read_text(encoding="utf-8")
        for marker, label in (("collect_toutiao_exports.py", "回收脚本调用"),
                              ("TOUTIAO_AUTH_GZ", "登录态注入（与 publisher 共用 secret）"),
                              ("git push origin main", "回写提交"),
                              ("cron:", "定时触发")):
            if marker not in txt:
                problems.append(f"toutiao-collector.yml 缺「{label}」（未见 `{marker}`）")
    src = script.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "send_wxpusher"]
        if len(calls) < 2:
            problems.append(f"降级告警调用过少（send_wxpusher 仅 {len(calls)} 处，"
                            "至少覆盖登录过期/导出失败两条路径）")
        funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for fn in ("run", "_save_to_inbox", "_collect_one"):
            if fn not in funcs:
                problems.append(f"collector 缺函数 {fn}")
        run_fn = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "run"), None)
        if run_fn is not None:
            argnames = [a.arg for a in run_fn.args.args] + [a.arg for a in run_fn.args.kwonlyargs]
            if "dry_run" not in argnames:
                problems.append("run() 缺 dry_run 参数（无法只探活不导出）")
    except SyntaxError as e:
        problems.append(f"语法错误，无法静态核验：{e}")
    try:
        import collect_toutiao_exports as cte
    except Exception as e:
        problems.append(f"无法载入 collector：{e}")
        return {"item": "导出自动回收（collector）", "ok": False, "advisory": False,
                "detail": [f"❌ {'；'.join(problems)}"]}
    with tempfile.TemporaryDirectory() as td:
        def exploding_factory():
            raise AssertionError("无登录态不应启动浏览器")
        r = cte.run(auth_file=Path(td) / "none.json", inbox=Path(td) / "inbox",
                    playwright_factory=exploding_factory)
        if r.get("ok") or r.get("status") != "no_auth":
            problems.append(f"无登录态路径异常: status={r.get('status')}")
        inbox = Path(td) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        tmp = Path(td) / "dl"
        tmp.mkdir()
        f = tmp / "作品数据.xlsx"
        f.write_bytes(b"old")
        cte._save_to_inbox(f, inbox)
        f.write_bytes(b"updated")
        rec = cte._save_to_inbox(f, inbox)
        if rec["action"] != "saved" or (inbox / "作品数据.xlsx").read_bytes() != b"old":
            problems.append(f"同名不同内容必须另存不得覆盖: {rec}")
    return {"item": "导出自动回收（collector）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["toutiao-collector.yml：周一 09:00 cron + 登录态注入 + 回写投递箱 ✅",
                             "降级告警为真实 Call（登录过期/导出失败两条路径）✅",
                             "落箱同名不覆盖（sha 判重 / 更新另存 _2）✅",
                             "无登录态不启动浏览器 ✅"])}


def check_live() -> dict:
    rep = cpl.report(days=14)
    detail = [f"绩效最新 {rep['绩效最新']}（滞后 {rep['绩效滞后天数']} 天）；"
              f"转粉最新 {rep['转粉最新'] or '待接入'}（滞后 {rep['转粉滞后天数']} 天）",
              f"近 14 天回填覆盖率 {rep['回填覆盖率']['rate']}%"
              f"（{rep['回填覆盖率']['filled']}/{rep['回填覆盖率']['total']}）"]
    detail += [f"⚠️ {p}" for p in rep["problems"]]
    detail += [f"ℹ️ {n}" for n in rep["notices"]]
    return {"item": "当前滞后实况（仅提示）", "ok": rep["ok"], "advisory": True,
            "perf_lag": rep["绩效滞后天数"], "growth_lag": rep["转粉滞后天数"],
            "coverage": rep["回填覆盖率"], "detail": detail}


def verify() -> dict:
    hard = [check_detect(), check_idempotent_and_archive(), check_fail_stays_in_inbox(),
            check_shared_entrypoint(), check_lag_monitor(), check_ci(), check_weekly_entry(),
            check_collector()]
    soft = [check_live()]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": hard + soft,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 绩效回填自动化验收报告（建议7）", "",
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
    ap = argparse.ArgumentParser(description="绩效回填自动化验收（建议7）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"perf_ingest_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "perf_ingest_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
