#!/usr/bin/env python3
"""头部账号对标抓取与基线计算（2026-11-01 建立）。

为什么要这个东西：
  2026-11-01 做对标时发现，我方 76 篇标题中位阅读 8，而头部「英超那些事」
  中位 5526——差距全在"标题有没有制造点开理由"。但那次结论是**手工抓的一次性分析**，
  一个月后没人记得数字怎么来的，也没法回答"竞对现在的打法变了吗"。
  本脚本把那次抓取固化成可复跑工具。

它做什么：
  1. 按 config/peer_accounts.yaml 逐个打开头条账号主页（playwright 渲染），
     抓「标题 + 阅读量 + 评论数 + 发布时间」——这些是主页公开显示的真实数字；
  2. 用 title_features 六维打标（与验收脚本同一套口径，禁止各自写正则）；
  3. 把我方同期成稿按同口径打标，做同表对比；
  4. 落 data/audit/benchmark_baseline_<date>.json + .md。

它不做什么：
  - 不做判定、不阻断发布。对标是**输入**，不是闸门；
  - 不进 CI 硬门禁：依赖外部站点 + playwright，不稳定，硬拦会变成常态假警；
  - 不推测粉丝增长、曝光量、完播——抓不到的东西一律不写。

退出码：0=正常产出 / 2=跳过（无 playwright 或全部账号失败）/ 1=产出但拿到 0 条。

用法：
  PYTHONPATH=. python3 scripts/benchmark_peers.py              # 全量
  PYTHONPATH=. python3 scripts/benchmark_peers.py --account 我爱英超 --scroll 3
  PYTHONPATH=. python3 scripts/benchmark_peers.py --reuse      # 复用上次落盘的原始文本，不联网
  PYTHONPATH=. python3 scripts/benchmark_peers.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import title_features  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CACHE_DIR = AUDIT_DIR / "benchmark_raw"
CONFIG_PATH = PROJECT_ROOT / "config" / "peer_accounts.yaml"
CST = timezone(timedelta(hours=8))

# 主页文本里的行形态（真实观察，2026-11-01）：
#   标题行
#   55阅读1评论3小时前      ← 图文
#   8阅读3小时前            ← 图文无评论
#   1.1万播放26评论前天18:56  ← 视频号（如「足球记忆FM」已全面转视频）
# 两种计量单位**不可混算**：阅读是图文读完，播放是视频起播，量级天然不同。
# 故逐条记 unit，汇总时按账号主单位出表，并在局限里写明。
RE_ROW = re.compile(r"^([\d.]+)(万)?(阅读|播放)(?:([\d.]+)(万)?评论)?(.+)$")


def _num(s: str, wan: bool) -> int:
    return int(float(s) * (10000 if wan else 1))


# ------------------------------------------------------------------ 解析
def parse_home_text(txt: str) -> list[dict]:
    """从账号主页渲染文本里抽作品行。抽不到就返回空列表，绝不编造。

    逐条带 `unit`（阅读/播放）。视频号的播放量不能和图文阅读量放在一个中位数里算，
    调用方需按单位分开汇总（见 `build_report`）。
    """
    lines = [x.strip() for x in (txt or "").split("\n") if x.strip()]
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = RE_ROW.match(line)
        if not m or i == 0:
            continue
        title = lines[i - 1]
        # 视频号在标题前还有一行时长（"03:06"），要跳过拿真正的标题
        if re.fullmatch(r"\d{1,2}:\d{2}", title) and i >= 2:
            title = lines[i - 2]
        if len(title) < 6 or RE_ROW.match(title):
            continue
        out.append({
            "title": title,
            "views": _num(m.group(1), bool(m.group(2))),
            "unit": m.group(3),
            "comments": _num(m.group(4), bool(m.group(5))) if m.group(4) else 0,
            "when": m.group(6),
        })
    # 同标题去重（主页切换 tab 时会出现重复行）
    seen, dedup = set(), []
    for r in out:
        k = r["title"][:26]
        if k in seen:
            continue
        seen.add(k)
        dedup.append(r)
    return dedup


def parse_followers(txt: str, account: str) -> int | None:
    """从主页文本里取粉丝数（"23.0万粉丝"）。取不到返回 None，不填 0。"""
    m = re.search(r"([\d.]+)\s*(万)?粉丝", txt or "")
    if not m:
        return None
    return _num(m.group(1), bool(m.group(2)))


# ------------------------------------------------------------------ 抓取
async def _fetch_one(pw_ctx, account: str, cfg: dict) -> tuple[str, str]:
    """返回 (主页文本, 落盘路径)；失败返回 ("", 错误说明)。"""
    import urllib.parse

    ua = (cfg.get("抓取") or {}).get("user_agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    rounds = int((cfg.get("抓取") or {}).get("scroll_rounds") or 6)
    wait = int((cfg.get("抓取") or {}).get("wait_ms") or 1200)
    page = await pw_ctx.new_page()
    try:
        url = "https://so.toutiao.com/search?keyword=%s&pd=user" % urllib.parse.quote(account)
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        loc = page.locator(f"text={account}").first
        if not await loc.count():
            return "", "搜索结果里找不到该账号名（可能已改名/被封）"
        await loc.click(timeout=6000)
        await page.wait_for_timeout(4500)
        for _ in range(rounds):
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(wait)
        return await page.inner_text("body"), page.url
    finally:
        await page.close()


async def _fetch_all(accounts: list[str], cfg: dict) -> dict[str, str]:
    from playwright.async_api import async_playwright

    ua = (cfg.get("抓取") or {}).get("user_agent")
    out: dict[str, str] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=ua, viewport={"width": 1440, "height": 1200}, locale="zh-CN")
        for acc in accounts:
            try:
                txt, meta = await _fetch_one(ctx, acc, cfg)
                if txt:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    (CACHE_DIR / f"{acc}.txt").write_text(txt, encoding="utf-8")
                    out[acc] = txt
                    print(f"  ✅ {acc}: {len(txt)} 字符 @ {meta[:60]}")
                else:
                    print(f"  ⚠️  {acc}: {meta}")
            except Exception as e:  # 单账号失败不影响其他账号
                print(f"  ❌ {acc}: {type(e).__name__} {str(e)[:60]}")
        await browser.close()
    return out


# ------------------------------------------------------------------ 我方基线
def load_our_articles(days: int = 30) -> list[dict]:
    """从 output/<date>/metadata.json + performance_log 取我方成稿与真实阅读。

    注意：只取**有真实阅读数据**的篇目。没有绩效的成稿不进分母——
    拿 0 冒充阅读会让"我方中位"虚低，属于口径造假。
    """
    perf_path = PROJECT_ROOT / "output" / "performance_log.json"
    if not perf_path.exists():
        return []
    perfs = json.loads(perf_path.read_text(encoding="utf-8")).get("articles") or {}
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    meta: dict[tuple, dict] = {}
    for d in sorted((PROJECT_ROOT / "output").glob("20*-*-*")):
        mp = d / "metadata.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in m.get("articles") or []:
            meta[(m.get("date"), a.get("slug"))] = a
    rows = []
    for _k, v in perfs.items():
        date = (v.get("date") or "")[:10]
        if not date or date < cutoff:
            continue
        a = meta.get((date, v.get("slug")), {})
        title = v.get("title") or a.get("title") or ""
        reads = v.get("reads")
        if not title or reads is None:
            continue
        rows.append({"title": title, "views": int(reads), "date": date,
                     "content_type": a.get("content_type")})
    return rows


# ------------------------------------------------------------------ 报表
def build_report(peers: dict[str, list[dict]], ours: list[dict], followers: dict) -> dict:
    rep: dict = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "peers": {},
        "ours": title_features.summarize(ours) if ours else {"n": 0},
        "caveats": [
            "主页显示的是当前累计阅读，我方后台是统计窗口内阅读，口径不完全对齐；倍数只代表方向",
            "抓取样本是账号主页滚动窗口（约近 1~7 天），不是我方自然月口径",
            "跨账号比较会骗人：全样本「含顶流人名」实测 0.32x（负相关），控制账号后 3.53x（正相关）——结论必须账号内验证",
            "没有曝光量，无法区分「标题不行」与「根本没进推荐池」，这是最大盲区",
        ],
    }
    for acc, rows in peers.items():
        # 同一账号可能混发图文和视频，按多数单位定主单位；
        # 主单位是"播放"的账号**不与图文账号横向比中位数**（量级不可比），只在表里标注。
        from collections import Counter
        unit = Counter(r.get("unit") or "阅读" for r in rows).most_common(1)[0][0]
        same = [r for r in rows if (r.get("unit") or "阅读") == unit]
        s = title_features.summarize(same)
        s["unit"] = unit
        s["mixed_units"] = len(set(r.get("unit") or "阅读" for r in rows)) > 1
        s["followers"] = followers.get(acc)
        s["top3"] = [r for r in sorted(same, key=lambda x: -x["views"])[:3]]
        s["bottom3"] = [r for r in sorted(same, key=lambda x: x["views"])[:3]]
        rep["peers"][acc] = s
    return rep


def render_md(rep: dict) -> str:
    L = [f"# 头部账号对标基线（{rep['generated_at'][:16]}）", ""]
    L += ["## 逐个账号", "",
          "| 账号 | 粉丝 | 主单位 | 样本 | 中位 | 过千率 | 过程锚点覆盖 | 疑问句占比 |",
          "|---|---|---|---|---|---|---|---|"]
    for acc, s in rep["peers"].items():
        ov = s.get("overall") or {}
        L.append("| {} | {} | {}{} | {} | {} | {}% | {}% | {}% |".format(
            acc,
            f"{s['followers']/10000:.1f}万" if s.get("followers") else "—",
            s.get("unit", "阅读"),
            "（混发）" if s.get("mixed_units") else "",
            s.get("n", 0),
            "—" if ov.get("median") is None else f"{ov['median']:.0f}",
            "—" if ov.get("over1000_rate") is None else ov["over1000_rate"],
            s.get("anchor_coverage", "—"),
            s.get("question_rate", "—"),
        ))
    o = rep.get("ours") or {}
    if o.get("n"):
        ov = o.get("overall") or {}
        L.append("| **我方（同期）** | ~0 | 阅读 | {} | {} | {}% | {}% | {}% |".format(
            o["n"],
            "—" if ov.get("median") is None else f"{ov['median']:.0f}",
            "—" if ov.get("over1000_rate") is None else ov["over1000_rate"],
            o.get("anchor_coverage"),
            o.get("question_rate"),
        ))
    L += ["", "> 主单位为「播放」的账号是视频号，播放量与图文阅读量**不可横向比**，"
              "只用于看它的选题方向和相对高低。", ""]
    L += ["", "## 标题组合分档（各档中位阅读）", "",
          "| 档位 | " + " | ".join(rep["peers"].keys()) + " | 我方 |",
          "|---|" + "---|" * (len(rep["peers"]) + 1)]
    for b in title_features.BUCKET_ORDER:
        cells = []
        for s in rep["peers"].values():
            v = (s["buckets"].get(b) or {})
            cells.append("—" if v.get("median") is None else
                         f"{v['median']:.0f}{'*' if v.get('thin') else ''}")
        ov = (o.get("buckets") or {}).get(b) or {}
        cells.append("—" if ov.get("median") is None else
                     f"{ov['median']:.0f}{'*' if ov.get('thin') else ''}")
        L.append("| {} | {} |".format(b, " | ".join(cells)))
    L += ["", "> `*` = 样本 <5，仅供参考，不可当结论。", "",
          "## 必须一起读的局限"]
    L += [f"- {c}" for c in rep["caveats"]]
    return "\n".join(L)



# ------------------------------------------------------------------ main
def load_config() -> dict:
    try:
        import yaml
    except Exception:
        return {}
    if not CONFIG_PATH.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_cached() -> dict[str, str]:
    if not CACHE_DIR.exists():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(CACHE_DIR.glob("*.txt"))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="头部账号对标抓取与基线计算")
    ap.add_argument("--account", action="append", default=[], help="只抓指定账号（可重复）")
    ap.add_argument("--scroll", type=int, default=0, help="覆盖滚动轮数")
    ap.add_argument("--reuse", action="store_true", help="复用上次落盘的原始文本，不联网")
    ap.add_argument("--days", type=int, default=30, help="我方基线回看天数")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    names = args.account or [a["name"] for a in (cfg.get("账号") or []) if a.get("name")]
    if not names:
        print("❌ config/peer_accounts.yaml 里没有可用账号")
        return 2
    if args.scroll:
        cfg.setdefault("抓取", {})["scroll_rounds"] = args.scroll

    # 1) 取原始文本
    if args.reuse:
        texts = load_cached()
        names = [n for n in names if n in texts]
        if not names:
            print("⚠️  没有可复用的落盘文本，先跑一次联网抓取")
            return 2
        if not args.quiet:
            print(f"♻️  复用 {len(names)} 个账号的落盘文本")
    else:
        try:
            import playwright  # noqa: F401
        except ImportError:
            print("⚠️  未安装 playwright，跳过对标抓取（本脚本不进 CI 硬门禁）")
            return 2
        if not args.quiet:
            print(f"🌐 抓取 {len(names)} 个账号主页…")
        texts = asyncio.run(_fetch_all(names, cfg))
        if not texts:
            print("⚠️  全部账号抓取失败，无产物")
            return 2

    # 2) 解析 + 打标
    peers, followers = {}, {}
    for acc, txt in texts.items():
        rows = parse_home_text(txt)
        if not rows:
            print(f"  ⚠️  {acc}: 文本里没解析出作品行（页面结构可能变了）")
            continue
        peers[acc] = rows
        f = parse_followers(txt, acc)
        if f is not None:
            followers[acc] = f
    if not peers:
        print("❌ 一个账号都没解析出作品行")
        return 1

    ours = load_our_articles(days=args.days)
    rep = build_report(peers, ours, followers)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"benchmark_baseline_{day}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    (AUDIT_DIR / "benchmark_baseline_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render_md(rep)
    (AUDIT_DIR / f"benchmark_baseline_{day}.md").write_text(md, encoding="utf-8")

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    elif not args.quiet:
        print()
        print(md)
        print(f"\n💾 已落盘 data/audit/benchmark_baseline_{day}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
