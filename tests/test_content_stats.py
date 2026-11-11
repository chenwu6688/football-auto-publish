"""正文摘要指标（`content_stats`）的契约测试。

为什么单独为它建一个文件：

1. **它此前零覆盖**：`summarize()` 只在 orchestrator 保存成稿时被调用（写进
   `metadata.articles[].content_stats`），单测/验收都没直接碰过它，`selftest()`
   更是**任何地方都没有执行**——13 条判据写了但从未跑过。
2. **它是「压字数到 300-500」这项建议的唯一度量**。度量错了，后面所有结论都错。
   2026-11-11 全量测试时发现一个真缺陷：成稿 `.md` 开头有 YAML front matter，
   `--file` 直接统计整份文件，段落数 33 vs 实际 20（虚高 65%）、汉字数多 23。
   修法是在 `summarize()`（唯一入口）先剥 front matter，本文件盯住它。
"""

import json

import pytest

import content_stats as cs


# ---------------------------------------------------------------- 自带自检
def test_selftest_all_pass():
    """模块自带自检的 13 条判据必须全过。

    这些判据此前只存在于源码里，没有任何测试或验收脚本调用 `cs.selftest()`
    —— 「写了但从不执行」等于没写。这里把执行权接到 pytest 上。
    """
    bad = [name for name, ok in cs.selftest() if not ok]
    assert bad == [], f"content_stats 自检失败项：{bad}"


# ---------------------------------------------------------------- front matter
FRONT_MATTER_DOC = """---
title: "波切蒂诺领跑曼联帅位，卡里克这周就得走人"
date: 2026-11-11
category: 足球
tags: [Pochettino, Manchester United]
keywords: [Pochettino, Manchester United]
article_index: 1
batch_name: 晨读
---

第一段正文。

第二段正文。
"""


def test_front_matter_is_not_counted_as_body():
    """成稿文件的 YAML 头部不得计入段落数/字数（2026-11-11 实测虚高 65%）。"""
    s = cs.summarize(FRONT_MATTER_DOC)
    assert s["paragraphs"] == 2, f"front matter 被算成段落了：{s['paragraphs']}"
    assert "波切蒂诺" not in str(s.get("tags")), "front matter 的 tags 不该被当成正文话题标签"
    assert s["tags"] == []


def test_front_matter_does_not_change_han_len():
    """剥不剥 front matter，正文字数必须一致。"""
    body = FRONT_MATTER_DOC.split("---\n", 2)[2]
    assert cs.summarize(FRONT_MATTER_DOC)["han_len"] == cs.summarize(body)["han_len"]


def test_unclosed_divider_is_not_stripped():
    """正文开头恰好是分隔线但**没有闭合** `---` 时不能误剥（否则整篇被吃掉）。"""
    s = cs.summarize("---\n第一段正文。\n第二段正文。\n")
    assert s["paragraphs"] == 2


def test_divider_pair_without_yaml_keys_is_not_stripped():
    """「开头一条分隔线 + 后文又一条分隔线」是普通 Markdown，不是 front matter。

    只凭首尾 `---` 判定会把这中间的内容整段吃掉。判据里那条「必须有一行 key:」
    就是为了挡住这种误剥。
    """
    doc = "---\n第一段正文。\n---\n第二段正文。\n"
    s = cs.summarize(doc)
    assert s["paragraphs"] == 2, f"正文被当成 front matter 剥掉了：{s['paragraphs']}"


def test_strip_front_matter_is_idempotent_on_plain_text():
    assert cs.strip_front_matter("第一段\n第二段") == "第一段\n第二段"
    assert cs.strip_front_matter("") == ""


# ---------------------------------------------------------------- 各字段口径
def test_empty_content_degrades_without_raising():
    s = cs.summarize("")
    assert s["degraded"] == ["正文为空"]
    assert s["han_len"] == 0 and s["paragraphs"] == 0


def test_paragraphs_exclude_headings_bullets_images_dividers():
    """段落数只数真段落：小标题、列表项、图片行、分隔线、纯标签行都不算。"""
    doc = """## 小标题

第一段。

- 列表一
- 列表二

![图](a.jpg)

---

#中超#

第二段。
"""
    s = cs.summarize(doc)
    assert s["paragraphs"] == 2
    assert s["headings"] == 1
    assert s["bullets"] == 2
    assert s["images"] == 1


def test_han_len_ignores_digits_and_latin():
    s = cs.summarize("abc 123 中文四字")
    assert s["han_len"] == 4


def test_cta_requires_context():
    """裸「关注」不算 CTA（「值得关注」不是粉丝钩子），带语境才算。"""
    assert cs.summarize("这场球值得关注，后续还有变数。")["cta"] is False
    assert cs.summarize("欢迎在评论区说说你的看法。")["cta"] is True


def test_tags_are_deduped_and_capped():
    doc = " ".join(f"#标签{i}#" for i in range(15)) + " #标签0#"
    tags = cs.summarize(doc)["tags"]
    assert len(tags) == 10, "话题标签上限 10 个"
    assert len(set(tags)) == len(tags), "重复标签必须去掉"


def test_quotes_count_pairs_only():
    """「」只出现一半不算一处引语（避免虚高）。"""
    assert cs.summarize("他说「这句话没闭合")["quotes"] == 0
    assert cs.summarize("他说「闭合了」")["quotes"] == 1


# ---------------------------------------------------------------- CLI
def test_cli_file_mode_prints_json(tmp_path, capsys, monkeypatch):
    f = tmp_path / "a.md"
    f.write_text(FRONT_MATTER_DOC, encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["content_stats.py", "--file", str(f)])
    assert cs.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["paragraphs"] == 2


def test_cli_selftest_exit_code(monkeypatch):
    monkeypatch.setattr("sys.argv", ["content_stats.py", "--selftest"])
    assert cs.main() == 0
