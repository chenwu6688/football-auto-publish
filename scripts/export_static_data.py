#!/usr/bin/env python3
"""导出管线文章为小程序可用的静态 JSON 数据（jsDelivr CDN 方案）

用法:
  python scripts/export_static_data.py              # 导出今天
  python scripts/export_static_data.py 2026-06-19   # 导出指定日期
  python scripts/export_static_data.py --all         # 导出所有有数据的日期

功能:
  - 读取 output/{date}/*.md + metadata.json
  - Markdown → HTML 转换
  - 图片路径替换为 jsDelivr CDN URL
  - 写入 static_data/today.json + static_data/history.json
  - 幂等：多次运行不会产生重复数据
"""

import os, sys, re, json, hashlib
from pathlib import Path
from datetime import date, datetime

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
STATIC_DIR = PROJECT_ROOT / "static_data"
CDN_BASE = "https://cdn.jsdelivr.net/gh/chenwu6688/football-auto-publish@main"

# ─── Markdown → HTML 简易转换 ──────────────────────────────

def md_to_html(text):
    """将 Markdown 正文转换为 HTML（支持文章常用的语法子集）"""
    lines = text.split("\n")
    html_parts = []
    in_list = False
    list_type = None

    def close_list():
        nonlocal in_list, list_type
        if in_list:
            html_parts.append(f"</{list_type}>")
            in_list = False
            list_type = None

    for line in lines:
        # 代码块（跳过，文章里几乎不用）
        if line.strip().startswith("```"):
            continue

        # 图片: ![alt](url) → <img src="CDN_url" alt="alt" />
        line = re.sub(
            r'!\[([^\]]*)\]\(([^)]+)\)',
            lambda m: _img_tag(m.group(1), m.group(2)),
            line,
        )

        # 链接: [text](url)
        line = re.sub(
            r'\[([^\]]+)\]\(([^)]+)\)',
            r'<a href="\2">\1</a>',
            line,
        )

        # 加粗: **text**
        line = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', line)

        # 斜体: *text*
        line = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', line)

        # 标题
        h_match = re.match(r'^(#{1,4})\s+(.+)$', line)
        if h_match:
            close_list()
            level = len(h_match.group(1))
            html_parts.append(f'<h{level}>{h_match.group(2)}</h{level}>')
            continue

        # 无序列表
        ul_match = re.match(r'^\s*[-*+]\s+(.+)$', line)
        if ul_match:
            if not in_list or list_type != "ul":
                close_list()
                html_parts.append("<ul>")
                in_list = True
                list_type = "ul"
            html_parts.append(f"<li>{ul_match.group(1)}</li>")
            continue

        # 有序列表
        ol_match = re.match(r'^\s*(\d+)\.\s+(.+)$', line)
        if ol_match:
            if not in_list or list_type != "ol":
                close_list()
                html_parts.append("<ol>")
                in_list = True
                list_type = "ol"
            html_parts.append(f"<li>{ol_match.group(2)}</li>")
            continue

        # 分割线
        if re.match(r'^-{3,}$', line.strip()) or re.match(r'^\*{3,}$', line.strip()):
            close_list()
            html_parts.append("<hr />")
            continue

        # 空行 = 段落分隔
        if line.strip() == "":
            close_list()
            continue

        # 正文段落
        close_list()
        html_parts.append(f"<p>{line}</p>")

    close_list()
    return "\n".join(html_parts)


def _img_tag(alt, url):
    """图片标签：将相对路径转为 CDN 绝对路径"""
    # 如果已经是 CDN 或 http URL，不处理
    if url.startswith("http") or url.startswith("//"):
        cdn_url = url
    else:
        # 相对路径如 images/article-2-img-001.jpg
        # 在导出时外部传入 date，这里用占位符替换
        cdn_url = url  # 外部会通过全局替换处理
    return f'<img src="{cdn_url}" alt="{alt}" style="width:100%;border-radius:8px;margin:12px 0;" />'


def _verify_img_tag(tag_html, src, local_img_dir):
    """检查图片是否存在于本地，不存在则移除 img 标签"""
    # 提取文件名
    filename = src.rsplit("/", 1)[-1] if "/" in src else src
    img_path = local_img_dir / filename
    if img_path.exists():
        return tag_html
    # 尝试找匹配的文件
    if local_img_dir.exists():
        for f in local_img_dir.iterdir():
            if f.name == filename or f.stem == Path(filename).stem:
                return tag_html
    # 图片不存在 → 移除整个 img 标签
    return ""


