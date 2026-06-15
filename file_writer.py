"""文件写入模块 - 负责将文章和图片保存到文件系统"""

import os
import json
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional


class FileWriter:
    def __init__(self, base_dir: str = "/home/chenwu/每日自媒体文案"):
        self.base_dir = Path(base_dir)

    def _slugify(self, title: str) -> str:
        """将中文标题转为英文slug"""
        # 简单处理：取标题前几个字 + hash
        clean = re.sub(r'[^\w一-鿿]', '', title)
        short = clean[:8] if len(clean) > 8 else clean
        h = hashlib.md5(title.encode()).hexdigest()[:6]
        return f"{short}-{h}"

    def get_date_dir(self, date_str: str) -> Path:
        """获取日期对应的目录路径"""
        return self.base_dir / date_str

    def create_date_directory(self, date_str: str) -> Path:
        """创建日期目录和图片子目录"""
        date_dir = self.get_date_dir(date_str)
        images_dir = date_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        return date_dir

    def save_article(self, date_str: str, index: int, article_data: dict) -> dict:
        """
        保存单篇文章

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            index: 文章序号 (1-3)
            article_data: {title, content, tags, images, category, keywords}

        Returns:
            {success, article_path, image_paths, slug}
        """
        date_dir = self.create_date_directory(date_str)
        images_dir = date_dir / "images"

        title = article_data.get("title", f"未命名文章{index}")
        slug = self._slugify(title)
        filename = f"article-{index}-{slug}.md"
        article_path = date_dir / filename

        content = article_data.get("content", "")
        tags = article_data.get("tags", [])
        category = article_data.get("category", "足球")
        keywords = article_data.get("keywords", [])

        # 构建 Markdown 文件内容
        tag_str = ", ".join(tags) if tags else ""
        kw_str = ", ".join(keywords) if keywords else ""
        sources_used = article_data.get("sources_used", [])
        sources_str = ", ".join(sources_used) if sources_used else ""
        originality_note = article_data.get("originality_note", "")

        batch_name = article_data.get("batch_name", "")
        batch_time = article_data.get("batch_time", "")

        md_content = f"""---
title: "{title}"
date: {date_str}
category: {category}
tags: [{tag_str}]
keywords: [{kw_str}]
article_index: {index}
batch_name: {batch_name}
batch_time: {batch_time}
sources_used: [{sources_str}]
originality_note: "{originality_note}"
---

# {title}

{content}

---
*球评人老六 · {date_str}*
"""
        article_path.write_text(md_content, encoding="utf-8")

        # 记录已保存的图片
        image_paths = []
        if "downloaded_images" in article_data:
            for img_info in article_data["downloaded_images"]:
                image_paths.append(str(img_info.get("local_path", "")))

        return {
            "success": True,
            "article_path": str(article_path),
            "image_paths": image_paths,
            "slug": slug,
        }

    def save_index(self, date_str: str, articles_meta: list) -> str:
        """
        生成每日索引页

        Args:
            date_str: 日期字符串
            articles_meta: [{title, slug, path, tags, keywords}, ...]
        """
        date_dir = self.get_date_dir(date_str)
        date_dir.mkdir(parents=True, exist_ok=True)
        index_path = date_dir / "index.md"

        lines = [
            f"# 每日自媒体文案 - {date_str}",
            "",
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"共 {len(articles_meta)} 篇文章",
            "",
            "---",
            "",
            "## 文章列表",
            "",
        ]

        for i, meta in enumerate(articles_meta, 1):
            title = meta.get("title", f"文章{i}")
            slug = meta.get("slug", "")
            tags = meta.get("tags", [])
            keywords = meta.get("keywords", [])

            lines.append(f"### {i}. {title}")
            lines.append(f"- 文件：`article-{i}-{slug}.md`")
            if tags:
                lines.append(f"- 标签：{', '.join(tags)}")
            if keywords:
                lines.append(f"- 关键词：{', '.join(keywords)}")
            lines.append("")

        lines.extend([
            "---",
            "",
            "## 发布检查清单",
            "",
            "- [ ] 文章标题是否吸引人？",
            "- [ ] 配图是否清晰且相关？",
            "- [ ] 是否有独特的个人观点？",
            "- [ ] 是否符合头条号发布规范？",
            "- [ ] 是否标注了信息来源？",
        ])

        index_path.write_text("\n".join(lines), encoding="utf-8")
        return str(index_path)

    def save_metadata(self, date_str: str, metadata: dict) -> str:
        """保存每日元数据 JSON"""
        date_dir = self.get_date_dir(date_str)
        date_dir.mkdir(parents=True, exist_ok=True)
        meta_path = date_dir / "metadata.json"

        meta_content = {
            "date": date_str,
            "generated_at": datetime.now().isoformat(),
            "total_articles": metadata.get("total_articles", 0),
            "articles": metadata.get("articles", []),
            "topics": metadata.get("topics", []),
            "data_sources": metadata.get("data_sources", {}),
        }

        meta_path.write_text(json.dumps(meta_content, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(meta_path)
