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

import os, sys, time, json, re
from pathlib import Path
from urllib.parse import parse_qs, unquote
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).parent
OUTPUT_BASE = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
AUTH_FILE = Path(os.environ.get("TOUTIAO_AUTH_FILE", PROJECT_ROOT / "toutiao_auth.json"))

# Toutiao URLs
TOUTIAO_LOGIN = "https://mp.toutiao.com/auth/page/login/"
TOUTIAO_PUBLISH = "https://mp.toutiao.com/profile_v4/graphic/publish"


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

        context.storage_state(path=str(AUTH_FILE))
        print(f"\n✅ 登录状态已保存至: {AUTH_FILE}")
        browser.close()


def load_articles(date_str):
    """Load generated articles from output directory."""
    date_dir = OUTPUT_BASE / date_str
    if not date_dir.exists():
        print(f"❌ 文章目录不存在: {date_dir}")
        sys.exit(1)

    articles = []
    for md_file in sorted(date_dir.glob("article-*.md")):
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
        })

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


def fill_prosemirror(page, text_content, selector='.ProseMirror'):
    """Fill ProseMirror editor via direct EditorView transaction.

    Finds the ProseMirror EditorView through React fiber walk, then uses
    view.dispatch(tr) to replace the document content with properly
    built ProseMirror nodes. This syncs internal state with the DOM.
    """
    # Split into paragraphs and escape HTML entities
    paragraphs = []
    for para in text_content.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        # HTML-escape the paragraph text
        para_escaped = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        paragraphs.append(para_escaped)

    # Also build HTML for innerHTML fallback
    html = "".join(f"<p>{p}</p>" for p in paragraphs)

    result = page.evaluate(
        """
        ([selector, html, paragraphs]) => {
            const el = document.querySelector(selector);
            if (!el) return {ok: false, error: 'editor not found'};

            // Find EditorView via React fiber
            """
        + _find_editor_view_js()
        + """
            if (!view) return {ok: false, error: 'EditorView not found'};

            try {
                const {state} = view;
                const {schema} = state;

                // Build paragraph nodes
                const paraNodes = [];
                for (const p of paragraphs) {
                    if (p) {
                        paraNodes.push(schema.nodes.paragraph.create(null, schema.text(p)));
                    }
                }
                if (paraNodes.length === 0) {
                    paraNodes.push(schema.nodes.paragraph.create(null));
                }

                // Create doc node
                const docNode = schema.nodes.doc.create(null, paraNodes);

                // Replace entire document content
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
    return result


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
    """Close any AI assistant drawers or popups that block the editor."""
    try:
        # Close AI assistant drawer if present
        close_btns = page.locator('.byte-drawer-wrapper .byte-icon-close, .byte-drawer .byte-icon-close')
        if close_btns.count() > 0:
            for i in range(min(close_btns.count(), 3)):
                try:
                    close_btns.nth(i).click(timeout=2000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass

        # Click mask to dismiss
        mask = page.locator('.byte-drawer-mask')
        if mask.is_visible(timeout=1000):
            try:
                mask.click(timeout=2000)
                page.wait_for_timeout(500)
            except Exception:
                pass
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

    # Navigate to publish page — use networkidle + reload for clean state
    page.goto(TOUTIAO_PUBLISH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    # Force reload to ensure clean editor state (avoids cached page issues)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(3000)

    # Close AI assistant drawer
    dismiss_overlays(page)
    page.wait_for_timeout(1000)

    # Verify editor is actually ready before interacting
    try:
        page.locator('.ProseMirror').first.wait_for(state="visible", timeout=10000)
    except Exception:
        print(f"  ⚠️  ProseMirror 编辑器未就绪，尝试继续...")

    # === Fill Title (contenteditable div, not input) ===
    # Use execCommand('insertText') which triggers beforeinput events that
    # ProseMirror detects and uses to sync its internal document state.
    title_result = fill_title(page, title)
    if title_result.get("ok") and title_result.get("textLen", 0) > 0:
        print(f"  ✅ 标题已填入 ({title_result['textLen']} 字)")
    else:
        print(f"  ❌ 无法填入标题: {title_result}")
        return False

    # === Fill Content (ProseMirror editor) ===
    pm_result = fill_prosemirror(page, text_body)
    if pm_result.get("ok") and pm_result.get("textLen", 0) > 0:
        print(f"  ✅ 正文已填入 ({pm_result['textLen']} 字, {pm_result.get('hasPTags', 0)} 段)")
    else:
        print(f"  ❌ 无法填入正文: {pm_result}")
        return False

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

        return True
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
                    print(f"\n  📡 === PUBLISH RESPONSE ===")
                    print(f"  Status: {response.status}")
                    print(f"  Code: {code}")
                    print(f"  Message: {msg}")
                    result = {"code": code, "message": msg}
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

                # Click confirmation button in the dialog
                confirmed = False
                for btn_text in ["发布", "确认发布", "确定"]:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}")').last
                        if btn.is_visible(timeout=2000):
                            btn.click(force=True)
                            print(f"  ✅ 已确认: {btn_text}")
                            confirmed = True
                            break
                    except Exception:
                        continue

                if not confirmed:
                    print(f"  ⚠️  未找到确认按钮")
                    return False

                # Poll for publish result (up to 30s)
                for _ in range(30):
                    page.wait_for_timeout(1000)
                    if any(r["code"] == 0 for r in publish_results):
                        return True

                # Timed out — check if any request succeeded
                success = any(r["code"] == 0 for r in publish_results)
                if not success:
                    codes = [r["code"] for r in publish_results]
                    print(f"  ❌ 发布未确认成功, 收到的响应码: {codes}")
                return success

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

    return False


def publish_all(date_str, draft_mode=False, headless=False):
    """Main publish flow."""
    if not AUTH_FILE.exists():
        print("❌ 未找到登录状态，请先运行: python scripts/publisher.py --login")
        sys.exit(1)

    articles = load_articles(date_str)
    print(f"📰 加载 {len(articles)} 篇文章, headless={headless}")
    print(f"🚀 启动浏览器...")

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
        page.goto(TOUTIAO_PUBLISH, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        # If redirected to login, auth expired
        current_url = page.url.lower()
        if "/auth/" in current_url or "/login" in current_url:
            print("⚠️  登录已过期，需要重新登录")
            print(f"   当前URL: {page.url[:100]}")
            print("   运行: python scripts/publisher.py --login")
            browser.close()
            sys.exit(1)

        print(f"✅ 已登录头条号 (URL: {page.url[:80]})")

        print(f"✅ 已登录头条号，开始发布 {len(articles)} 篇文章...")

        for article in articles:
            try:
                ok = publish_article(page, article, date_str, draft_mode)
                if ok:
                    print(f"  ✅ [{article['index']}] {article['title'][:40]}")
                else:
                    print(f"  ⚠️  [{article['index']}] 跳过: {article['title'][:40]}")
                # Longer delay between articles to ensure clean state
                print(f"  ⏳ 等待页面稳定...")
                time.sleep(5)
            except Exception as e:
                print(f"  ❌ 发布异常: {e}")
                print(f"     跳过: {article['title'][:40]}")

        browser.close()

    print(f"\n{'='*60}")
    print(f"发布完成! 共处理 {len(articles)} 篇文章")
    print(f"{'='*60}")


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
        publish_all(date_str, draft_mode, headless=headless)
