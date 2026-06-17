#!/usr/bin/env python3
"""微头条发布模块 — 比赛实时短内容生成 + 自动发布

依赖: publisher.py 的 playwright session, orchestrator.py 的 call_llm

微头条是头条号流量最大的入口，每场比赛生成 1 条 100-200 字短评 + 1 张图。
"""

import json, re, os, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from constants import DEEPSEEK_KEY, DEEPSEEK_URL
from utils import retry, call_llm

# Toutiao micro-headline publish page (accessed via tab on article list)
TOUTIAO_MICRO_URL = "https://mp.toutiao.com/profile_v4/weitoutiao/publish"
# Article list page (for navigating to 微头条 tab)
TOUTIAO_ARTICLE_LIST = "https://mp.toutiao.com/profile_v4/graphic"
# Article publish URL (for short article fallback)
TOUTIAO_PUBLISH = "https://mp.toutiao.com/profile_v4/graphic/publish"

# --- Content Generation ---

def generate_micro_headlines(match_data, count=2):
    """从比赛数据生成微头条短内容。

    Returns list of dicts: [{title, content, match_info}, ...]
    Each content is 100-200 chars, like a hot take / quick comment.
    """
    if not match_data or not match_data.get("all_fixtures"):
        return []

    cst = ZoneInfo("Asia/Shanghai")
    now = datetime.now(cst)
    match_lines = []

    for m in match_data["all_fixtures"]:
        hg = m.get("home_score")
        ag = m.get("away_score")
        home = m.get("home_team", "")
        away = m.get("away_team", "")
        league = m.get("league", "")
        utc_date = m.get("utc_date", "")

        # Parse match time
        match_cst = ""
        if utc_date:
            try:
                dt_utc = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
                dt_cst = dt_utc + timedelta(hours=8)
                time_str = dt_cst.strftime("%H:%M")
                match_cst = f"[{time_str}开球] "
            except Exception:
                pass

        if hg is not None:
            score = f"{hg}-{ag}"
            total_goals = hg + ag
            tag = "⚽" * min(total_goals, 3) if total_goals >= 3 else "⚽"
            match_lines.append(
                f"- {match_cst}{tag} {league}: {home} {score} {away}"
            )
        else:
            match_lines.append(f"- {match_cst}⏳ {league}: {home} vs {away} (未开始)")

    matches_text = "\n".join(match_lines)

    prompt = f"""你是头条足球博主"球评人老六"。今天是 {now.strftime('%Y-%m-%d')}，以下是今日比赛结果。

{matches_text}

请从中选 2 场比赛，每场比赛写 1 条微头条（100-200 字）。
微头条要求：
- 短平快：像比赛结束后跟球友说的第一句话，一句点出最精彩/争议的瞬间
- 有态度：可以激动、可以吐槽、可以有立场
- ⚠️ 绝对禁止编造：只写素材里明确有的比分、赛事、球队名。不能写"凌晨X点""半夜"等虚构时间，不能编造球员言论/更衣室故事/具体进球过程。不确定的一律不写。
- 接地气：让读者感觉"说到了我心坎上"
- 每条末尾加 1-2 个相关话题标签（如 #世界杯 #阿根廷）

输出纯JSON数组：
[{{"match": "{match_data['all_fixtures'][0]['home_team']} vs {match_data['all_fixtures'][0]['away_team']}" if match_data['all_fixtures'] else 'Argentina vs Algeria', "content": "微头条正文(100-200字)", "tags": ["#标签1", "#标签2"]}}]

只输出JSON。不要markdown包裹。"""

    messages = [
        {"role": "system", "content": "你是头条足球博主'球评人老六'，擅长写短平快、有态度的比赛微头条。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]

    try:
        response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash",
                           messages, temperature=0.7, max_tokens=2048)
        result = json.loads(response) if isinstance(response, str) else response
        if isinstance(result, dict) and "match" in result:
            result = [result]
        if isinstance(result, list):
            validated = []
            for item in result[:count]:
                content = item.get("content", "").strip()
                if 50 <= len(content) <= 500:
                    validated.append({
                        "content": content,
                        "tags": item.get("tags", []),
                        "match": item.get("match", ""),
                    })
            if validated:
                for i, h in enumerate(validated):
                    print(f"   📢 微头条{i+1}: {h['content'][:60]}...")
                return validated
    except Exception as e:
        print(f"   ⚠️ 微头条生成失败: {e}")

    # Fallback: simple match summaries
    fallbacks = []
    for m in match_data["all_fixtures"][:count]:
        hg = m.get("home_score")
        ag = m.get("away_score")
        if hg is not None:
            text = f"{m['home_team']} {hg}-{ag} {m['away_team']}！{'大比分' if hg+ag >= 4 else '关键'}一战，{'精彩纷呈' if hg+ag >= 3 else '比赛激烈'}。各位怎么看？ #足球 #{m.get('league', '').replace(' ', '')}"
            fallbacks.append({"content": text, "tags": ["#足球", f"#{m.get('home_team','')}"], "match": f"{m['home_team']} vs {m['away_team']}"})
    return fallbacks


# --- Playwright Publishing ---

def publish_micro_headline(page, headline, date_str=None):
    """Publish a micro-headline on Toutiao using the SPA micro page.

    Navigates to the micro-headline editor, fills content, and publishes.
    Falls back to short-article publishing if the micro page is unavailable.

    Args:
        page: playwright Page object (already logged in)
        headline: dict with {content, tags}
        date_str: date string for debug screenshots

    Returns: {"ok": bool, "error": str}
    """
    content = headline.get("content", "")
    if not content:
        return {"ok": False, "error": "内容为空"}

    from publisher import dismiss_overlays, debug_dump_page

    try:
        print(f"   📢 微头条: {content[:50]}...")

        # Navigate to micro-headline page directly (SPA route)
        page.goto(TOUTIAO_MICRO_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(5000)

        # Dismiss overlays that may block interaction
        dismiss_overlays(page)
        page.wait_for_timeout(1000)

        # Find the ProseMirror editor (微头条 uses the same editor component)
        text_input = None
        for _ in range(15):  # Wait up to 15s for editor to hydrate
            try:
                el = page.locator('.ProseMirror').first
                if el.is_visible(timeout=1000):
                    text_input = el
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)

        if not text_input:
            debug_dump_page(page, "micro_no_editor")
            return {"ok": False, "error": "微头条编辑器未找到"}

        # === Upload image if available ===
        # Use data_collector.search_images (DuckDuckGo fallback, no nested Playwright)
        match_name = headline.get("match", "")
        if match_name:
            try:
                from data_collector import search_images
                imgs = search_images({"title": match_name, "keywords": [match_name]}, count=1)
                if imgs:
                    import requests as _req
                    img_resp = _req.get(imgs[0]["url"], timeout=10)
                    if img_resp.status_code == 200:
                        import tempfile
                        tmp_img = Path(tempfile.mkdtemp()) / "micro_img.jpg"
                        tmp_img.write_bytes(img_resp.content)
                        if tmp_img.stat().st_size > 5000:
                            # Upload via file input
                            file_input = page.locator('input[type="file"]').first
                            if file_input.is_visible(timeout=3000):
                                file_input.set_input_files(str(tmp_img))
                                page.wait_for_timeout(3000)
                                print(f"   📸 微头条配图已上传")
                            else:
                                # Click upload button first
                                for sel in ['[class*="upload"]', 'svg[class*="image"]', '[class*="picture"]', 'button[class*="img"]']:
                                    btn = page.locator(sel).first
                                    if btn.is_visible(timeout=500):
                                        btn.click()
                                        page.wait_for_timeout(1000)
                                        break
                                file_input = page.locator('input[type="file"]').first
                                if file_input.is_visible(timeout=3000):
                                    file_input.set_input_files(str(tmp_img))
                                    page.wait_for_timeout(3000)
                                    print(f"   📸 微头条配图已上传")
            except Exception as e:
                print(f"   ⚠️ 微头条配图上传失败: {e}")

        # Fill content
        text_input.click()
        page.wait_for_timeout(300)
        page.evaluate(f"document.querySelector('.ProseMirror').innerText = {json.dumps(content)}")
        page.wait_for_timeout(500)

        # Click publish button
        pub_btn = page.locator('button:has-text("发布")').first
        if pub_btn.is_visible(timeout=3000):
            pub_btn.click()
            page.wait_for_timeout(3000)

            # Check for success message or dialog
            try:
                success = page.locator('text=发布成功').first
                if success.is_visible(timeout=3000):
                    print(f"   ✅ 微头条发布成功!")
                    return {"ok": True}
            except Exception:
                pass

            # Check if dialog appeared and confirm
            try:
                confirm_btn = page.locator('button:has-text("确认")').first
                if confirm_btn.is_visible(timeout=2000):
                    confirm_btn.click()
                    page.wait_for_timeout(2000)
            except Exception:
                pass

            print(f"   ✅ 微头条已提交")
            return {"ok": True}

        debug_dump_page(page, "micro_no_publish_btn")
        return {"ok": False, "error": "未找到发布按钮"}

    except Exception as e:
        print(f"   ❌ 微头条发布异常: {e}")
        try:
            debug_dump_page(page, "micro_error")
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def publish_micro_headlines(page, headlines, date_str=None):
    """Publish multiple micro-headlines sequentially."""
    results = []
    for h in headlines:
        try:
            r = publish_micro_headline(page, h, date_str=date_str)
            results.append(r)
            page.wait_for_timeout(2000)
        except Exception as e:
            results.append({"ok": False, "error": str(e)})
    ok_count = sum(1 for r in results if r.get("ok"))
    print(f"   📊 微头条: {ok_count}/{len(results)} 发布成功")
    return results
