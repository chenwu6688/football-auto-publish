"""图片服务模块 - 搜索、下载、验证、去重"""

import os, re
import io
import hashlib
import time
import requests
from pathlib import Path
from typing import Optional
from PIL import Image, UnidentifiedImageError
from urllib.parse import quote_plus


class ImageService:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        img_config = self.config.get("images", {})

        self.min_width = img_config.get("min_width", 800)
        self.min_height = img_config.get("min_height", 600)
        self.max_size = img_config.get("max_size_bytes", 5 * 1024 * 1024)
        self.min_size = img_config.get("min_size_bytes", 50 * 1024)
        self.max_per_article = img_config.get("max_per_article", 5)
        self.required = img_config.get("required_per_article", 3)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        # Short timeout for image operations so they don't block the save
        self.search_timeout = 5
        self.download_timeout = 8

    def search_images(self, query: str, count: int = 8) -> list:
        """
        搜索图片，返回URL列表。
        优先级：Unsplash → DuckDuckGo（免费方案）
        """
        results = []
        query_encoded = quote_plus(query)

        # Source 1: Unsplash (CC0 license)
        try:
            unsplash_key = os.environ.get("UNSPLASH_ACCESS_KEY", "")
            if unsplash_key:
                url = f"https://api.unsplash.com/search/photos?query={query_encoded}&per_page={count}&orientation=landscape"
                resp = self.session.get(url, headers={"Authorization": f"Client-ID {unsplash_key}"}, timeout=self.search_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("results", []):
                        results.append({
                            "url": item["urls"]["regular"],
                            "source": "unsplash",
                            "description": item.get("description") or item.get("alt_description", ""),
                            "width": item.get("width", 0),
                            "height": item.get("height", 0),
                            "author": item.get("user", {}).get("name", "Unsplash"),
                        })
        except Exception:
            pass

        # Source 2: DuckDuckGo Images (free, no key, always tried as fallback)
        if len(results) < count:
            try:
                vqd_url = "https://duckduckgo.com/"
                resp = self.session.get(vqd_url, params={"q": f"{query} football soccer"}, timeout=10)
                import re
                match = re.search(r'vqd=([\d-]+)', resp.text)
                if match:
                    vqd = match.group(1)
                    img_url = f"https://duckduckgo.com/i.js?q={query_encoded}+football+soccer&vqd={vqd}&o=json"
                    resp = self.session.get(img_url, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("results", [])[:count]:
                            results.append({
                                "url": item.get("image", ""),
                                "source": "duckduckgo",
                                "description": item.get("title", ""),
                                "width": item.get("width", 0),
                                "height": item.get("height", 0),
                                "author": "DuckDuckGo",
                            })
            except Exception:
                pass

        # Source 3: Bing Image Search (requires API key)
        bing_key = os.environ.get("BING_SEARCH_KEY", "")
        if len(results) < count and bing_key:
            try:
                bing_url = "https://api.bing.microsoft.com/v7.0/images/search"
                resp = self.session.get(bing_url, params={
                    "q": f"{query} football soccer",
                    "count": count,
                    "imageType": "Photo",
                    "license": "ShareCommercially",
                }, headers={"Ocp-Apim-Subscription-Key": bing_key}, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("value", [])[:count]:
                        results.append({
                            "url": item.get("contentUrl", ""),
                            "source": "bing",
                            "description": item.get("name", ""),
                            "width": item.get("width", 0),
                            "height": item.get("height", 0),
                            "author": item.get("hostPageDisplayUrl", "Bing"),
                        })
            except Exception:
                pass

        return results

    def _compute_hash(self, image_data: bytes) -> str:
        """计算图片的 MD5 哈希"""
        return hashlib.md5(image_data).hexdigest()

    def _compute_phash(self, image: Image.Image) -> str:
        """简单的感知哈希，用于去重"""
        img = image.resize((32, 32), Image.LANCZOS).convert("L")
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if p > avg else "0" for p in pixels)
        return hex(int(bits, 2))[2:]

    def download_image(self, url: str, target_dir: Path, prefix: str, index: int,
                       existing_hashes: set) -> Optional[dict]:
        """
        下载并处理单张图片

        Returns:
            {local_path, url, hash, width, height} or None on failure
        """
        try:
            resp = self.session.get(url, timeout=15, stream=True)
            resp.raise_for_status()

            # 检查大小
            content_length = int(resp.headers.get("content-length", 0))
            if content_length > self.max_size or (0 < content_length < self.min_size):
                return None

            # 读取数据
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > self.max_size:
                    return None
                chunks.append(chunk)

            image_data = b"".join(chunks)

            # 检查最小大小
            if len(image_data) < self.min_size:
                return None

            # MD5 去重
            md5_hash = self._compute_hash(image_data)
            if md5_hash in existing_hashes:
                return None

            # 打开图片验证
            try:
                img = Image.open(io.BytesIO(image_data))
                img.verify()
                img = Image.open(io.BytesIO(image_data))  # reopen after verify
            except (UnidentifiedImageError, Exception):
                return None

            # 检查尺寸
            width, height = img.size
            if width < self.min_width or height < self.min_height:
                return None

            # 检查格式，转 JPEG
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")

            # 缩放（最大宽度1200px）
            if width > 1200:
                ratio = 1200 / width
                new_size = (1200, int(height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # 保存
            filename = f"{prefix}-{index:03d}.jpg"
            filepath = target_dir / filename
            img.save(filepath, "JPEG", quality=85)

            # 感知哈希
            phash = self._compute_phash(img)

            return {
                "local_path": str(filepath),
                "filename": filename,
                "url": url,
                "md5": md5_hash,
                "phash": phash,
                "width": img.width,
                "height": img.height,
            }

        except Exception:
            return None

    def download_and_crop_image(self, url: str, target_dir: Path, prefix: str, index: int,
                                 crop_bottom_ratio: float = 0.10) -> Optional[dict]:
        """Download an image and crop the bottom portion to remove watermarks.

        Hupu images have a watermark at the bottom ~8-12%. This crops the
        bottom portion then saves as JPEG. Returns same dict as download_image.
        """
        try:
            resp = self.session.get(url, timeout=15, stream=True)
            resp.raise_for_status()

            content_length = int(resp.headers.get("content-length", 0))
            if content_length > self.max_size or (0 < content_length < self.min_size):
                return None

            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > self.max_size:
                    return None
                chunks.append(chunk)

            image_data = b"".join(chunks)
            if len(image_data) < self.min_size:
                return None

            md5_hash = self._compute_hash(image_data)

            try:
                img = Image.open(io.BytesIO(image_data))
                img.verify()
                img = Image.open(io.BytesIO(image_data))
            except (UnidentifiedImageError, Exception):
                return None

            width, height = img.size
            if width < self.min_width or height < self.min_height:
                return None

            # Convert to RGB if needed
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")

            # Crop bottom portion to remove Hupu watermark
            crop_px = max(int(height * crop_bottom_ratio), 25)
            crop_bottom = height - crop_px
            if crop_bottom > 0:
                img = img.crop((0, 0, width, crop_bottom))

            # Resize if too wide
            new_width, new_height = img.size
            if new_width > 1200:
                ratio = 1200 / new_width
                img = img.resize((1200, int(new_height * ratio)), Image.LANCZOS)

            filename = f"{prefix}-{index:03d}.jpg"
            filepath = target_dir / filename
            img.save(filepath, "JPEG", quality=85)

            phash = self._compute_phash(img)

            return {
                "local_path": str(filepath),
                "filename": filename,
                "url": url,
                "md5": md5_hash,
                "phash": phash,
                "width": img.width,
                "height": img.height,
                "source": "hupu",
                "description": "",
            }

        except Exception:
            return None

    def download_article_images(self, query: str, article_index: int,
                                date_str: str, base_dir: str = "/home/chenwu/每日自媒体文案") -> list:
        """为一篇文章下载并保存图片的完整流程"""
        images_dir = Path(base_dir) / date_str / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        prefix = f"article-{article_index}-img"

        search_results = self.search_images(query, count=self.max_per_article)

        downloaded = []
        existing_hashes = set()

        for img_result in search_results:
            if len(downloaded) >= self.required:
                break
            url = img_result.get("url", "")
            if not url:
                continue
            result = self.download_image(
                url=url, target_dir=images_dir, prefix=prefix,
                index=len(downloaded) + 1, existing_hashes=existing_hashes)
            if result:
                result["source"] = img_result.get("source", "unknown")
                result["description"] = img_result.get("description", "")
                result["author"] = img_result.get("author", "")
                downloaded.append(result)
                existing_hashes.add(result["md5"])
            time.sleep(0.5)

        return downloaded

    def capture_match_screenshot(self, home_team: str, away_team: str, league: str = "",
                                  save_dir: Optional[Path] = None) -> Optional[dict]:
        """Use Playwright to screenshot a match score page from a football stats site.

        Tries Dongqiudi (懂球帝) first, then falls back to flashscore.
        Returns dict with {local_path, filename, source, url} or None.

        This guarantees at least 1 real match image per article, unlike
        external image APIs which often return empty results.
        """
        import subprocess
        from playwright.sync_api import sync_playwright

        save_path = Path(save_dir) if save_dir else Path("/tmp/match_screenshots")
        save_path.mkdir(parents=True, exist_ok=True)

        # Build search query for Dongqiudi
        query = f"{home_team} {away_team}".strip()
        safe_name = re.sub(r'[^a-zA-Z0-9_一-鿿]', '_', query)[:30]
        filename = f"match_{safe_name}_{int(time.time())}.jpg"
        filepath = save_path / filename

        urls_to_try = [
            f"https://www.dongqiudi.com/search?q={query.replace(' ', '+')}",
            f"https://www.flashscore.com/search/?q={query.replace(' ', '+')}",
        ]

        for url in urls_to_try:
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1280, "height": 720})
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)

                    # Try to find a match result card/score element
                    score_selectors = [
                        '.match-result',
                        '.score',
                        '.result-content',
                        '.match-item',
                        '[class*="score"]',
                        '[class*="match"]',
                        '.search-result-item',
                        'table',
                        '.live-item',
                    ]

                    screenshot_taken = False
                    for sel in score_selectors:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=2000):
                                el.screenshot(path=str(filepath))
                                screenshot_taken = True
                                break
                        except Exception:
                            continue

                    if not screenshot_taken:
                        # Full page screenshot as fallback
                        page.screenshot(path=str(filepath), full_page=True)

                    browser.close()

                    if filepath.exists() and filepath.stat().st_size > 10000:
                        print(f"   📸 截图成功: {filename} ({filepath.stat().st_size // 1024}KB)")
                        return {
                            "local_path": str(filepath),
                            "filename": filename,
                            "source": "screenshot",
                            "url": url,
                        }
            except Exception as e:
                print(f"   ⚠️ 截图失败 ({url[:30]}...): {e}")
                continue

        print(f"   ❌ 截图失败: {home_team} vs {away_team} (所有源都不可用)")
        return None
