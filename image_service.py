"""图片服务模块 - 下载、尺寸闸门、格式转换、MD5 去重

只保留生产链路真正在用的两个方法：`download_image` / `_download_one`
（调用方 `orchestrator.py:2227/2397`）。

2026-11-11 删掉的三条旧路径（均 0 生产调用方，详见
`docs/audit/2026-11-11-不动项清理清单.md`）：
  - `search_images` / `download_article_images`：整篇批量下图，早已被
    `data_collector.search_images`（四层配图链）+ orchestrator 自己的循环取代；
  - `capture_match_screenshot`：Playwright 截图，全仓 0 调用，且写 /tmp（本机 10MB tmpfs）；
  - `_compute_phash`：感知哈希，**没有消费者、也没有落盘**（metadata 只存 local_path），
    且是全仓唯一的 `Image.getdata()` 调用点（Pillow 14 于 2027-10-15 移除该 API）。
    去重一直走 MD5（`_compute_hash`），跨篇不共享集合属既有行为，不是本次改动引入。
"""

import io
import hashlib
import requests
from pathlib import Path
from typing import Optional
from PIL import Image, UnidentifiedImageError


class ImageService:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        img_config = self.config.get("images", {})

        self.min_width = img_config.get("min_width", 800)
        self.min_height = img_config.get("min_height", 600)
        self.max_size = img_config.get("max_size_bytes", 5 * 1024 * 1024)
        self.min_size = img_config.get("min_size_bytes", 50 * 1024)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        # Short timeout for image operations so they don't block the save
        self.download_timeout = 8

    def _compute_hash(self, image_data: bytes) -> str:
        """计算图片的 MD5 哈希"""
        return hashlib.md5(image_data).hexdigest()

    def download_image(self, url: str, target_dir: Path, prefix: str, index: int,
                       existing_hashes: set, min_width: Optional[int] = None,
                       min_height: Optional[int] = None,
                       fallback_url: Optional[str] = None) -> Optional[dict]:
        """
        下载并处理单张图片

        min_width/min_height: 覆盖实例默认闸门。**封面还是正文图，闸门不一样**：
            封面（`article-N-img-001.jpg`）头条会拿去做封面，必须 800×600；
            正文插图渲染宽度只有 ~640px，新闻源图床的 720×480 / 644×476 完全够用，
            用 800 去卡会把「新闻源正文图」这条精度最高的链路整批拒掉
            （2026-11-05 实测：直播吧正文图 `_1280` 版本 1280×853 过闸门，
            但「无尺寸后缀」的原图只有 644×476，一拒就是 0 图）。
        fallback_url: 主 URL 失败时回退用的地址（新闻图床的 `_720` 版本）。

        Returns:
            {local_path, filename, url, md5, width, height} or None on failure
        """
        got = self._download_one(url, target_dir, prefix, index, existing_hashes,
                                 min_width, min_height)
        if got is None and fallback_url and fallback_url != url:
            got = self._download_one(fallback_url, target_dir, prefix, index,
                                     existing_hashes, min_width, min_height)
            if got is not None:
                got["url"] = url                   # 台账记原始地址，可回溯到出处
        return got

    def _download_one(self, url: str, target_dir: Path, prefix: str, index: int,
                      existing_hashes: set, min_width: Optional[int],
                      min_height: Optional[int]) -> Optional[dict]:
        """单次下载（不含回退），供 `download_image` 调用。"""
        min_w = self.min_width if min_width is None else int(min_width)
        min_h = self.min_height if min_height is None else int(min_height)
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
            if width < min_w or height < min_h:
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

            return {
                "local_path": str(filepath),
                "filename": filename,
                "url": url,
                "md5": md5_hash,
                "width": img.width,
                "height": img.height,
            }

        except Exception:
            return None

