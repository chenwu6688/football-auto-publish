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
OUTPUT_BASE = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
AUTH_FILE = Path(os.environ.get("TOUTIAO_AUTH_FILE", PROJECT_ROOT / "toutiao_auth.json"))

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


def load_articles(date_str, batch_filter=None):
    """Load generated articles from output directory. Optionally filter by batch.

    Args:
        date_str: Date string YYYY-MM-DD
        batch_filter: If set, only load articles whose batch_name matches (e.g. 'morning').
                      If None or empty, load all articles (backward compatible).
    """
    date_dir = OUTPUT_BASE / date_str
    if not date_dir.exists():
        # Fallback: try previous day (scheduler delay may have shifted Beijing date)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            print(f"❌ 文章目录不存在: {date_dir}")
            sys.exit(1)
        from datetime import timedelta
        prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_dir = OUTPUT_BASE / prev_date
        if prev_dir.exists():
            print(f"⚠️  今日目录不存在，回退到昨日: {prev_dir}")
            date_dir = prev_dir
        else:
            print(f"❌ 文章目录不存在: {date_dir} (也尝试了 {prev_dir})")
            sys.exit(1)

    all_md_files = sorted(date_dir.glob("article-*.md"))
    print(f"在 {date_dir} 中找到 {len(all_md_files)} 个 article-*.md 文件: {[f.name for f in all_md_files]}")
    articles = []
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
            if before > 0:
                # 批次过滤 0 命中但有文章：说明 batch_name 缺失（如应急/预测文章未打标签）。
                # 降级发布全部文章，避免整批失败丢失内容。
                print(f"⚠️ 批次筛选 '{batch_filter}' 未匹配到文章 (目录共 {before} 篇，batch_name 可能缺失)")
                print(f"   为不丢失内容，降级发布全部 {before} 篇 (请检查 orchestrator 是否正确写入 batch_name)")
                articles = all_articles
            else:
                print(f"❌ 没有找到 {batch_filter} 批次的文章 (目录为空)，中止发布")
                sys.exit(1)

    if not articles:
        print(f"❌ {date_dir} 中没有找到文章")
        sys.exit(1)

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
                    return {"ok": True}

                # Poll for new publish result after confirmation (up to 15s).
                for _ in range(15):
                    page.wait_for_timeout(1000)
                    if any(r["code"] == 0 for r in publish_results):
                        return {"ok": True}

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
                                return {"ok": True}
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
                    return {"ok": False, "error": msg}
                codes = [(r["code"], r.get("message", "")[:30]) for r in publish_results]
                msg = f"发布失败, 响应: {codes}"
                print(f"  ❌ {msg}")
                return {"ok": False, "error": msg}

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

    return {"ok": False, "error": "发布流程异常终止"}


def publish_all(date_str, draft_mode=False, headless=False, batch_filter=None):
    """Main publish flow.

    Args:
        batch_filter: If set, only publish articles from this batch (morning/noon/evening).
    """
    if not AUTH_FILE.exists():
        print("❌ 未找到登录状态，请先运行: python scripts/publisher.py --login")
        sys.exit(1)

    articles = load_articles(date_str, batch_filter=batch_filter)
    print(f"📰 加载 {len(articles)} 篇文章, headless={headless}")
    print(f"🚀 启动浏览器...")

    publish_ok = 0
    publish_fail = 0
    publish_details = []

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
            sys.exit(1)

        print(f"✅ 已登录头条号 (URL: {page.url[:80]})")

        print(f"✅ 已登录头条号，开始发布 {len(articles)} 篇文章...")

        for article in articles:
            try:
                result = publish_article(page, article, date_str, draft_mode)
                title_short = article['title'][:35]
                if result.get("ok"):
                    print(f"  ✅ [{article['index']}] {title_short}")
                    publish_ok += 1
                    publish_details.append(f"✅ {title_short}")
                else:
                    err = result.get("error", "未知错误")
                    print(f"  ⚠️  [{article['index']}] 跳过: {title_short} — {err}")
                    publish_fail += 1
                    publish_details.append(f"⚠️ {title_short}: {err}")
                # Longer delay between articles to ensure clean state
                print(f"  ⏳ 等待页面稳定...")
                time.sleep(5)
            except Exception as e:
                print(f"  ❌ 发布异常: {e}")
                print(f"     跳过: {article['title'][:40]}")
                publish_fail += 1
                publish_details.append(f"❌ {article['title'][:35]}: {e}")

        browser.close()

    summary = f"发布 {publish_ok}/{publish_ok + publish_fail} 篇\n" + "\n".join(publish_details)

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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

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
        publish_all(date_str, draft_mode, headless=headless, batch_filter=batch_filter)
