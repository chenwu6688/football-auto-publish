#!/usr/bin/env python3
"""头条号自动发布 — Playwright 浏览器自动化

Usage:
  # 首次使用: 先登录保存状态
  python scripts/publisher.py --login

  # 发布今日文章
  python scripts/publisher.py 2026-05-26

  # 发布为草稿（不公开发布）
  python scripts/publisher.py 2026-05-26 --draft
"""

import os, sys, time, json, re, requests
from pathlib import Path
from urllib.parse import parse_qs, unquote
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).parent
# 被 `python scripts/publisher.py` 或 CI 从别处调用时，脚本目录未必在 sys.path 里
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import prediction_gate           # noqa: E402  （必须在本目录入 sys.path 之后）

OUTPUT_BASE = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
AUTH_FILE = Path(os.environ.get("TOUTIAO_AUTH_FILE", PROJECT_ROOT / "toutiao_auth.json"))
AUDIT_DIR = Path(os.environ.get("AUDIT_DIR", PROJECT_ROOT / "data" / "audit"))

# Toutiao URLs
TOUTIAO_LOGIN = "https://mp.toutiao.com/auth/page/login/"
TOUTIAO_PUBLISH = "https://mp.toutiao.com/profile_v4/graphic/publish"

# WxPusher
WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", "")
WXPUSHER_UID = os.environ.get("WXPUSHER_UID", "")

# Batch name mapping: English key (from CLI --batch=noon) → Chinese display name (from article frontmatter)
BATCH_NAME_MAP = {
    "morning": "晨读",
    "noon": "午间",
    "evening": "晚间",
}

# 退出码约定（2026-11-05 立）。判据：看完退出码就知道**下一步该做什么**。
#   0  至少一篇发出去了
#   2  本批有稿，但全被规则跳过（预测稿/禁发类型）——**重跑没有意义**，可标批次完成
#   3  本批一篇成稿都没有（上游空转）——值得让兜底 cron 重跑一次，不标完成
#   1  真失败（登录失效/熔断/浏览器异常/全部发布失败）
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOTHING_SKIPPED = 2
EXIT_NOTHING_EMPTY = 3


class NoPublishableBatch(Exception):
    """本批**没有可发布的稿件**——不是「发布失败」，也不该报「成功」。

    2026-11-05 晚间：3 篇成稿里 2 篇被 `score` 字段撞车丢掉，剩下 1 篇是预测稿
    （按 R13 决策④不发头条）。publisher 直接 `sys.exit(1)`，于是：

      · 上层 job 判 failure，和「浏览器崩了」长得一模一样；
      · 更糟的是「回写批次状态」把这批写成了已完成 → 人工补发被幂等跳过
        → 当批永久丢失。

    根子是**三种完全不同的终态被压成了一个 exit 1**。所以这里把「没稿可发」
    拆成两种，用退出码区分，让上层能分别处理（详见 EXIT_* 常量）。
    """

    def __init__(self, code, detail, skipped=None):
        super().__init__(detail)
        self.code = code                     # "empty" | "all_skipped"
        self.detail = detail
        self.skipped = list(skipped or [])

    @property
    def exit_code(self):
        return EXIT_NOTHING_SKIPPED if self.code == "all_skipped" else EXIT_NOTHING_EMPTY



def send_wxpusher(title, content):
    if not WXPUSHER_APPTOKEN or not WXPUSHER_UID:
        return
    try:
        requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={"appToken": WXPUSHER_APPTOKEN, "content": f"{title}\n\n{content}",
                  "contentType": 1, "uids": [WXPUSHER_UID]},
            timeout=10,
        )
    except Exception:
        pass


