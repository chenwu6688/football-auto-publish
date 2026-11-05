# -*- coding: utf-8 -*-
"""配图台账的公共判据（无依赖，保存侧 / 验收侧 / CI 合并脚本共用）。

## 为什么需要单独一个模块

2026-11-05 当天，`output/2026-11-05/metadata.json` 里同时躺着两种产物：

* 12:21 的 noon 批 3 篇——版式库改造**之前**的代码生成，每篇 1 张图；
* 15:13 的 morning 批 1 篇——改造后的代码生成，3 张图。

「每篇 ≥3 张」这条下限要判的是**改造之后的产物**。判据不能用日期（同一天有新老），
也不能用 `images` 的条数（那正是被检对象），只能用「这篇有没有配图台账」：
新版保存侧固定写 `image_cards` / `image_anchors` 两栏，旧版根本没有这两个键。

同一判据原先在两个地方各写一份（验收脚本、单测），CI 合并脚本里又按「全部文章」
另算了一次 `articles_below` —— 三处口径不一致，于是 `articles_below: 0` 与
「3 篇各 1 张」同时出现在一份文件里。这里收敛成一处。
"""

from __future__ import annotations

#: 新版保存侧写给每篇文章的配图台账字段（存在即为「走过新路径」，哪怕值是空列表）
IMAGE_LEDGER_FIELDS: tuple[str, ...] = ("image_cards", "image_anchors")


def has_image_ledger(article: dict) -> bool:
    """这篇是否由「带配图台账的保存侧」生成。

    注意区分 `None` 与 `[]`：旧版产物**没有这个键**（`.get()` 得到 None），
    新版即使一张原创信息图都没出，也会写一个空列表。所以判据是 `is not None`，
    不是真值判断。
    """
    if not isinstance(article, dict):
        return False
    return any(article.get(k) is not None for k in IMAGE_LEDGER_FIELDS)


def count_below_floor(articles, min_images: int = 3) -> tuple[int, list[str]]:
    """统计**有台账的**文章里有几篇配图不足，返回 (篇数, 标题列表)。

    无台账的文章（改造前产物）不计入——它们不是「不达标」，是「没有可比性」。
    这与验收脚本的三态一致：未判定 ≠ 通过。
    """
    below: list[str] = []
    for a in articles or []:
        if not has_image_ledger(a):
            continue
        imgs = a.get("images") or a.get("downloaded_images") or []
        if len(imgs) < int(min_images):
            below.append(str(a.get("title", ""))[:24])
    return len(below), below
