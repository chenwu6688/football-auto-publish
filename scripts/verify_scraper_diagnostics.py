#!/usr/bin/env python3
"""采集失败分类验收（计划九 · 降级纪律）。

## 防的是什么

「返回空」这种接口设计会把四种完全不同的情况抹平成同一个 `[]`：

| 真实情况 | 调用方看到的 |
|---|---|
| 今天确实没新闻 | 0 篇 |
| 被 EdgeOne 挑战页拦了（HTTP 567） | 0 篇 |
| 网络超时 / DNS 失败 | 0 篇 |
| 改版把选择器打穿了 | 0 篇 |

2026-10-26 就是这样翻车的：懂球帝返回 567 挑战页，567 不在 403/429/404 分支里，
重试耗尽后 `return ""` → 备源静默归零，日志只报「懂球帝文章: 0 篇」。
查了半天以为是懂球帝没内容。

## 硬项（不达标退出码 1）

A. **567/5xx 必须抛异常**，不得 `return ""`；且抛出的类型要能区分 challenge / blocked
B. **404 归 empty 而不是 blocked**——页面真没了和被拦是两件事
C. **200 挑战页壳子必须识别**（薄壳 + 厂商特征串），且长文里出现「captcha」不许误判
D. **负向验证**：台账记录被篡改（kind 改成未知值 / 缺字段）时，校验器必须报错

## 告警项

E. 真实台账 `data/audit/scraper_fetch_latest.json` 里的「需人工介入来源」清单

用法：
  python3 scripts/verify_scraper_diagnostics.py [--quiet]
产出：data/audit/scraper_diagnostics_<date>.md + scraper_diagnostics_latest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import media_scraper as ms       # noqa: E402
import scraper_diagnostics as diag  # noqa: E402

CST = timezone(timedelta(hours=8))
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"


def _resp(code, text="", headers=None):
    return types.SimpleNamespace(status_code=code, text=text, headers=headers or {},
                                 encoding="utf-8", apparent_encoding="utf-8")


def _scraper():
    s = ms.SportsScraper()
    s.MAX_RETRIES = 1
    s.REQUEST_DELAY = 0
    return s


def check_status_raises() -> dict:
    """硬项 A：567/5xx 必须抛，且类型可区分。"""
    details, ok = [], True
    cases = [(567, ms.ScraperChallengeError, "EdgeOne 挑战"),
             (521, ms.ScraperChallengeError, "Cloudflare 挑战"),
             (403, ms.ScraperBlockedError, "403 封禁"),
             (503, Exception, "5xx 服务异常")]
    for code, want, label in cases:
        diag.reset()
        s = _scraper()
        s.session = mock.Mock()
        s.session.get.return_value = _resp(code, "blocked")
        got, raised = None, False
        try:
            got = s._http_get("https://www.dongqiudi.com/")
        except BaseException as e:                    # noqa: BLE001
            raised = True
            if not isinstance(e, want):
                ok = False
                details.append(f"{label}（{code}）抛了 {type(e).__name__}，期望 {want.__name__} ❌")
                continue
        if not raised:
            ok = False
            details.append(f"{label}（{code}）**没有抛异常**，返回 {got!r} —— 又回到静默失效 ❌")
            continue
        details.append(f"{label}（{code}）→ 抛出 {want.__name__} ✅")
    diag.reset()
    return {"item": "非 200/404 必须抛出而非静默返空", "ok": ok,
            "advisory": False, "detail": details}


def check_empty_vs_blocked() -> dict:
    """硬项 B：404 归 empty，403/567 归 blocked/challenge，三者互不冒充。"""
    details, ok = [], True
    diag.reset()
    s = _scraper()
    s.session = mock.Mock()
    s.session.get.return_value = _resp(404)
    out = s._http_get("https://www.zhibo8.cc/gone.htm")
    rec = diag.records()[-1]
    if out != "" or rec["kind"] != diag.KIND_EMPTY:
        ok = False
        details.append(f"404 → 返回值 {out!r}、类型 {rec['kind']}，期望 ''/empty ❌")
    else:
        details.append("404 → 返回空串 + kind=empty（页面真没了）✅")

    diag.reset()
    s = _scraper()
    s.session = mock.Mock()
    s.session.get.return_value = _resp(403, "forbidden")
    try:
        s._http_get("https://www.zhibo8.cc/")
    except BaseException:                             # noqa: BLE001
        pass
    kinds = {r["kind"] for r in diag.records()}
    if diag.KIND_BLOCKED not in kinds or diag.KIND_EMPTY in kinds:
        ok = False
        details.append(f"403 → 记录了 {kinds}，期望只含 blocked ❌")
    else:
        details.append("403 → kind=blocked（不与 empty 混记）✅")

    # empty 单独出现时不得被判成失败
    diag.reset()
    diag.record("zhibo8", "u", diag.KIND_EMPTY, status=404)
    if not diag.summary()["ok"]:
        ok = False
        details.append("只有 empty 时 summary 却报失败 ❌（真空数据不是故障）")
    else:
        details.append("只有 empty 时不报警（真空数据不是故障）✅")
    diag.reset()
    return {"item": "真空数据 / 被拦 / 挑战页三者可区分", "ok": ok,
            "advisory": False, "detail": details}


def check_challenge_shell() -> dict:
    """硬项 C：200 挑战页要认出来，长文误伤要挡住。"""
    details, ok = [], True
    thin = "<html><title>EdgeOne</title>Just a moment</html>"
    if not diag.looks_like_challenge(thin):
        ok = False
        details.append("薄壳 + EdgeOne 特征未识别 ❌")
    else:
        details.append("薄壳 + EdgeOne 特征 → challenge ✅")

    long_article = ("皇马 2-1 巴萨，" * 8000) + "captcha"
    if diag.looks_like_challenge(long_article):
        ok = False
        details.append("长文里出现 captcha 被误判成挑战页 ❌（会白丢正文）")
    else:
        details.append(f"长文含 captcha（{len(long_article)} 字符）不误判 ✅")

    # 端到端：200 + 挑战壳必须抛
    diag.reset()
    s = _scraper()
    s.session = mock.Mock()
    s.session.get.return_value = _resp(200, thin)
    try:
        s._http_get("https://www.dongqiudi.com/")
        ok = False
        details.append("200 挑战壳未抛异常 ❌")
    except ms.ScraperChallengeError:
        details.append("200 挑战壳 → ScraperChallengeError ✅")
    diag.reset()
    return {"item": "200 挑战页壳子可识别且不误伤长文", "ok": ok,
            "advisory": False, "detail": details}


def check_ledger_integrity(rep: dict) -> tuple[bool, list[str]]:
    """台账完整性校验（供硬项 D 的负向验证调用，也可被下游复用）。"""
    problems = []
    if not isinstance(rep, dict):
        return False, ["台账不是对象"]
    recs = rep.get("records")
    if not isinstance(recs, list) or not recs:
        return False, ["台账无 records 或不是数组"]
    for i, r in enumerate(recs):
        if not isinstance(r, dict):
            problems.append(f"第 {i} 条不是对象")
            continue
        if r.get("kind") not in diag.KINDS:
            problems.append(f"第 {i} 条 kind 非法: {r.get('kind')!r}")
        if "status" not in r:
            problems.append(f"第 {i} 条缺 status 字段")
        if not r.get("source"):
            problems.append(f"第 {i} 条缺 source")
    return (not problems), problems


def check_negative() -> dict:
    """硬项 D：篡改台账必须被校验器抓到。"""
    details, ok = [], True
    diag.reset()
    diag.record("dongqiudi", "u", diag.KIND_CHALLENGE, status=567)
    tmp = Path(tempfile.mkdtemp()) / "ledger.json"
    diag.LEDGER_PATH = tmp
    p = diag.flush()
    if not p or not p.exists():
        return {"item": "负向验证：篡改台账必须报错", "ok": False, "advisory": False,
                "detail": ["台账根本没落盘，负向验证无法进行 ❌"]}
    good = json.loads(p.read_text(encoding="utf-8"))
    passed, problems = check_ledger_integrity(good)
    if not passed:
        ok = False
        details.append(f"未篡改的正样本就报错: {problems} ❌")
    else:
        details.append("正样本通过校验 ✅")

    bad = json.loads(json.dumps(good))
    bad["records"][0]["kind"] = "totally-made-up"
    passed, problems = check_ledger_integrity(bad)
    if passed:
        ok = False
        details.append("kind 被改成未知值，校验器仍通过 ❌（负向验证失效）")
    else:
        details.append(f"kind 篡改 → 报错：{problems[0]} ✅")

    bad2 = json.loads(json.dumps(good))
    del bad2["records"][0]["status"]
    passed, _ = check_ledger_integrity(bad2)
    if passed:
        ok = False
        details.append("删掉 status 后校验器仍通过 ❌")
    else:
        details.append("status 缺失 → 报错 ✅")
    diag.reset()
    return {"item": "负向验证：篡改台账必须报错", "ok": ok,
            "advisory": False, "detail": details}


def check_real_ledger() -> dict:
    """告警项 E：真实台账里的「需人工介入」来源。"""
    p = AUDIT_DIR / "scraper_fetch_latest.json"
    if not p.exists():
        return {"item": "真实采集台账（告警项）", "ok": True, "advisory": True,
                "detail": ["尚无 data/audit/scraper_fetch_latest.json（下次批次运行后生成）"]}
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"item": "真实采集台账（告警项）", "ok": False, "advisory": True,
                "detail": [f"台账读取失败: {e}"]}
    s = rep.get("summary") or {}
    att = s.get("attention_sources") or {}
    detail = [f"生成时间 {rep.get('generated_at', '')}｜记录 {s.get('total', 0)} 条"]
    if att:
        detail += [f"需人工介入：{src} → {kind}" for src, kind in att.items()]
    else:
        detail.append("无异常来源 ✅")
    return {"item": "真实采集台账（告警项）", "ok": not att, "advisory": True,
            "detail": detail}


def verify() -> dict:
    hard = [check_status_raises(), check_empty_vs_blocked(),
            check_challenge_shell(), check_negative()]
    advisory = [check_real_ledger()]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": hard + advisory,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 采集失败分类验收报告", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "（告警项）" if c.get("advisory") else "（硬项）"
        L.append(f"## {c['item']}{tag}：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="采集失败分类验收")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"scraper_diagnostics_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "scraper_diagnostics_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