def login_and_save_auth():
    """Open browser, let user login manually, auto-detect and save auth state."""
    print("=" * 50)
    print("头条号登录")
    print("=" * 50)
    print("\n浏览器已打开，请在 5 分钟内完成登录（扫码/验证码/密码均可）。")
    print("登录成功后请 ⚠️不要关闭浏览器⚠️，脚本会自动保存。\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(TOUTIAO_LOGIN, wait_until="domcontentloaded")
        print(f"已打开: {TOUTIAO_LOGIN}")

        max_wait = 300
        logged_in = False
        for elapsed in range(0, max_wait, 3):
            time.sleep(3)
            try:
                # Check login by trying to access publish page in a background tab
                bg_page = context.new_page()
                bg_page.goto(TOUTIAO_PUBLISH, wait_until="domcontentloaded", timeout=8000)
                bg_page.wait_for_timeout(2000)
                bg_url = bg_page.url.lower()
                bg_page.close()

                if "/auth/" not in bg_url and "/login" not in bg_url:
                    print(f"\n✅ 检测到登录成功! (可访问发布页)")
                    logged_in = True
                    # Save immediately while context is still alive
                    time.sleep(1)
                    try:
                        context.storage_state(path=str(AUTH_FILE))
                        print(f"   状态已保存: {AUTH_FILE}")
                    except Exception as save_err:
                        print(f"   保存异常: {save_err}")
                    break

                # Also periodically save state as backup
                if elapsed > 0 and elapsed % 15 == 0:
                    try:
                        context.storage_state(path=str(AUTH_FILE))
                    except Exception:
                        pass

                if elapsed % 30 == 0:
                    print(f"   等待扫码中... ({elapsed}s / {max_wait}s)")
            except Exception as e:
                if "closed" in str(e).lower():
                    print("\n⚠️  浏览器被关闭，尝试从最近的备份恢复...")
                    break
                if elapsed % 30 == 0:
                    print(f"   等待中... ({elapsed}s / {max_wait}s)")

        if not logged_in:
            print("   注意: 未确认登录成功，将尝试保存当前状态...")
            try:
                context.storage_state(path=str(AUTH_FILE))
            except Exception:
                pass

        try:
            browser.close()
        except Exception:
            pass

    if AUTH_FILE.exists():
        print(f"\n✅ 登录状态已保存至: {AUTH_FILE}")
    else:
        print("\n❌ 保存失败，请重试")


def _published_titles(date_str):
    """当天**已经真发出去**的标题（读发布台账，不看成稿 metadata）。

    为什么不读 `output/<date>/metadata.json` 的 platform_id：那条链在 CI 里断过
    （runner.temp 里的 metadata 与仓库里提交的那份不是同一份，`.platform_id` 至今全是空），
    拿它当去重依据等于没有依据。台账是每次发布都落盘并随 chore 提交的，可靠。

    这道保险丝挡的是「降级发布」：批次筛选 0 命中时会退化到发全部文章，
    不设防就会把早上已经发过的稿在晚上再发一遍（同一标题两个作品 ID）。
    """
    titles = set()
    for p in sorted(AUDIT_DIR.glob(f"publish_ledger_{date_str}_*.json")):
        if p.stem.endswith("_latest"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("records") or []:
            if r.get("status") == "published":
                t = (r.get("title") or "").strip()
                if t:
                    titles.add(t)
    return titles


def _no_publishable(batch_filter, date_dir, md_files, skip_report, batch_names):
    """构造 NoPublishableBatch，并**判清是哪一种 0 篇**。

    这两种 0 篇的处置完全不同，混在一起就等于把上游故障当成了正常：
      · all_skipped：本批**有**成稿，但全被规则跳过（预测稿/禁发类型）
        → 重跑还是同样的结果，标批次完成，只告警；
      · empty：本批**一篇都没有**（无论目录里是空的，还是只有别批的稿）
        → 上游空转，让兜底 cron 重跑一次才有意义。
    """
    total_md = len(md_files or [])
    skipped = list(skip_report or [])
    if batch_names:
        mine = [s for s in skipped if (s.get("batch_name") or "") in batch_names]
    else:
        mine = skipped
    if mine:
        # 原因要带出来：只写 tier 名等于「有告警没原因」（10-25 的老毛病）。
        reasons = "；".join(
            f"{s.get('title', '')[:12]}（{s.get('prediction_tier') or s.get('status')}："
            f"{(s.get('reason') or '')[:24]}）" for s in mine[:3])
        return NoPublishableBatch(
            "all_skipped",
            f"{batch_filter or '本批'} 有 {len(mine)} 篇成稿但全部被规则跳过（{reasons}）",
            mine)
    return NoPublishableBatch(
        "empty",
        f"{batch_filter or '本批'} 一篇成稿都没有（目录 {date_dir.name} 共 {total_md} 个 md，"
        f"其中属于本批的 0 个）")


def load_articles(date_str, batch_filter=None, skip_report=None):
    """Load generated articles from output directory. Optionally filter by batch.

    Args:
        date_str: Date string YYYY-MM-DD
        batch_filter: If set, only load articles whose batch_name matches (e.g. 'morning').
                      If None or empty, load all articles (backward compatible).
        skip_report: 可选的 list；预测稿等被显式跳过的条目会 append 到这里，
                     供发布台账记录「没发的原因」（默认 None，不影响既有调用方）。
    """
    date_dir = OUTPUT_BASE / date_str
    if not date_dir.exists():
        # Fallback: try previous day (scheduler delay may have shifted Beijing date)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            # 日期格式错是**调用方传参错**，不是「没稿可发」：抛出去让进程以 1 退出。
            # 不用 sys.exit 是为了让「本函数绝不自行终止进程」这条契约成立
            # （scripts/verify_publish_states 用 AST 钉着，见判据 B）。
            raise ValueError(f"日期格式非法: {date_str!r}（应为 YYYY-MM-DD）") from None
        from datetime import timedelta
        prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_dir = OUTPUT_BASE / prev_date
        if prev_dir.exists():
            print(f"⚠️  今日目录不存在，回退到昨日: {prev_dir}")
            date_dir = prev_dir
        else:
            # 目录不存在 = 上游一篇都没产出。这不是「发布失败」，而是上游空转，
            # 值得让兜底 cron 重跑（EXIT_NOTHING_EMPTY）；仍在退出码上与真失败分开。
            raise NoPublishableBatch("empty", f"文章目录不存在: {date_dir}（也试了 {prev_dir}）")


    all_md_files = sorted(date_dir.glob("article-*.md"))
    print(f"在 {date_dir} 中找到 {len(all_md_files)} 个 article-*.md 文件: {[f.name for f in all_md_files]}")
    articles = []
    _pred_publishable = 0          # 本批已放行的「有据前瞻」篇数（配额见 prediction_gate）
    # 跳过清单**始终**在本地积累：它是「本批为什么 0 篇」的唯一证据。
    # 不能只在调用方传了 skip_report 时才记账——那样「不传就查不出原因」，
    # 而这个函数恰恰要靠它区分 all_skipped 与 empty（两种终态处置完全不同）。
    _skips = skip_report if skip_report is not None else []
    _published = _published_titles(date_str)
    for md_file in all_md_files:
        content = md_file.read_text(encoding="utf-8")
        # Parse frontmatter
        meta = {}
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        key, _, val = line.partition(":")
                        meta[key.strip()] = val.strip().strip('"').strip("'")
                body = parts[2].strip()
            else:
                body = content
        else:
            body = content

        # Extract title
        title = meta.get("title", "")
        if not title:
            for line in body.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break

        # 已发布去重（保险丝）：台账里已 published 的标题一律不再发第二次。
        if title and title in _published:
            print(f"  ♻️ 跳过已发布稿（同日台账已有作品）: {title[:40]}")
            _skips.append({"status": "skipped_already_published", "title": title,
                           "slug": meta.get("slug") or "",
                           "batch_name": meta.get("batch_name", ""),
                           "platform_id": "", "reason": "同日台账已有发布记录，防重复发文"})
            continue

        # 预测稿走分级判定（唯一口径 prediction_gate），不再按体裁一刀切。
        # R13 决策④ 的原意是「别拿无据断言冒充内容」，实现却写成了「凡是预测稿都不发」：
        # 2026-11-05 晚间整批 3 篇里唯一活下来的就是预测稿 → 0 篇可发 → 整批判失败。
        # 现在按**依据**判：有据前瞻（basis≥2 项且无断言、不押比分）照发，观点稿照样不发，
        # 但跳过的原因会写进台账（tier + reasons），不再是「预测稿」三个字。
        _pmeta = {"title": title, "is_prediction": meta.get("is_prediction"),
                  "prediction_basis": meta.get("prediction_basis"),
                  "prediction_basis_items": meta.get("prediction_basis_items"),
                  "batch_name": meta.get("batch_name", ""), "slug": meta.get("slug") or ""}
        if prediction_gate.is_prediction(_pmeta):
            _verdict = prediction_gate.assess(_pmeta, body)
            if not _verdict["publish"]:
                print(f"  🔮 跳过预测稿（{_verdict['tier']}）: {title[:40]}"
                      f"—— {'；'.join(_verdict['reasons'])[:70]}")
                _skips.append({"status": "skipped_prediction", "title": title,
                               "slug": _pmeta["slug"],
                               "batch_name": _pmeta["batch_name"],
                               "platform_id": "",
                               "prediction_tier": _verdict["tier"],
                               "reason": "；".join(_verdict["reasons"])})
                continue
            # 有据前瞻：批内配额 1 篇（预测稿占位就不能再挤掉主稿）
            if prediction_gate.quota_left(_pred_publishable) <= 0:
                print(f"  🔮 跳过预测稿（配额已满 {prediction_gate.MAX_PUBLISHABLE_PER_BATCH} 篇）: {title[:40]}")
                _skips.append({"status": "skipped_prediction", "title": title,
                               "slug": _pmeta["slug"],
                               "batch_name": _pmeta["batch_name"],
                               "platform_id": "",
                               "prediction_tier": _verdict["tier"],
                               "reason": f"批内预测稿配额已满（上限 "
                                         f"{prediction_gate.MAX_PUBLISHABLE_PER_BATCH} 篇）"})
                continue
            _pred_publishable += 1
            meta["_prediction_tier"] = _verdict["tier"]
            print(f"  ✅ 预测稿放行（{_verdict['tier']}）：{_verdict['reasons'][0][:60]}")

        articles.append({
            "title": title,
            "file": str(md_file),
            "meta": meta,
            "body": body,
            "index": meta.get("article_index", "0"),
            "batch_name": meta.get("batch_name", ""),
        })

    # Apply batch filter if specified
    if batch_filter:
        before = len(articles)
        all_articles = articles  # 保留原始列表，供兜底发布
        # Try direct match first, then map English key → Chinese display name
        batch_names_to_match = {batch_filter}
        if batch_filter in BATCH_NAME_MAP:
            batch_names_to_match.add(BATCH_NAME_MAP[batch_filter])
        articles = [a for a in articles if a["batch_name"] in batch_names_to_match]
        skipped = before - len(articles)
        if skipped > 0:
            print(f"🔍 批次筛选 '{batch_filter}': {before} → {len(articles)} 篇 (跳过 {skipped} 篇其他批次)")
        if not articles:
            # 只降级「无批次归属」的稿（batch_name 为空的应急/补录稿）：它们**可能**属于本批。
            # 绝不降级「明确写了别的批次」的稿 —— 那是重复发文：晚上 0 篇时会去发早上的两篇，
            # 同一标题拿到第二个作品 ID，谁都看不出来（2026-11-05 复核实测这条路径就是这样）。
            orphan = [a for a in all_articles if not (a.get("batch_name") or "").strip()]
            if orphan:
                print(f"⚠️ 批次筛选 '{batch_filter}' 未匹配到文章；有 {len(orphan)} 篇无批次归属，"
                      f"按本批发布（请检查 orchestrator 是否正确写入 batch_name）")
                articles = orphan
            else:
                raise _no_publishable(batch_filter, date_dir, all_md_files,
                                      _skips, batch_names_to_match)

    if not articles:
        raise _no_publishable(None, date_dir, all_md_files, _skips, None)

    print(f"加载 {len(articles)} 篇文章:")
    for a in articles:
        print(f"  [{a['index']}] {a['title'][:50]}")
    return articles


def strip_ai_parentheticals(text):
    """Remove AI-typical parenthetical expressions from text.

    Removes full-width parentheses () containing AI-sounding filler phrases.
    """
    import re
    # Patterns for AI-sounding parenthetical content (Chinese full-width parens)
    ai_patterns = [
        r'（注[：:][^）]*）',
        r'（需要说明[^）]*）',
        r'（数据来源[^）]*）',
        r'（值得一提的是[^）]*）',
        r'（正如[^）]*）',
        r'（从某种意义上[^）]*）',
        r'（不可否认[^）]*）',
        r'（众所周知[^）]*）',
        r'（总而言之[^）]*）',
        r'（换句话说[^）]*）',
        r'（严格来说[^）]*）',
        r'（实际上[^）]*）',
        r'（可以说[^）]*）',
        r'（不得不说[^）]*）',
        r'（必须承认[^）]*）',
        r'（据[^）]*报道[^）]*）',
        r'（详见[^）]*）',
        r'（参考[^）]*）',
        r'（具体[^）]*）',
        r'（注[：:][^）]*）',
        r'（补充[^）]*）',
        r'（以上[^）]*）',
        r'（本文[^）]*）',
    ]
    for pattern in ai_patterns:
        text = re.sub(pattern, '', text)
    # Also remove empty parens that might be left over
    text = re.sub(r'（）', '', text)
    return text


def convert_md_to_text(body, title=""):
    """Convert markdown body to plain text with paragraph structure.

    Preserves paragraph boundaries (blank lines → \\n\\n separators).
    Skips the title line if it appears in the body.
    Strips AI-sounding parenthetical expressions.
    """
    import re
    raw_lines = body.split("\n")
    total_lines = len(raw_lines)
    paragraphs = []
    current = []
    title_clean = title.strip()

    for idx, line in enumerate(raw_lines):
        stripped = line.strip()

        # Blank line = paragraph boundary
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue

        # Skip image references
        if stripped.startswith("![") and "](" in line:
            continue
        # Skip auto-generated footer
        if stripped in ("每日自动生成", "球评人老六", "*每日自动生成"):
            continue
        if "每日自动生成" in stripped or "球评人老六" in stripped:
            continue
        if stripped == "---" and idx > total_lines * 0.8:
            continue
        # Skip orphaned single-char lines (LLM artifacts)
        if len(stripped) <= 1 and stripped.isascii() and not stripped.isdigit():
            continue

        # Strip markdown syntax
        cleaned = line.replace("**", "").replace("*", "")
        if cleaned.startswith("# "):
            cleaned = cleaned[2:]
        elif cleaned.startswith("## "):
            cleaned = cleaned[3:]
        elif cleaned.startswith("### "):
            cleaned = cleaned[4:]
        if cleaned.startswith("> "):
            cleaned = cleaned[2:]

        # Strip AI parenthetical expressions from this line
        cleaned = strip_ai_parentheticals(cleaned)

        # Skip if this line matches the title
        if title_clean and cleaned.strip() == title_clean:
            continue

        current.append(cleaned)

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def extract_images(body):
    """Extract image references from markdown body."""
    import re
    images = re.findall(r'!\[.*?\]\((images/article-\d+-img-\d+\.jpg)\)', body)
    return list(dict.fromkeys(images))  # dedup, preserve order


def debug_dump_page(page, label=""):
    """Dump page HTML and take screenshot for debugging."""
    import tempfile
    ts = time.strftime("%H%M%S")
    tmpdir = Path(tempfile.gettempdir()) / "toutiao_debug"
    tmpdir.mkdir(exist_ok=True)

    # Take screenshot
    ss_path = tmpdir / f"screenshot-{label}-{ts}.png"
    try:
        page.screenshot(path=str(ss_path), full_page=False)
        print(f"  📸 截图已保存: {ss_path}")
    except Exception as e:
        print(f"  ⚠️  截图失败: {e}")

    # Dump relevant HTML sections
    html_path = tmpdir / f"page-{label}-{ts}.html"
    try:
        # Get HTML snippets for key areas
        snippets = {}
        for area, selector in [
            ("toolbar", ".syl-toolbar-container"),
            ("toolbar2", '[class*="toolbar"]'),
            ("editor_header", ".publish-editor-header"),
            ("editor_main", ".publish-editor"),
            ("footer_actions", ".publish-action-bar"),
            ("footer", ".publish-footer"),
            ("all_buttons", "button"),
        ]:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=1000):
                    html = el.evaluate("el => el.outerHTML")
                    snippets[area] = html[:2000]
            except Exception:
                pass

        # Also dump all input elements
        try:
            inputs = page.locator("input").all()
            for idx, inp in enumerate(inputs[:10]):
                try:
                    html = inp.evaluate("el => el.outerHTML")
                    snippets[f"input_{idx}"] = html[:500]
                except Exception:
                    pass
        except Exception:
            pass

        # List ALL toolbar items with their class names
        try:
            tools = page.locator('.syl-toolbar-tool').all()
            tool_info = []
            for idx, tool in enumerate(tools):
                try:
                    cls = tool.get_attribute("class") or ""
                    text = tool.text_content() or ""
                    tool_info.append(f"[{idx}] class='{cls}' text='{text[:30]}'")
                except Exception:
                    pass
            snippets["toolbar_items"] = "\n".join(tool_info)
        except Exception:
            pass

        with open(html_path, "w") as f:
            f.write(f"<!-- Debug dump for: {label} at {ts} -->\n")
            for name, html in snippets.items():
                f.write(f"\n<!-- === {name} === -->\n")
                f.write(html)
                f.write("\n")
        print(f"  📄 HTML已保存: {html_path}")
    except Exception as e:
        print(f"  ⚠️  HTML导出失败: {e}")

    return tmpdir


def _find_editor_view_js(var_name="view"):
    """Return JS code that finds the ProseMirror EditorView via React fiber walk."""
    return f"""
    let {var_name} = null;
    (() => {{
        const rootEl = document.getElementById('root');
        if (!rootEl) return;
        const fiberKey = Object.keys(rootEl).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactContainer'));
        if (!fiberKey) return;

        function walkFiber(fiber, depth) {{
            if (!fiber || depth > 100) return null;
            if (fiber.memoizedState) {{
                let hook = fiber.memoizedState;
                while (hook) {{
                    const val = hook.memoizedState;
                    if (val && typeof val === 'object') {{
                        if (val.view && val.view.state && val.view.dispatch)
                            return val.view;
                        if (val.current && val.current.state && val.current.dispatch)
                            return val.current;
                    }}
                    hook = hook.next;
                }}
            }}
            return walkFiber(fiber.child, depth + 1) || walkFiber(fiber.sibling, depth + 1);
        }}
        {var_name} = walkFiber(rootEl[fiberKey], 0);
    }})();
    """


def fill_prosemirror(page, text_content, selector='.ProseMirror', max_retries=3):
    """Fill ProseMirror editor via direct EditorView transaction.

    Retries up to max_retries times because the EditorView may not be
    fully initialized when the page first renders (SPA hydration delay).
    """
    # Split into paragraphs and escape HTML entities
    paragraphs = []
    for para in text_content.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        para_escaped = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        paragraphs.append(para_escaped)

    # Also build HTML for innerHTML fallback
    html = "".join(f"<p>{p}</p>" for p in paragraphs)

    last_error = None
    for attempt in range(max_retries):
        if attempt > 0:
            wait_ms = 2000 * attempt
            print(f"     EditorView 未就绪，等待 {wait_ms}ms 重试 ({attempt+1}/{max_retries})...")
            page.wait_for_timeout(wait_ms)

        result = page.evaluate(
            """
            ([selector, html, paragraphs]) => {
                const el = document.querySelector(selector);
                if (!el) return {ok: false, error: 'editor not found'};
                """
            + _find_editor_view_js()
            + """
                if (!view) return {ok: false, error: 'EditorView not found'};

                try {
                    const {state} = view;
                    const {schema} = state;

                    const paraNodes = [];
                    for (const p of paragraphs) {
                        if (p) {
                            paraNodes.push(schema.nodes.paragraph.create(null, schema.text(p)));
                        }
                    }
                    if (paraNodes.length === 0) {
                        paraNodes.push(schema.nodes.paragraph.create(null));
                    }

                    const docNode = schema.nodes.doc.create(null, paraNodes);
                    const tr = state.tr.replaceWith(0, state.doc.content.size, docNode.content);
                    view.dispatch(tr);

                    return {
                        ok: true,
                        textLen: view.state.doc.textContent.length,
                        innerHTML_len: el.innerHTML.length,
                        hasPTags: el.querySelectorAll('p').length,
                        pmSynced: true,
                        pmDocSize: view.state.doc.content.size,
                        pmTextLen: view.state.doc.textContent.length,
                    };
                } catch(e) {
                    return {ok: false, error: 'dispatch failed: ' + e.message + ' stack: ' + (e.stack || '')};
                }
            }
        """,
            [selector, html, paragraphs],
        )

        if result.get("ok") and result.get("textLen", 0) > 0:
            return result
        last_error = result.get("error", "unknown")

    return {"ok": False, "error": f"after {max_retries} retries: {last_error}"}


def fill_title(page, title, selector=".publish-editor-title textarea"):
    """Fill title textarea — it's a <textarea> inside .publish-editor-title."""
    try:
        el = page.locator(selector).first
        el.wait_for(state="attached", timeout=5000)

        # Click to focus the textarea
        el.click(force=True)
        page.wait_for_timeout(300)

        # Clear and fill using fill() which handles React controlled inputs properly
        el.fill(title)
        page.wait_for_timeout(500)

        # Verify
        value = el.input_value()
        ok = len(value) >= len(title) * 0.8
        return {"ok": ok, "textLen": len(value), "text": value[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e), "textLen": 0}


def dismiss_overlays(page):
    """Close any AI assistant drawers or popups that block the editor. Use JS for reliability."""
    try:
        # Force-remove AI assistant drawer via JS (most reliable for SPA)
        page.evaluate("""() => {
            const remove = (sel) => document.querySelectorAll(sel).forEach(el => el.remove());
            remove('.byte-drawer-mask, .byte-modal-mask');
            remove('.byte-drawer-wrapper, .byte-drawer, .ai-assistant-drawer');
            remove('.byte-modal, .byted-modal');
            // Remove any absolutely positioned overlays
            document.querySelectorAll('div[style*="fixed"], div[class*="overlay"]').forEach(el => {
                if (getComputedStyle(el).position === 'fixed') el.remove();
            });
            // Restore body scroll
            document.body.style.overflow = '';
        }""")
        page.wait_for_timeout(300)
    except Exception:
        pass


def _first_pgc_id(results) -> str:
    """从发布响应/请求记录里取第一个有效 pgc_id（= 头条作品 ID，即 platform_id）。

    2026-11-01 月度分析查出的洞：`publish_article` 只回 `{"ok": True}`，把响应里
    现成的 pgc_id 丢掉了 → 成稿的 platform_id 只能靠「下次导出按标题反查」补，
    而导出只回溯 7 天，跨期就永久补不回来。这里把 ID 直接带出来，发布即可绑定。
    """
    for r in results or []:
        pid = str(r.get("pgc_id") or "").strip()
        if pid and pid not in ("0", "-1", "None", "null"):
            return pid
    return ""


def publish_article(page, article, date_str, draft_mode=False):
    """Publish a single article on Toutiao."""
    title = article["title"]
    body = article["body"]
    images = extract_images(body)
    text_body = convert_md_to_text(body, title=title)

    print(f"\n{'='*60}")
    print(f"发布: {title[:50]}")
    print(f"图片: {len(images)} 张")
    print(f"{'='*60}")

    # Navigate to publish page — use domcontentloaded (networkidle can hang on SPA)
    page.goto(TOUTIAO_PUBLISH, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    # Force reload to ensure clean editor state (avoids cached page issues)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

    # Close AI assistant drawer
    dismiss_overlays(page)
    page.wait_for_timeout(1000)

    # Wait for editor to be fully ready (SPA hydration can be slow)
    editor_ready = False
    for attempt in range(5):
        try:
            pm = page.locator('.ProseMirror').first
            if pm.is_visible(timeout=3000):
                # Verify editor has contenteditable working (not just visible DOM)
                has_content = page.evaluate("""() => {
                    const pm = document.querySelector('.ProseMirror');
                    if (!pm) return false;
                    return pm.getAttribute('contenteditable') === 'true' || pm.textContent !== undefined;
                }""")
                if has_content:
                    editor_ready = True
                    break
        except Exception:
            pass
        page.wait_for_timeout(2000)
        print(f"  ⏳ 等待编辑器就绪 ({attempt+1}/5)...")

    if not editor_ready:
        print(f"  ⚠️  ProseMirror 编辑器未就绪，尝试继续...")

    # === Fill Title (contenteditable div, not input) ===
    # Use execCommand('insertText') which triggers beforeinput events that
    # ProseMirror detects and uses to sync its internal document state.
    title_result = fill_title(page, title)
    if title_result.get("ok") and title_result.get("textLen", 0) > 0:
        print(f"  ✅ 标题已填入 ({title_result['textLen']} 字)")
    else:
        print(f"  ❌ 无法填入标题: {title_result}")
        debug_dump_page(page, f"title_fail_{article['index']}")
        return {"ok": False, "error": f"标题填入失败: {title_result}"}

    # === Fill Content (ProseMirror editor) ===
    pm_result = fill_prosemirror(page, text_body)
    if pm_result.get("ok") and pm_result.get("textLen", 0) > 0:
        print(f"  ✅ 正文已填入 ({pm_result['textLen']} 字, {pm_result.get('hasPTags', 0)} 段)")
    else:
        print(f"  ❌ 无法填入正文: {pm_result}")
        debug_dump_page(page, f"content_fail_{article['index']}")
        return {"ok": False, "error": f"正文填入失败: {pm_result}"}

    # === Upload Images via toolbar ===
    if images:
        upload_ok = 0

        # Click in editor first to ensure it's initialized
        page.locator('.ProseMirror').first.click(force=True)
        page.wait_for_timeout(500)

        for i, img_rel in enumerate(images[:3]):
            img_path = OUTPUT_BASE / date_str / img_rel
            if not img_path.exists():
                print(f"  ⚠️  图片不存在: {img_path}")
                continue

            print(f"  上传图片 {i+1}/{min(len(images), 3)}: {img_path.name}...")

            try:
                imgs_before = page.locator('.ProseMirror img').count()

                # Close any existing popovers
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

                # Click image toolbar button
                page.locator('.syl-toolbar-tool.image').first.click(force=True)
                page.wait_for_timeout(3000)

                # Try to find "本地上传" link/button in the popover
                # On first try, the popover might show "网络图片" tab by default
                upload_triggered = False
                try:
                    # First try direct text match
                    for text in ["本地上传", "上传图片"]:
                        el = page.locator(f'text="{text}"').first
                        if el.is_visible(timeout=3000):
                            with page.expect_file_chooser(timeout=10000) as fc_info:
                                el.click(force=True)
                            fc = fc_info.value
                            fc.set_files(str(img_path))
                            upload_triggered = True
                            print(f"    ✅ 点击了'{text}'")
                            break
                except Exception:
                    pass

                if not upload_triggered:
                    # Try to find any clickable element in the popover
                    try:
                        # Look for file input directly
                        file_input = page.locator('input[type="file"]').first
                        file_input.set_input_files(str(img_path))
                        upload_triggered = True
                        print(f"    ✅ 直接file input上传")
                    except Exception:
                        pass

                if not upload_triggered:
                    print(f"    ⚠️  无法触发上传")
                    page.keyboard.press("Escape")
                    if i == 0:
                        debug_dump_page(page, "image_upload_error")
                    continue

                # Wait for upload to complete
                page.wait_for_timeout(5000)

                # Check if image was auto-inserted (upload might auto-insert)
                imgs_check = page.locator('.ProseMirror img').count()
                if imgs_check > imgs_before:
                    # Already inserted, skip "确定"
                    print(f"    ✅ 图片已自动插入")
                else:
                    # Click "确定" to insert
                    try:
                        confirm_btn = page.locator('button:has-text("确定")').first
                        if confirm_btn.is_visible(timeout=5000):
                            confirm_btn.click(force=True)
                            page.wait_for_timeout(3000)
                            print(f"    ✅ 已点击确定")
                    except Exception:
                        pass

                # Dismiss popover
                page.keyboard.press("Escape")
                page.wait_for_timeout(1500)

                imgs_after = page.locator('.ProseMirror img').count()
                if imgs_after > imgs_before:
                    upload_ok += 1
                    print(f"    ✅ 上传成功 ({upload_ok}/{min(len(images), 3)}) [编辑器内图片: {imgs_after}]")
                else:
                    print(f"    ⚠️  图片未插入编辑器")

            except Exception as e:
                print(f"    ⚠️  上传失败: {e}")
                page.keyboard.press("Escape")
                if i == 0:
                    debug_dump_page(page, "image_upload_error")

            if i < len(images) - 1:
                page.wait_for_timeout(1500)

        if upload_ok == 0:
            print(f"  ⚠️  所有图片上传均失败，继续发布纯文本")

    # === Set cover mode ===
    if len(images) == 0:
        try:
            no_cover = page.locator('span:has-text("无封面")').first
            if no_cover.is_visible(timeout=1000):
                no_cover.click()
                page.wait_for_timeout(500)
                print(f"  📷 已选择无封面模式")
        except Exception:
            pass
    elif len(images) >= 3:
        try:
            san_tu = page.locator('span:has-text("三图")').first
            if san_tu.is_visible(timeout=1000):
                san_tu.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

    # === Publish or Save Draft ===
    page.wait_for_timeout(2000)
    if draft_mode:
        print("\n  📝 草稿模式：触发保存...")

        # Method 1: Try Ctrl+S (common save shortcut)
        saved = False
        try:
            page.locator('.ProseMirror').first.click(force=True)
            page.wait_for_timeout(300)
            page.keyboard.press("Control+s")
            page.wait_for_timeout(3000)
            # Check if save indicator changed
            try:
                indicator = page.locator('.footer-tip-save, [class*="draft-save"] span').first
                text = (indicator.text_content() or "").strip()
                print(f"  💾 Ctrl+S后: {text}")
                if "已保存" in text or "保存成功" in text:
                    saved = True
            except Exception:
                pass
        except Exception as e:
            print(f"  Ctrl+S 失败: {e}")

        # Method 2: Type a char and delete to trigger input event then wait
        if not saved:
            try:
                editor = page.locator('.ProseMirror').first
                editor.click(force=True)
                page.wait_for_timeout(300)
                # Type and delete a space to trigger real input events
                editor.press("Space")
                page.wait_for_timeout(200)
                editor.press("Backspace")
                page.wait_for_timeout(3000)
            except Exception:
                pass

        # Method 3: Click 发文设置 dropdown
        if not saved:
            try:
                settings_btn = page.locator('.footer-back-content:has-text("发文设置")').first
                if settings_btn.is_visible(timeout=2000):
                    settings_btn.click(force=True)
                    page.wait_for_timeout(1500)
                    for txt in ["保存草稿", "存草稿", "暂存", "草稿"]:
                        try:
                            opt = page.locator(f'text="{txt}"').first
                            if opt.is_visible(timeout=1000):
                                opt.click(force=True)
                                print(f"  ✅ 点击了: {txt}")
                                page.wait_for_timeout(3000)
                                saved = True
                                break
                        except Exception:
                            continue
                    if not saved:
                        # Close dropdown by clicking elsewhere
                        page.locator('.publish-editor-title').first.click(force=True)
            except Exception:
                pass

        # Wait and monitor save status
        for sec in range(0, 12, 2):
            page.wait_for_timeout(2000)
            try:
                indicator = page.locator('.footer-tip-save, [class*="draft-save"] span').first
                if indicator.is_visible(timeout=1000):
                    text = (indicator.text_content() or "").strip()
                    print(f"  ⏳ [{sec+2}s] {text}")
                    if "已保存" in text or "保存成功" in text:
                        saved = True
                        break
                else:
                    print(f"  ⏳ [{sec+2}s] save indicator hidden")
                    saved = True
                    break
            except Exception:
                saved = True
                break

        if saved:
            print(f"  ✅ 草稿已保存")
        else:
            print(f"  ⚠️  自动保存状态不明，尝试直接发布...")

        return {"ok": True}
    else:
        print("\n  🚀 公开发布...")

        publish_results = []  # Collect all responses; check for any code=0

        def handle_publish_route(route):
            req = route.request
            if "/article/publish" in req.url:
                post_data = req.post_data
                if post_data:
                    print(f"\n  📤 === PUBLISH REQUEST ===")
                    print(f"  URL: {req.url[:150]}")
                    try:
                        parsed = parse_qs(post_data)
                        for k, v in parsed.items():
                            v_str = str(v[0])
                            if len(v_str) > 200:
                                print(f"    {k}=[{len(v_str)} chars] {v_str[:150]}...")
                            else:
                                print(f"    {k}={v_str}")
                        # Extract pgc_id from request for fallback publish
                        if "pgc_id" in parsed:
                            pgc_id_val = parsed["pgc_id"][0]
                            publish_results.append({"code": -1, "message": "from_request", "pgc_id": pgc_id_val})
                    except Exception:
                        pass
                    print(f"  === END REQUEST ===\n")
            route.continue_()

        page.route("**/article/publish**", handle_publish_route)

        def on_publish_response(response):
            if "/article/publish" in response.url:
                try:
                    body = response.json()
                    code = body.get("code", body.get("err_code"))
                    msg = body.get("message", body.get("msg", ""))
                    pgc_id = body.get("data", {}).get("pgc_id") or body.get("pgc_id", "")
                    print(f"\n  📡 === PUBLISH RESPONSE ===")
                    print(f"  Status: {response.status}")
                    print(f"  Code: {code}")
                    print(f"  Message: {msg}")
                    if pgc_id:
                        print(f"  pgc_id: {pgc_id}")
                    result = {"code": code, "message": msg, "pgc_id": pgc_id}
                    publish_results.append(result)
                    if code == 0:
                        print(f"  ✅ 发布成功!")
                    else:
                        print(f"  ❌ 发布失败!")
                        print(f"  Full response: {json.dumps(body, ensure_ascii=False)[:500]}")
                    print(f"  === END RESPONSE ===\n")
                except Exception as e:
                    print(f"  📡 Publish response (non-JSON): status={response.status}, error={e}")

        page.on("response", on_publish_response)

        try:
            publish_btn = page.locator('button:has-text("预览并发布")').first
            if publish_btn.is_visible(timeout=3000):
                publish_btn.click(force=True)
                print(f"  ✅ 已点击预览并发布")

                # Wait for the first response (from 预览并发布 click) to arrive.
                # The dialog's "发布" button only sends the real request after the
                # preview response settles. If we click too early, the 2nd request
                # never fires. Poll for up to 10s.
                for _ in range(20):
                    page.wait_for_timeout(500)
                    if publish_results:
                        break

                # Extra wait for publish dialog to fully render
                page.wait_for_timeout(2000)


                # Click confirmation button in the dialog
                confirmed = False
                for btn_text in ["发布", "确认发布", "确定", "确认并发布", "提交"]:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}")').last
                        if btn.is_visible(timeout=2000):
                            btn.click(force=True)
                            print(f"  ✅ 已确认: {btn_text}")
                            confirmed = True
                            break
                    except Exception:
                        continue

                # Fallback: try clicking any button in the dialog footer
                if not confirmed:
                    try:
                        # Try to find the dialog and click its primary button
                        for selector in [
                            '.byte-modal button.byte-btn-primary',
                            '.byte-dialog button.byte-btn-primary',
                            '[class*="modal"] button[class*="primary"]',
                            '[class*="dialog"] button[class*="primary"]',
                            '.publish-dialog button:last-child',
                            'button:has-text("发布")',
                        ]:
                            try:
                                btn = page.locator(selector).last
                                if btn.is_visible(timeout=1000):
                                    btn.click(force=True)
                                    print(f"  ✅ 已通过选择器确认: {selector}")
                                    confirmed = True
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

                # Last resort: try pressing Enter (some dialogs accept Enter as confirm)
                if not confirmed:
                    try:
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(1500)
                        print(f"  🔄 尝试 Enter 键确认...")
                        confirmed = True  # Assume it worked, let poll determine
                    except Exception:
                        pass

                if not confirmed:
                    print(f"  ⚠️  未找到确认按钮")
                    debug_dump_page(page, f"no_confirm_btn_{article['index']}")
                    return {"ok": False, "error": "未找到发布确认按钮"}

                # Check ALL publish responses for success (if any returned Code:0, it's published).
                # The "预览并发布" auto-save response (Code:0) counts as success too.
                # After clicking confirm, poll briefly for new responses but accept any Code:0.
                all_results_ok = any(r["code"] == 0 for r in publish_results)
                if all_results_ok:
                    return {"ok": True, "pgc_id": _first_pgc_id(publish_results)}

                # Poll for new publish result after confirmation (up to 15s).
                for _ in range(15):
                    page.wait_for_timeout(1000)
                    if any(r["code"] == 0 for r in publish_results):
                        return {"ok": True, "pgc_id": _first_pgc_id(publish_results)}

                # Timed out — try direct API call as fallback
                # First API response already saved the draft; extract pgc_id and submit
                pgc_id = None
                for req_data in [r.get("pgc_id") for r in publish_results if r.get("pgc_id")]:
                    if req_data:
                        pgc_id = req_data
                        break
                if not pgc_id:
                    # Try to parse pgc_id from request URLs
                    import re as _re
                    for r in publish_results:
                        if r.get("pgc_id"):
                            pgc_id = r["pgc_id"]
                            break
                if pgc_id:
                    try:
                        print(f"  🔄 尝试API直调发布 (pgc_id={pgc_id})...")
                        import requests as _req
                        cookies = page.context.cookies()
                        s = _req.Session()
                        for c in cookies:
                            if c.get('name'):
                                s.cookies.set(c['name'], c['value'])
                        api_headers = {
                            'User-Agent': 'Mozilla/5.0',
                            'Referer': 'https://mp.toutiao.com/',
                            'Content-Type': 'application/x-www-form-urlencoded',
                        }
                        payload = {
                            'pgc_id': str(pgc_id),
                            'source': '29',
                            'save': '0',
                            'timer_status': '0',
                            'entrance': 'main',
                        }
                        r = s.post(
                            'https://mp.toutiao.com/mp/agw/article/publish',
                            data=payload, headers=api_headers, timeout=15)
                        if r.status_code == 200:
                            result = r.json()
                            if result.get('code') == 0:
                                print(f"  ✅ API直调发布成功!")
                                _pid = _first_pgc_id(publish_results) or str(pgc_id).strip()
                                return {"ok": True, "pgc_id": _pid}
                            else:
                                print(f"  ⚠️ API返回: {result.get('message', 'unknown')}")
                        else:
                            print(f"  ⚠️ API HTTP {r.status_code}")
                    except Exception as api_err:
                        print(f"  ⚠️ API直调失败: {api_err}")

                if not publish_results:
                    msg = "发布超时: 无API响应"
                    print(f"  ❌ {msg}")
                    debug_dump_page(page, f"publish_timeout_{article['index']}")
                    return {"ok": False, "error": msg, "pgc_id": ""}
                codes = [(r["code"], r.get("message", "")[:30]) for r in publish_results]
                msg = f"发布失败, 响应: {codes}"
                print(f"  ❌ {msg}")
                # 失败也带出 pgc_id：草稿已建时 ID 是存在的，留着才能人工补发/定位
                return {"ok": False, "error": msg, "pgc_id": _first_pgc_id(publish_results)}

        except Exception as e:
            print(f"  ❌ 发布失败: {e}")
        finally:
            try:
                page.unroute("**/article/publish**")
            except Exception:
                pass
            try:
                page.remove_listener("response", on_publish_response)
            except Exception:
                pass

    return {"ok": False, "error": "发布流程异常终止", "pgc_id": ""}


def bind_platform_ids(date_str: str, bindings: list[dict]) -> dict:
    """把发布时拿到的 pgc_id 写回 `output/<date>/metadata.json` 的对应成稿。

    匹配优先 slug（批内唯一、跨平台稳定），其次标题。**匹配不上不猜**——记为
    unmatched 返回给调用方，宁可留着让导出回填，也不要按 index 乱绑（index 会重复）。

    返回 {"bound": n, "unmatched": [...], "missing_meta": bool}
    """
    out = {"bound": 0, "unmatched": [], "missing_meta": False}
    meta_path = OUTPUT_BASE / date_str / "metadata.json"
    if not meta_path.exists():
        out["missing_meta"] = True
        out["unmatched"] = [b.get("slug") or b.get("title") for b in bindings]
        return out
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        out["missing_meta"] = True
        out["unmatched"] = [f"metadata 解析失败: {e}"]
        return out

    arts = meta.get("articles") or []
    by_slug = {a.get("slug"): a for a in arts if a.get("slug")}
    by_title = {str(a.get("title") or "").strip(): a for a in arts}
    changed = False
    for b in bindings:
        pid = str(b.get("platform_id") or "").strip()
        if not pid:
            continue
        art = by_slug.get(b.get("slug")) or by_title.get(str(b.get("title") or "").strip())
        if not art:
            out["unmatched"].append(b.get("slug") or b.get("title"))
            continue
        art["platform_id"] = pid
        art["platform_id_source"] = b.get("source") or "publish_response"
        out["bound"] += 1
        changed = True
    if changed:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _day_batch_files(date_str: str) -> list[Path]:
    """当天各批次的台账文件（按批次名排序，便于复现）。"""
    return sorted(AUDIT_DIR.glob(f"publish_ledger_{date_str}_*.json"))


def _merge_day(date_str: str, current: dict) -> dict:
    """把当天各批次台账合成一份「当天」视图。

    2026-11-04 修复：此前 ``publish_ledger_{date}.json`` 是**直接覆盖写**，
    而一天有晨/午/晚三个批次 —— 后一批把前一批的记录整份盖掉。
    11-04 就是这样：晨间那篇发布记录消失，只剩午间 2 条，
    「今天实际发出几篇」从台账上永远算不对。

    兼容旧数据：旧格式文件（无 ``version`` 字段）当作一个未知批次并入，
    新格式（``version >= 2``）本身就是聚合结果，不再二次合并（否则翻倍）。
    """
    rows: list[dict] = []
    seen: set[tuple] = set()

    def _add(rec: dict, batch: str) -> None:
        key = (rec.get("title") or "", rec.get("status") or "", batch)
        if key in seen:
            return
        seen.add(key)
        r = dict(rec)
        r.setdefault("batch", batch)
        rows.append(r)

    legacy_batches: list[str] = []
    no_pub: dict[str, dict] = {}
    for p in _day_batch_files(date_str):
        batch = p.stem[len(f"publish_ledger_{date_str}_"):]
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for rec in d.get("records") or []:
            _add(rec, batch)
        # 「本批 0 篇」的记录要跟着聚合走，否则当天看板只能看到「少了一批」，
        # 看不出那一批是「有稿全被跳过」还是「上游没产出」。
        if d.get("no_publishable"):
            no_pub[batch] = d["no_publishable"]
        legacy_batches.append(batch)

    for rec in current.get("records") or []:
        _add(rec, current.get("batch_filter") or "all")

    # 旧格式（无 version）单文件：并入，避免历史当天记录丢失
    old = AUDIT_DIR / f"publish_ledger_{date_str}.json"
    if old.exists():
        try:
            d = json.loads(old.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        if not d.get("version"):
            b = (d.get("batch_filter") or "legacy") + "(legacy)"
            for rec in d.get("records") or []:
                _add(rec, b)
            legacy_batches.append(b)

    ok = sum(1 for r in rows if r.get("status") == "published")
    fail = sum(1 for r in rows if r.get("status") == "failed")
    skip = sum(1 for r in rows if r.get("status") == "skipped_prediction")
    dup = sum(1 for r in rows if r.get("status") == "skipped_already_published")
    return {
        "version": 2,
        "date": date_str,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_filter": "+".join(sorted(set(legacy_batches))) or "all",
        "batches": sorted(set(legacy_batches)),
        "counts": {
            "total": len(rows), "published": ok, "failed": fail,
            "skipped_prediction": skip, "skipped_already_published": dup,
            "published_without_id": sum(1 for r in rows if r.get("status") == "published"
                                        and not r.get("platform_id")),
            "published_without_cover": sum(1 for r in rows if r.get("status") == "published"
                                           and not (r.get("images") or 0)),
        },
        "platform_id_binding": current.get("platform_id_binding") or {},
        "records": rows,
        "no_publishable": no_pub or None,
        "note": ("成稿数 ≠ 发布数。published_without_id > 0 说明平台没返回作品 ID，"
                 "这些篇只能等后台导出按标题回填，跨 7 天窗口就永久缺失。"
                 "本文件是当天各批次聚合，批次明细见 publish_ledger_{date}_<batch>.json。"),
    }


def write_publish_ledger(date_str: str, records: list[dict], *, batch_filter: str | None = None,
                         binding: dict | None = None,
                         no_publishable: dict | None = None) -> Path:
    """落发布结果台账：每篇成稿的归宿（published / failed / skipped_prediction）。

    2026-11-01 之前这件事**完全没做**：`publish_all` 只把结果拼成一段人话发
    WxPusher，不落库。后果是「成稿 242 篇、实际发布 93 篇」这种 62% 的漏斗
    谁都不知道，月度分析只能靠导出文件反推。台账补齐后，覆盖率的分母才有意义。

    2026-11-04 改为三层写入（此前按天单文件，多批次互相覆盖）：
      ① ``publish_ledger_{date}_{batch}.json`` —— 本批次，不覆盖兄弟批次
      ② ``publish_ledger_{date}.json``         —— 当天聚合（version=2）
      ③ ``publish_ledger_latest.json``         —— 本次批次，兼容既有读取方
    """
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    batch = (batch_filter or "all").strip() or "all"
    ok = sum(1 for r in records if r.get("status") == "published")
    fail = sum(1 for r in records if r.get("status") == "failed")
    skip = sum(1 for r in records if r.get("status") == "skipped_prediction")
    dup = sum(1 for r in records if r.get("status") == "skipped_already_published")
    no_pid = sum(1 for r in records if r.get("status") == "published" and not r.get("platform_id"))
    # 无封面发布篇数：头条「封面模式」在 0 图时被主动设成「无封面」，
    # 而这条路径的聚合点击率只有有封面篇的 1/4（2026-11-01 体检）。
    no_cover = sum(1 for r in records
                   if r.get("status") == "published" and not (r.get("images") or 0))
    payload = {
        "version": 2,
        "date": date_str,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_filter": batch,
        "counts": {"total": len(records), "published": ok, "failed": fail,
                   "skipped_prediction": skip, "skipped_already_published": dup,
                   "published_without_id": no_pid,
                   "published_without_cover": no_cover},
        "platform_id_binding": binding or {},
        "records": records,
        "note": ("成稿数 ≠ 发布数。published_without_id > 0 说明平台没返回作品 ID，"
                 "这些篇只能等后台导出按标题回填，跨 7 天窗口就永久缺失。"),
    }
    if no_publishable:
        # 「本批 0 篇」必须留证：光有退出码，事后没人能回答「为什么没发」。
        # code=all_skipped 有稿全被规则跳过（设计内）；empty 上游没产出（故障）。
        payload["counts"]["no_publishable"] = no_publishable.get("code")
        payload["no_publishable"] = no_publishable
        payload["note"] += (" no_publishable 记录本批为什么 0 篇："
                            "all_skipped=有稿但全被规则跳过（重跑无意义），"
                            "empty=上游一篇都没产出（值得重跑）。")
    (AUDIT_DIR / f"publish_ledger_{date_str}_{batch}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # 当天聚合必须在「本批次文件」落盘之后再算，否则会漏掉自己
    p = AUDIT_DIR / f"publish_ledger_{date_str}.json"
    p.write_text(json.dumps(_merge_day(date_str, payload), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    (AUDIT_DIR / "publish_ledger_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def publish_all(date_str, draft_mode=False, headless=False, batch_filter=None):
    """Main publish flow.

    Args:
        batch_filter: If set, only publish articles from this batch (morning/noon/evening).
    """
    # 计划第十章 · 合规红线：熔断中一律不发布（发布边界硬闸）。
    # 2026-10-13 事故谱系：一致性打回超阈值的熔断在「生成文章」阶段才触发，
    # 而 batch.yml 此前只在批次开头检查一次 → 当晚批次照常生成并发布，
    # 故障直到次日批次开头才停下。本检查是发布前的最后一道闸——
    # 无论被 CI 还是人工调用，熔断即中止，宁停勿错。
    try:
        import compliance_guard
        _cg = compliance_guard.status()
    except Exception as e:
        # 计划九：合规检查自身异常不得静默放行，也不得静默拦截——打印后按不熔断继续
        print(f"   ⚠️ 合规熔断检查异常（按不熔断继续）: {e}")
        _cg = {"suspended": False}
    if _cg.get("suspended"):
        _msg = (f"合规熔断中，发布已中止：{_cg.get('reason', '')}"
                f"（自 {_cg.get('since', '')}）。人工定位原因后运行 "
                f"python3 compliance_guard.py --clear 解除，再手动补发本批次。")
        print(f"   ⛔ {_msg}")
        send_wxpusher("足球自媒体 ⛔ 合规熔断",
                      f"{date_str} 发布已中止：{_cg.get('reason', '')}")
        sys.exit(EXIT_FAIL)

    if not AUTH_FILE.exists():
        print("❌ 未找到登录状态，请先运行: python scripts/publisher.py --login")
        sys.exit(EXIT_FAIL)

    _skip_records: list[dict] = []
    articles = load_articles(date_str, batch_filter=batch_filter, skip_report=_skip_records)
    print(f"📰 加载 {len(articles)} 篇文章, headless={headless}")
    print(f"🚀 启动浏览器...")

    publish_ok = 0
    publish_fail = 0
    publish_details = []
    records: list[dict] = list(_skip_records)

    with sync_playwright() as p:
        launch_args = []
        if headless:
            # Anti-detection args for headless mode
            launch_args = [
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        browser = p.chromium.launch(headless=headless, args=launch_args)
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
            "storage_state": str(AUTH_FILE),
            "permissions": ["clipboard-read", "clipboard-write"],
        }
        if headless:
            ctx_kwargs["user_agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        # Auto-dismiss any "leave page" dialogs
        page.on("dialog", lambda dialog: dialog.accept())

        # Check if auth is still valid by navigating to publish page
        page.goto(TOUTIAO_PUBLISH, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # If redirected to login, auth expired
        current_url = page.url.lower()
        if "/auth/" in current_url or "/login" in current_url:
            print("⚠️  登录已过期，需要重新登录")
            print(f"   当前URL: {page.url[:100]}")
            print("   运行: python scripts/publisher.py --login")
            browser.close()
            send_wxpusher("足球自媒体 ⚠️", f"{date_str} 头条号登录已过期，发布中止")
            sys.exit(EXIT_FAIL)

        print(f"✅ 已登录头条号 (URL: {page.url[:80]})")

        print(f"✅ 已登录头条号，开始发布 {len(articles)} 篇文章...")

        for article in articles:
            try:
                result = publish_article(page, article, date_str, draft_mode)
                title_short = article['title'][:35]
                _pid = str(result.get("pgc_id") or "").strip()
                # 正文内嵌图数量 = 头条「封面模式」的判定依据：0 张 → 发布时主动选「无封面」。
                # 2026-11-01 体检：10 月已发布篇里 37% 走的就是这条路，这类篇的聚合点击率
                # 0.52%，有封面的 2.07%（差 4 倍）。这个数字必须进台账，否则无封面率永远是黑的。
                _img_n = len(extract_images(article.get("body") or ""))
                _rec = {"title": article["title"],
                        "slug": (article.get("meta") or {}).get("slug") or "",
                        "batch_name": article.get("batch_name") or "",
                        "platform_id": _pid, "reason": "", "images": _img_n}
                if result.get("ok"):
                    print(f"  ✅ [{article['index']}] {title_short}")
                    publish_ok += 1
                    publish_details.append(f"✅ {title_short}")
                    _rec["status"] = "published"
                    if not _pid:
                        # 发布成功但平台没返回作品 ID：这篇的数据以后只能靠导出标题反查，
                        # 过 7 天窗口就永久缺失。**必须点名**，不能只报「发布成功」。
                        print(f"       ⚠️ 未取得 platform_id，该篇绩效将依赖导出回填")
                        _rec["reason"] = "平台未返回 pgc_id，需靠导出回填"
                    records.append(_rec)
                else:
                    err = result.get("error", "未知错误")
                    print(f"  ⚠️  [{article['index']}] 跳过: {title_short} — {err}")
                    publish_fail += 1
                    publish_details.append(f"⚠️ {title_short}: {err}")
                    _rec["status"] = "failed"
                    _rec["reason"] = str(err)[:200]
                    records.append(_rec)
                # Longer delay between articles to ensure clean state
                print(f"  ⏳ 等待页面稳定...")
                time.sleep(5)
            except Exception as e:
                print(f"  ❌ 发布异常: {e}")
                print(f"     跳过: {article['title'][:40]}")
                publish_fail += 1
                publish_details.append(f"❌ {article['title'][:35]}: {e}")
                records.append({"status": "failed", "title": article["title"],
                                "slug": (article.get("meta") or {}).get("slug") or "",
                                "batch_name": article.get("batch_name") or "",
                                "platform_id": "", "reason": f"异常: {e}"[:200],
                                "images": len(extract_images(article.get("body") or ""))})

        browser.close()

    # ============================================================
    # 发布台账 + platform_id 绑定（2026-11-01 新增）
    # ============================================================
    # 此前这里什么都不落：结果只拼成一段人话发 WxPusher，然后就没了。
    # 结果是「成稿 242 篇 / 实际发布 93 篇」这种 62% 漏斗一个月都没人发现。
    # 现在每篇都留归宿（published / failed / skipped_prediction）并当场绑 ID。
    _binding: dict = {}
    _ledger_path = None
    try:
        _bind_list = [{"slug": r.get("slug"), "title": r.get("title"),
                       "platform_id": r.get("platform_id"),
                       "source": "publish_response"}
                      for r in records
                      if r.get("status") == "published" and r.get("platform_id")]
        if _bind_list:
            _binding = bind_platform_ids(date_str, _bind_list)
            print(f"  🔗 platform_id 绑定：成功 {_binding.get('bound', 0)} 篇"
                  + (f"，未匹配 {len(_binding.get('unmatched') or [])} 篇"
                     if _binding.get("unmatched") else ""))
        _ledger_path = write_publish_ledger(date_str, records,
                                            batch_filter=batch_filter, binding=_binding)
        try:
            _shown = _ledger_path.relative_to(PROJECT_ROOT)
        except ValueError:      # 审计目录被指到仓库外（测试/冒烟）→ 记绝对路径，别误报失败
            _shown = _ledger_path
        print(f"  📒 发布台账已落盘：{_shown}")
    except Exception as e:
        # 台账写失败不得影响发布结论，但必须可见——否则又会变成「悄悄丢掉」
        print(f"  ⚠️ 发布台账写入失败（不影响已发布结果）: {e}")

    _no_id = sum(1 for r in records if r.get("status") == "published" and not r.get("platform_id"))
    _id_warn = (f"\n⚠️ {_no_id} 篇发布成功但未取得作品 ID（绩效需靠导出回填，超 7 天永久缺失）"
                if _no_id else "")

    summary = f"发布 {publish_ok}/{publish_ok + publish_fail} 篇\n" + "\n".join(publish_details) + _id_warn

    print(f"\n{'='*60}")
    print(f"发布完成! {summary}")
    print(f"{'='*60}")

    # Send WxPusher notification after publishing
    if publish_fail == 0:
        send_wxpusher("足球自媒体 ✅", f"{date_str} 发布完成\n\n{summary}")
    elif publish_ok > 0:
        send_wxpusher("足球自媒体 ⚠️", f"{date_str} 部分发布成功\n\n{summary}")
    else:
        send_wxpusher("足球自媒体 ❌", f"{date_str} 发布全部失败\n\n{summary}")

    # 全部失败必须体现在退出码上：此前这里一律 return，CI 看到 rc=0 就当发布了，
    # 于是「2 篇全发失败」在流水线上长得和「2 篇全发成功」一模一样。
    return EXIT_FAIL if (publish_fail > 0 and publish_ok == 0) else EXIT_OK


def _emit_no_publishable(date_str, batch_filter, exc):
    """「本批 0 篇」的统一收尾：人话日志 + 台账留证 + Actions 注解。

    三件事一件都不能省：日志给当场的人看，台账给一个月后复盘的人看，
    注解给只瞄一眼 Actions 界面的人看（绿勾/红叉旁边直接写明原因）。
    """
    print(f"\n{'='*60}")
    print(f"⚠️  本批无可发布稿件（{exc.code}）：{exc.detail}")
    print(f"    退出码 {exc.exit_code}："
          + ("重跑不会有不同结果，本批按「无事可做」收尾"
             if exc.code == "all_skipped" else "上游没产出，留给兜底 cron 重跑"))
    print(f"{'='*60}")
    try:
        p = write_publish_ledger(date_str, exc.skipped, batch_filter=batch_filter,
                                 no_publishable={"code": exc.code, "detail": exc.detail})
        try:
            p = p.relative_to(PROJECT_ROOT)
        except ValueError:
            pass
        print(f"  📒 无稿台账已落盘：{p}")
    except Exception as e:
        print(f"  ⚠️ 无稿台账写入失败（不影响结论）: {e}")
    print(f"::warning::本批无可发布稿件（{exc.code}）：{exc.detail}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(EXIT_FAIL)

    if sys.argv[1] == "--login":
        login_and_save_auth()
    else:
        date_str = sys.argv[1]
        draft_mode = "--draft" in sys.argv
        headless = "--headless" in sys.argv
        batch_filter = None
        for arg in sys.argv:
            if arg.startswith("--batch="):
                batch_filter = arg.split("=", 1)[1]
        try:
            _rc = publish_all(date_str, draft_mode, headless=headless, batch_filter=batch_filter)
        except NoPublishableBatch as _e:
            _emit_no_publishable(date_str, batch_filter, _e)
            sys.exit(_e.exit_code)
        sys.exit(int(_rc or EXIT_OK))
