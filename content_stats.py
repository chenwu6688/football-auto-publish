#!/usr/bin/env python3
"""正文摘要指标（P1-2 观测基础，2026-11-04）。

## 为什么必须先落这个

2026-11-04 的 GitHub 项目筛选里发现一个可观测性缺口：正文 `.md` 只存在于 CI 的临时目录
（`/home/runner/work/_temp/output/...`），仓库里只有 `metadata.json`。于是
「字数 / 段数 / 列表 / 引语」这四项**无法从仓库复算**——想验证「压字数到 300-500」
这条建议，先得把指标写进 metadata。

这和「37% 无封面黑了很久」同源：**结论落库了，过程没落库**。
没有过程指标，任何「改一版看看效果」都只能靠感觉。

## 口径

- 统计前先剥掉 YAML front matter（成稿 `.md` 的 `title:` / `tags:` 头部不算正文，
  否则段落数与字数都会虚高——2026-11-11 实测虚高 65%）；
- `han_len`：汉字数（不含标点/英文/数字）——压字数看这个，不看字符数；
- `paragraphs`：正文段落数（空行分隔、排除标题行/图片行/分隔线）；
- `headings`：`## 小标题` 数；
- `bullets`：列表项数（`- ` / `* ` / `1. `）；
- `quotes`：引用块或中文引语数（`>` 行、「」/“” 成对出现）；
- `tags`：话题标签（`#话题#` 或 `#话题`）；
- `cta`：是否含评论区引导（**必须带语境**，裸「关注」不算——「值得关注」不是粉丝钩子）；
- `images`：Markdown 图片数（`![...](...)`）。

只统计、不改写、不抛异常：任何字段算不出来就给 0，并在 `degraded` 里说明。
「算不出来」和「确实是 0」在调用方要能区分，故 `degraded` 是列表不是布尔。

命令行：
    python3 content_stats.py --selftest
    python3 content_stats.py --file output/2026-11-04/xxx.md
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_RE_HAN = re.compile(r"[\u4e00-\u9fa5]")
_RE_HEADING = re.compile(r"^\s{0,3}#{2,6}\s+\S")
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_BULLET = re.compile(r"^\s*(?:[-*+]\s+|\d{1,2}[.、]\s+)\S")
_RE_DIVIDER = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_RE_BLOCKQUOTE = re.compile(r"^\s*>\s?")
# 话题标签：头条号用 #话题#，也兼容单个 #话题
_RE_TAG = re.compile(r"#([^#\s]{1,20})#?")
# CTA 必须带语境（项目纪律：闸门判词必须带语境）
_RE_CTA = re.compile(
    r"(评论区(见|聊聊|说说|告诉我|拍砖|等你|走起)|你觉得|你怎么看|你选谁|"
    r"留言|说说你的|来聊聊|站队|投票|下注)")

# 图片行 / 空行不参与段落计数；引用块与纯标签行也不算正文段落
_RE_TAG_ONLY = re.compile(r"^(?:#[^#\s]{1,20}#?\s*)+$")
_RE_IMAGE_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_SKIP_LINE = (_RE_HEADING, _RE_DIVIDER, _RE_BLOCKQUOTE, _RE_TAG_ONLY, _RE_IMAGE_LINE)

# YAML front matter：成稿 .md 的真实形态（title/date/category/tags/keywords/…）。
# 2026-11-11 实测：不剥它，`--file output/<date>/article-N.md` 会把它当正文算——
# 段落数 33 vs 实际 20（虚高 65%），汉字数多 23。而「字数/段数」正是用来验证
# 「压字数到 300-500」的指标，虚高一倍这项就没法用了。
_RE_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def strip_front_matter(text: str) -> str:
    """剥掉文件开头的 YAML front matter。找不到闭合 `---` 就原样返回（不误伤正文）。

    额外判一条：块内必须至少有一行 `key:` 形式。否则「正文以分隔线开头、后文又有
    另一条分隔线」的普通 Markdown 会被误当成 front matter 整段吃掉。
    """
    text = text or ""
    m = _RE_FRONT_MATTER.match(text)
    if not m:
        return text
    if not re.search(r"^[A-Za-z_][\w-]*:", m.group(0), re.M):
        return text
    return text[m.end():]


def summarize(content: str) -> dict:
    """统计正文摘要指标。空正文返回全 0 + degraded 说明（不抛）。

    若传入的是带 YAML front matter 的成稿文件全文，先剥掉头部再统计——
    否则 `title:` / `tags:` 这些行会被算成正文段落。
    """
    text = strip_front_matter(content)
    out = {"chars": len(text), "han_len": 0, "paragraphs": 0, "headings": 0,
           "bullets": 0, "quotes": 0, "tags": [], "cta": False, "images": 0,
           "degraded": []}
    if not text.strip():
        out["degraded"].append("正文为空")
        return out

    out["han_len"] = len(_RE_HAN.findall(text))
    out["headings"] = sum(1 for ln in text.splitlines() if _RE_HEADING.match(ln))
    out["bullets"] = sum(1 for ln in text.splitlines() if _RE_BULLET.match(ln))
    out["images"] = len(_RE_IMAGE.findall(text))

    paras = 0
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if any(p.match(s) for p in _SKIP_LINE):
            continue
        if _RE_BULLET.match(s):
            continue                      # 列表项算 bullets，不算段落
        paras += 1
    out["paragraphs"] = paras

    quotes = sum(1 for ln in text.splitlines() if _RE_BLOCKQUOTE.match(ln))
    # 中文引语成对出现算 1 处（避免「」只出现一半时虚高）
    pairs = min(text.count("「"), text.count("」")) + min(text.count("“"), text.count("”"))
    out["quotes"] = quotes + pairs

    tags: list[str] = []
    for m in _RE_TAG.finditer(text):
        t = m.group(1).strip()
        if t and t not in tags:
            tags.append(t)
    out["tags"] = tags[:10]

    out["cta"] = bool(_RE_CTA.search(text))
    return out


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []

    body = """## 小标题一
第一段正文，说的是这场球的转折。

- 列表一
- 列表二

## 小标题二
第二段里有「引语」和 3 个数字。

> 引用一句

![配图](images/a.jpg)
---
最后一句，你怎么看？评论区聊聊。
#中超#  #国安
"""
    s = summarize(body)
    cases.append(("汉字数可复算", s["han_len"] > 20))
    cases.append(("小标题 2 个", s["headings"] == 2))
    cases.append(("列表 2 项", s["bullets"] == 2))
    cases.append(("图片 1 张", s["images"] == 1))
    cases.append(("段落排除标题/列表/图片/分隔线", s["paragraphs"] == 3))
    cases.append(("引语计入", s["quotes"] >= 2))
    cases.append(("CTA 带语境识别", s["cta"] is True))
    cases.append(("话题标签", set(s["tags"]) == {"中超", "国安"}))

    # 裸「关注」不是 CTA（「值得关注」不是粉丝钩子）
    s2 = summarize("这场球值得关注，后续还有变数。")
    cases.append(("裸「关注」不算 CTA", s2["cta"] is False))
    s3 = summarize("这场球值得关注，欢迎在评论区说说你的看法。")
    cases.append(("「评论区说说」算 CTA", s3["cta"] is True))

    cases.append(("空正文不抛且标 degraded", summarize("")["degraded"] == ["正文为空"]))
    cases.append(("纯标题不产生段落", summarize("## 只有标题")["paragraphs"] == 0))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="正文摘要指标（只算不改）")
    ap.add_argument("--file", help="统计一个 Markdown 文件的指标")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.file:
        p = Path(args.file)
        print(json.dumps(summarize(p.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
        return 0

    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