def rebuild_image_urls(html, date_str):
    """将 HTML 中的图片相对路径替换为 CDN 绝对路径"""
    cdn_prefix = f"{CDN_BASE}/output/{date_str}"
    # 替换 images/xxx → CDN/{date}/images/xxx
    html = re.sub(
        r'src="images/',
        f'src="{cdn_prefix}/images/',
        html,
    )
    # 替换 src="xxx" 其中 xxx 是 images/ 路径但没有引号包裹的
    html = re.sub(
        r'src=\'(images/[^\']+)\'',
        f"src='{cdn_prefix}/" + r"\1",
        html,
    )
    return html


# ─── 文章解析 ─────────────────────────────────────────────

def parse_markdown_file(filepath, date_str):
    """解析一篇 article markdown 文件，返回结构化数据"""
    raw = filepath.read_text(encoding="utf-8")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None

    frontmatter_text = parts[1].strip()
    body = parts[2].strip()

    # 去除尾部的 球评人老六 · YYYY-MM-DD
    body = re.sub(r'\n---\s*\n\*球评人老六\s*·\s*[^*]+\*$', '', body).strip()
    body = re.sub(r'\*球评人老六\s*·\s*[^*]+\*$', '', body).strip()

    # 解析 frontmatter
    fm = {}
    for line in frontmatter_text.split("\n"):
        m = re.match(r'^(\w+):\s*(.*)', line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            # 去掉引号
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            # 解析数组 [a, b, c]
            arr_match = re.match(r'^\[(.+)\]$', val)
            if arr_match:
                val = [x.strip().strip('"').strip("'") for x in arr_match.group(1).split(",") if x.strip()]
            fm[key] = val

    # 从 frontmatter 获取信息
    title = fm.get("title", filepath.stem)
    tags = fm.get("tags", []) if isinstance(fm.get("tags"), list) else []
    keywords = fm.get("keywords", []) if isinstance(fm.get("keywords"), list) else []

    # 提取 first line 作为摘要
    body_text = re.sub(r'<[^>]+>', '', body)
    body_text = re.sub(r'!\[.*?\]\(.*?\)', '', body_text)
    body_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', body_text)
    summary = _extract_summary(body_text)

    # Markdown → HTML
    html = md_to_html(body)
    html = rebuild_image_urls(html, date_str)

    # 移除所有图片标签（CDN 图片在国内访问不稳定）
    # 未来 CDN 稳定后可取消此限制
    html = re.sub(r'<img[^>]*>', '', html)
    html = re.sub(r'<p>\s*</p>', '', html)

    # 提取金句（**加粗** 的句子）
    golden_lines = re.findall(r'\*\*([^*]{3,80})\*\*', body)
    golden_lines = [re.sub(r'!\[.*?\]\(.*?\)', '', g).strip() for g in golden_lines]
    golden_lines = [g for g in golden_lines if len(g) > 4][:3]

    # 封面图暂时留空（CDN 图片国内访问不稳定）
    cover_img = ""

    # 文章 ID
    article_id = f"{date_str}_{filepath.stem.split('-')[-1]}"

    # batch_name 从 frontmatter 或文件名推断
    batch_name = fm.get("batch_name", "")

    return {
        "id": article_id,
        "title": title,
        "summary": summary,
        "date": date_str,
        "tags": tags if isinstance(tags, list) else [],
        "keywords": keywords if isinstance(keywords, list) else [],
        "golden_lines": golden_lines,
        "cover_image": cover_img,
        "html_content": html,
        "batch_name": batch_name,
    }


def _extract_summary(body_text):
    """从正文提取摘要（第一段有意义的内容，跳过 # 标题行）"""
    for line in body_text.split("\n"):
        line = line.strip()
        # 跳过 markdown 标题行
        if line.startswith("#"):
            continue
        if len(line) > 10:
            return line[:80] + ("..." if len(line) > 80 else "")
    return ""


def get_metadata(date_str):
    """读取日期目录的 metadata.json"""
    meta_path = OUTPUT_DIR / date_str / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return None


def scan_articles(date_str):
    """扫描日期目录下的所有文章，返回结构化列表"""
    articles = []
    md_dir = OUTPUT_DIR / date_str
    if not md_dir.exists():
        return articles

    for f in sorted(md_dir.iterdir()):
        if f.name.startswith("article-") and f.name.endswith(".md"):
            parsed = parse_markdown_file(f, date_str)
            if parsed:
                articles.append(parsed)

    return articles


def build_batch_groups(date_str):
    """按批次分组文章"""
    meta = get_metadata(date_str)
    if not meta:
        return [], {}

    articles = scan_articles(date_str)
    if not articles:
        return [], {}

    # 批次映射
    batch_map = {"晨读": "morning", "午间": "noon", "晚间": "evening"}
    reverse_map = {"morning": "晨读", "noon": "午间", "evening": "晚间"}
    batch_order = ["晨读", "午间", "晚间"]

    # 按 batch_name 分组
    groups = {}
    for a in articles:
        bn = a.get("batch_name", "")
        if bn not in groups:
            # 从 metadata 获取 column info
            col_name = ""
            for ma in meta.get("articles", []):
                if ma.get("title") == a["title"]:
                    col_name = ma.get("column_name", "")
                    break
            groups[bn] = {
                "batch_name": bn,
                "batch_key": batch_map.get(bn, bn),
                "column_name": col_name,
                "articles": [],
            }
        groups[bn]["articles"].append(a)

    # 排序
    ordered = []
    for bn in batch_order:
        if bn in groups:
            ordered.append(groups[bn])
    # 其他批次名
    for bn in groups:
        if bn not in batch_order:
            ordered.append(groups[bn])

    return ordered, {a["id"]: a for a in articles}


def _articles_from_metadata(meta, date_str):
    """从 metadata 构建基础文章信息（没有 md 文件时兜底）"""
    articles = []
    for i, ma in enumerate(meta.get("articles", [])):
        articles.append({
            "id": f"{date_str}_{i+1}",
            "title": ma.get("title", "未知标题"),
            "summary": ma.get("originality_note", "")[:80],
            "date": date_str,
            "tags": ma.get("tags", []),
            "keywords": ma.get("keywords", []),
            "golden_lines": [],
            "cover_image": "",
            "html_content": f"<p>{ma.get('originality_note', '内容加载中...')}</p>",
            "batch_name": ma.get("batch_name", ""),
        })
    return articles


# ─── 历史数据 ─────────────────────────────────────────────

def scan_all_dates():
    """扫描所有有 metadata 的日期"""
    dates = []
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir():
            continue
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', d.name):
            continue
        if (d / "metadata.json").exists():
            dates.append(d.name)
    return dates


# ─── 导出 ─────────────────────────────────────────────────

def export_today(date_str=None):
    """导出今日数据到 static_data/today.json"""
    if date_str is None:
        date_str = date.today().isoformat()

    print(f"[export] 导出日期: {date_str}")

    meta = get_metadata(date_str)
    if not meta:
        print(f"[export] ⚠️  {date_str} 没有 metadata，创建空数据")
        batches = []
        articles = {}
    else:
        batches, articles = build_batch_groups(date_str)

    data = {
        "date": date_str,
        "batches_completed": meta.get("batches_completed", []) if meta else [],
        "batches": batches,
        "articles": list(articles.values()),
        "generated_at": datetime.now().isoformat(),
    }

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    path = STATIC_DIR / "today.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"[export] ✅ 已写入 {path} ({len(data['articles'])} 篇文章)")
    return True


def export_history():
    """导出历史日期索引"""
    dates = scan_all_dates()
    data = {"dates": dates, "count": len(dates), "generated_at": datetime.now().isoformat()}

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    path = STATIC_DIR / "history.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"[export] ✅ 历史索引已写入 {path} ({len(dates)} 个日期)")
    return True


def export_all():
    """导出所有日期"""
    dates = scan_all_dates()
    for d in dates:
        export_today(d)
    export_history()
    print(f"[export] ✅ 全部导出完成，共 {len(dates)} 天")
    return True


# ─── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    if "--all" in sys.argv:
        export_all()
    elif len(sys.argv) > 1 and re.match(r'^\d{4}-\d{2}-\d{2}$', sys.argv[1]):
        export_today(sys.argv[1])
        export_history()
    else:
        export_today()
        export_history()
