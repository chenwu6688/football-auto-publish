#!/usr/bin/env python3
"""标题三要素（P0-1）—— 数字 / 对比 / 专名的**唯一**判定口径。

## 为什么要立这道闸门（2026-11-04 阅读量瓶颈诊断）

84 篇可关联标题的实发稿：篇均 40.6、中位 7、聚合 CTR 0.89%。分层一看，
瓶颈不在「写了多少」，而在「标题里有没有一个可核对的点」：

    含具体数字   82.2 阅读 vs 无数字   13.6（6.0×）
    数字 + 反差 120.5（CTR 2.44%，8.9×）
    标题 ≥25 字  60.5 vs <25 字 12.6（4.8×）

而 BOTTOM10 里 6 篇是「怀旧 + 抒情 + 空问句」——标题里没有任何数字、没有对照关系、
没有可检索的专名，读者在信息流里没有任何理由点开。

所以三要素是**下限判据**，不是爆款公式：
  1. 具体数字（比分/第N/度量）——给人一个可核对的锚；
  2. 对比关系（战绩组合/落差/反差）——给人一个「想知道为什么」的缺口；
  3. 可检索专名（球队/球员/赛事/城市）——让推荐系统知道这条内容属于谁。

## 为什么不是「三项全过」（回测推翻了初版硬阈值）

初版按报告原文写「三项全过才发」。用真实数据复算后改了：

  - 我方 88 篇实发稿：三项全过 19 篇（22%），中位 17；≥2 项 43 篇（49%），中位 15。
  - **TOP1（494 阅读，全站第一）只有 2/3**：「广州球迷联盟喊出70年，李璇10月13日
    这番话到底戳中了谁？」——有数字（70年、10月13日）、有专名（广州），但没有对比关系。
  - **BOTTOM 里也有 3/3**：「勒阿弗尔半场只落后一球，下半场怎么就被打散了」0 阅读。
  - 退役球星常不在 entity_map（巴乔/托蒂/普拉蒂尼），专名一栏会漏判，
    三项全过会把这些合理的「双人对比」标题挡在门外。

结论：**硬门槛取「≥2 项且必含专名」，对比缺项只触发一次重拟（不直接拦）**。
这是「把下限抬起来」，而不是「保证爆款」——三项全过照样可能 0 阅读，
三要素齐全只是入场券。回测数据写在 `scripts/analyze_title_elements.py` 里可复算。

## 与 title_features 的分工

`title_features` 是**统计口径**（对标分档、CTR 归因），`has_number` 宽松到连裸年份都算；
本模块是**闸门口径**，数字必须带度量单位/比分形态（「2026金球奖」的 2026 不算，
「2026年」算）。两者是子集关系，selftest 里有断言钉死，避免以后各改各的。

纪律：
  - 只判定、不生成、不改写；判定不出不猜（专名表读不到 → 只看数字+对比，不因此拦稿）；
  - 正则保守，宁可少认不误认；新增/收窄词表必须同步补 selftest 用例；
  - 不读 YAML 配置（词表内聚在此）；entity_map.json 是数据不是配置，读失败静默降级。

命令行：
    python3 title_elements.py --selftest
    python3 title_elements.py --title "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENTITY_MAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"

# 硬门槛：至少几项（见 docstring 的回测结论，别随手改成 3）
MIN_ELEMENTS = 2
# 专名是否必含：推荐系统靠它判断「这是谁的内容」，比数字/对比更基础
REQUIRE_PROPER = True

# ------------------------------------------------------------------ 数字
# 「具体数字」= 能被读者核对的度量。刻意不收：
#   - 裸年份（「2026金球奖」）：那是赛事名的一部分，不是信息量；
#   - 裸编号（「第3篇」之外的序数词）。
_NUMBER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("比分", r"\d{1,2}\s*[-:比]\s*\d{1,2}"),
    ("序次", r"第\s?\d{1,3}(?!\d)"),
    ("赛制", r"\d{1,2}\s*/\s*\d{1,2}|[Uu]\s?\d{2}"),
    ("度量", r"\d+(?:\.\d+)?\s*(?:球|分|场|轮|人|次|天|年|月|日|岁|名|位|"
             r"分钟|秒|%|％|万|亿|倍|个|球|胜|负|平|强|连|记|粒)"),
    # 战绩组合：1平2负 / 3胜1负 / 12轮只赢2场（后者由「轮/场」度量覆盖）
    ("战绩", r"\d{1,2}\s*(?:胜|平|负)\s*\d{1,2}(?:\s*(?:胜|平|负)\s*\d{1,2})?"),
    # 中文数字必须跟量词（「连过五人」算，「一个凡人」不算——「个」不在量词表里）
    ("中文数字", r"[一二三四五六七八九十两]{1,3}\s*(?:球|分|场|轮|人|次|天|年|月|岁|名|位|"
                 r"分钟|倍|胜|负|平|强|连|记|粒)"),
    # 中文序数：「位列第五」「排第三」——没有量词，但确实是可核对的名次
    ("中文序数", r"(?:位列|排名|排|居|第)\s?[一二三四五六七八九十]{1,2}"),
)

# ------------------------------------------------------------------ 对比
# 「对比关系」= 两个可比较对象之间出现落差，或预期与现实相反。
_COMPARE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("比分对照", r"\d{1,2}\s*[-:比]\s*\d{1,2}"),
    ("战绩对照", r"\d{1,2}\s*(?:胜|平|负)\s*\d{1,2}"),
    ("明示对比", r"(?:vs|VS|对阵|对决|大比拼|大对比|对比|相较|相比)"),
    ("倍数", r"\d+(?:\.\d+)?\s*倍"),
    ("落差", r"(?:甩开|领先|落后|追到|仅差|只差|压着|撑到|只剩|仅剩|还剩|只拿|只赢|只输|"
             r"只排|垫底|倒数|碾压|完胜|高出|低出|反超|逆转)"),
    ("差距", r"(?:差距|落差|差这么多|差那么多|差一点|不差)"),
    ("跨度", r"从.{1,10}到.{1,10}"),
    ("比较级", r"比.{1,10}(?:还|更)"),
    ("反差", r"(?:却|反而|竟然|竟|没想到|原来|结果|不料|偏偏|倒是)"),
    ("时间落差", r"\d{1,3}\s*年.{0,8}(?:还|仍|才|已|只)"),
    ("序位", r"(?:第一|第二|第三|最后|头名|榜首|榜尾|垫底)"),
)

# ------------------------------------------------------------------ 专名
# 赛事名（含常见联赛简称）。刻意不含「决赛/小组赛」这类泛指——那是 title_antipattern
# 规则二要拦的东西，不是专名。
_TOURNAMENTS = (
    "英超", "英冠", "英甲", "西甲", "西乙", "意甲", "意乙", "德甲", "德乙", "法甲", "法乙",
    "荷甲", "葡超", "土超", "苏超", "比甲", "俄超", "巴甲", "阿甲", "美职联", "沙特联",
    "欧冠", "欧联", "欧协联", "亚冠", "亚协杯", "世俱杯", "欧洲杯", "世界杯", "美洲杯",
    "亚洲杯", "非洲杯", "欧国联", "世预赛", "欧预赛", "足总杯", "联赛杯", "国王杯",
    "德国杯", "意大利杯", "法国杯", "社区盾", "超级杯", "中超", "中甲", "中乙", "足协杯",
    "国足", "女足", "国奥", "国青", "U23", "2034杯",
)

# 足球城市（用于「广州球迷联盟」这类没有队名但有明确地域指向的标题）
_CITIES = (
    "广州", "上海", "北京", "大连", "成都", "济南", "天津", "武汉", "重庆", "深圳",
    "青岛", "长春", "郑州", "南京", "杭州", "沈阳", "西安", "长沙", "苏州", "梅州",
    "南通", "昆明", "厦门", "石家庄", "太原", "合肥", "南昌", "福州", "南宁", "贵阳",
    "米兰", "罗马", "都灵", "那不勒斯", "马德里", "巴塞罗那", "塞维利亚", "瓦伦西亚",
    "慕尼黑", "多特蒙德", "莱比锡", "法兰克福", "勒沃库森", "柏林", "汉堡",
    "巴黎", "马赛", "里昂", "摩纳哥", "里斯本", "波尔图", "阿姆斯特丹", "鹿特丹",
    "曼彻斯特", "利物浦", "伦敦", "伯明翰", "纽卡斯尔", "埃弗顿", "利兹",
)

# 顶流球员 / 传奇名宿 / 名帅（2026-11-04 回测补）：
# entity_map 的 558 名球员来自 football-data.org **五大联赛射手榜**，梅西、C罗、
# 内马尔、马拉多纳、巴乔、托蒂这些「顶流但不在五大联赛或已退役」的人一个都不在里面，
# 导致「贝纳蒂亚公开质疑2026金球奖排名：梅西凭什么位列第五？」被判成 0/3。
# 这批名单与 title_features.TOP_NAME_PATTERN 的 12 人保持同步（selftest 有断言），
# 另加中文足球语境里的高频传奇/名帅。
_TOP_PEOPLE = (
    # title_features.TOP_NAME_PATTERN 的 12 位（改那边必须同步这边）
    "梅西", "C罗", "凯恩", "姆巴佩", "亚马尔", "哈兰德", "贝林厄姆", "维尼修斯",
    "内马尔", "萨拉赫", "齐达内", "小罗",
    # 传奇名宿（历史回顾/对比类标题高频，实测阅读很低，但仍属可检索专名）
    "马拉多纳", "贝利", "巴乔", "托蒂", "普拉蒂尼", "大罗", "罗纳尔多", "卡卡",
    "劳尔", "贝克汉姆", "亨利", "杰拉德", "兰帕德", "皮尔洛", "托雷斯", "伊布",
    # 现役/中超高频
    "孙兴慜", "武磊", "张玉宁", "韦世豪", "郑智", "孙雯", "王霜", "曲波",
    # 名帅
    "瓜迪奥拉", "穆里尼奥", "安切洛蒂", "阿尔特塔", "克洛普", "滕哈赫", "波切蒂诺",
    "西蒙尼", "恩里克", "弗里克", "斯洛特", "阿莫林",
)


def top_people() -> tuple[str, ...]:
    """顶流/传奇/名帅名单（供配图链路补实体：entity_map 只有五大联赛射手榜）。

    2026-11-04：`data_collector.search_images` 的实体来源是 entity_map，梅西/C罗/
    马拉多纳都不在里面 → 「梅西C罗数据大对比」这类稿子 0 图 → 发布时被判「无封面」
    （无封面组聚合 CTR 0.52% vs 有封面 2.07%）。这里把同一份名单开放给封面链路复用，
    避免两处各维护一份「谁算名人」。
    """
    return _TOP_PEOPLE


@lru_cache(maxsize=1)
def _entity_names() -> frozenset[str]:
    """专名表 = entity_map 的球队/球员译名与别名 ∪ 顶流/传奇/名帅补充。

    entity_map 只有五大联赛射手榜里的 558 人，顶流与退役传奇要另补（见 _TOP_PEOPLE）。
    读不到 entity_map 也**不抛**——只少了词典部分，赛事名/城市名/补充名单照常工作。
    """
    names: set[str] = set(_TOP_PEOPLE)
    try:
        data = json.loads(ENTITY_MAP_PATH.read_text(encoding="utf-8"))
        for _key, v in (data.get("entities") or {}).items():
            if not isinstance(v, dict):
                continue
            if v.get("name_zh"):
                names.add(str(v["name_zh"]))
            for a in (v.get("aliases") or []):
                names.add(str(a))
    except Exception:
        pass
    # 单字名没有区分度，且易误命中
    return frozenset(n for n in names if len(n) >= 2)


@dataclass(frozen=True)
class Elements:
    """一条标题的三要素命中情况。字段名即报表列名，改动需同步下游。"""
    numbers: tuple[str, ...]
    contrasts: tuple[str, ...]
    proper_nouns: tuple[str, ...]

    @property
    def has_number(self) -> bool:
        return bool(self.numbers)

    @property
    def has_contrast(self) -> bool:
        return bool(self.contrasts)

    @property
    def has_proper(self) -> bool:
        return bool(self.proper_nouns)

    @property
    def count(self) -> int:
        return int(self.has_number) + int(self.has_contrast) + int(self.has_proper)

    @property
    def missing(self) -> list[str]:
        out = []
        if not self.has_number:
            out.append("数字")
        if not self.has_contrast:
            out.append("对比")
        if not self.has_proper:
            out.append("专名")
        return out

    def to_row(self) -> dict:
        d = asdict(self)
        d.update({"has_number": self.has_number, "has_contrast": self.has_contrast,
                  "has_proper": self.has_proper, "count": self.count})
        return d


def _hits(title: str, patterns: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """命中类别（去重、按词表顺序），不返回匹配文本——报表只要类别名。"""
    out: list[str] = []
    for kind, pat in patterns:
        if kind not in out and re.search(pat, title):
            out.append(kind)
    return tuple(out)


def _proper_nouns(title: str) -> tuple[str, ...]:
    """可检索专名：实体词典（最长优先）+ 赛事名 + 城市名。"""
    hits: list[str] = []
    rest = title
    for name in sorted(_entity_names(), key=len, reverse=True):
        if name in rest:
            hits.append(name)
            rest = rest.replace(name, "\x00")     # 防「米兰」与「AC米兰」重复计入同一处
            if len(hits) >= 6:
                break
    for name in _TOURNAMENTS + _CITIES:
        if name in rest and name not in hits:
            hits.append(name)
    return tuple(hits[:8])


@lru_cache(maxsize=4096)
def extract(title: str) -> Elements:
    """提取一条标题的三要素（带缓存：批量回测时同一标题会被反复判）。"""
    t = (title or "").strip()
    if not t:
        return Elements((), (), ())
    return Elements(_hits(t, _NUMBER_PATTERNS), _hits(t, _COMPARE_PATTERNS), _proper_nouns(t))


def check(title: str, *, min_elements: int = MIN_ELEMENTS,
          require_proper: bool = REQUIRE_PROPER) -> dict:
    """判定标题是否达到三要素下限。

    返回 dict：
      ok        True = 放行
      hit       bool，是否命中闸门（= not ok 且有实质判据）
      count     命中项数（0~3）
      missing   缺哪些项
      elements  Elements.to_row()
      hint      给重写用的具体指令
      detail    人话解释（进日志/打回原因）
    """
    t = (title or "").strip()
    if not t:
        return {"ok": True, "hit": False, "count": 0, "missing": [], "elements": {},
                "hint": "", "detail": ["标题为空，不判定"]}

    e = extract(t)
    ok = e.count >= int(min_elements) and (e.has_proper or not require_proper)
    if ok:
        return {"ok": True, "hit": False, "count": e.count, "missing": e.missing,
                "elements": e.to_row(), "hint": "",
                "detail": [f"三要素 {e.count}/3 达标（{'、'.join(
                    [x for x, on in (('数字', e.has_number), ('对比', e.has_contrast),
                                     ('专名', e.has_proper)) if on]) or '无'}）"]}

    lacks = []
    if not e.has_number:
        lacks.append("一个可核对的具体数字（比分/第几轮/第几分钟/积分/人数/年龄）")
    if not e.has_contrast:
        lacks.append("一处对比或落差（1平2负、22分压着对手、甩开一倍、从X到Y）")
    if not e.has_proper:
        lacks.append("一个可检索的专名（球队/球员/教练/赛事/城市，不要写「这支豪门」）")
    hint = ("标题信息量不足（三要素只命中 "
            f"{e.count}/3，下限 {int(min_elements)} 项且须含专名）。缺：" + "；".join(lacks) +
            "。只能用事实卡里已有的数字，不得编造。")
    return {
        "ok": False, "hit": True, "count": e.count, "missing": e.missing,
        "elements": e.to_row(), "hint": hint,
        "detail": [f"三要素 {e.count}/3 未达下限（需 ≥{int(min_elements)} 项"
                   f"{'且含专名' if require_proper else ''}）",
                   "缺：" + "、".join(e.missing)],
    }


def coverage(titles) -> dict:
    """批次层面的三要素覆盖率。空样本各项为 None，**不填 0**。"""
    items = [t for t in (titles or ()) if (t or "").strip()]
    if not items:
        return {"n": 0, "number": None, "contrast": None, "proper": None,
                "all_three": None, "pass_rate": None, "median_count": None}
    rows = [extract(t) for t in items]
    n = len(rows)
    counts = sorted(r.count for r in rows)
    return {
        "n": n,
        "number": round(100 * sum(1 for r in rows if r.has_number) / n, 1),
        "contrast": round(100 * sum(1 for r in rows if r.has_contrast) / n, 1),
        "proper": round(100 * sum(1 for r in rows if r.has_proper) / n, 1),
        "all_three": round(100 * sum(1 for r in rows if r.count == 3) / n, 1),
        "pass_rate": round(100 * sum(1 for r in rows if check_ok(r)) / n, 1),
        "median_count": counts[n // 2] if n % 2 else (counts[n // 2 - 1] + counts[n // 2]) / 2,
    }


def check_ok(e: Elements) -> bool:
    """Elements 版放行判定（供 coverage 复用，避免 check() 里重复 extract）。"""
    return e.count >= MIN_ELEMENTS and (e.has_proper or not REQUIRE_PROPER)


# ------------------------------------------------------------------ 自检
def selftest() -> list[tuple[str, bool]]:
    """用例全部来自真实成稿/对标样本；改词表必须同步改这里。"""
    cases: list[tuple[str, bool]] = []

    # 三要素齐全的真实高阅读标题（TOP1~TOP6）
    for t in ("亚冠第三轮上座榜出炉，没想到国安27638人竟甩开海港一倍多",
              "米兰德比谁先崩？国米22分压着米兰，两位主帅的底牌竟然差在这？",
              "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？",
              "没想到莱比锡主帅压力比拜仁还大，9分对上11分，他输不起什么？"):
        e = extract(t)
        cases.append((f"三要素齐全：{t[:16]}", e.count == 3))
        cases.append((f"放行：{t[:16]}", check(t)["ok"] is True))

    # TOP1 只有 2/3（缺对比），必须仍然放行——这是「不取三项全过」的直接原因
    top1 = "广州球迷联盟喊出70年，李璇10月13日这番话到底戳中了谁？"
    cases.append(("TOP1 是三要素 2/3（缺对比）不是 0/3", extract(top1).count == 2))
    cases.append(("TOP1 必须放行（否则闸门误杀全站第一）", check(top1)["ok"] is True))

    # 空问句：只有专名、没有数字和对比 → 拦
    for t in ("当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？",
              "马拉多纳的上帝之手，英格兰到底冤不冤呢"):
        r = check(t)
        cases.append((f"拦空问句：{t[:16]}", r["ok"] is False and r["hit"] is True))
        cases.append((f"拦的理由点名缺项：{t[:10]}", "缺：" in "".join(r["detail"])))

    # 泛称无专名 → 拦（「这支豪门」「主帅」不算可检索专名）
    r = check("这支豪门到底怎么了，连续三场崩盘还有救吗")
    cases.append(("泛称无专名 → 拦", r["ok"] is False and "专名" in r["missing"]))

    # 3/3 但 0 阅读的真实样本：闸门放行，只说明「齐全≠爆款」
    threestar = "勒阿弗尔半场只落后一球，下半场怎么就被打散了"
    cases.append(("三项全过仍可能 0 阅读（闸门只保下限）", check(threestar)["ok"] is True))

    # 数字判定：裸年份不算，「2026年」算
    cases.append(("裸年份不算数字", extract("2026金球奖揭晓").has_number is False))
    cases.append(("「2026年」算数字", extract("2026年的金球奖揭晓").has_number is True))
    cases.append(("「十二码」不算数字（无度量单位）", extract("十二码前的犹豫").has_number is False))

    # 对比判定：裸「差」不算，「差距」算
    cases.append(("裸「差」不算对比", extract("这球踢得真差").has_contrast is False))
    cases.append(("「差距」算对比", extract("两队的差距摆在那").has_contrast is True))

    # 专名：队名/球员/赛事/城市
    for t, want in (("申花争冠最后拼图回来了", True), ("亚冠第三轮上座榜", True),
                    ("广州球迷联盟喊出70年", True), ("这支球队的更衣室", False)):
        cases.append((f"专名判定：{t[:12]}→{want}", extract(t).has_proper is want))

    # 顶流/传奇不在五大联赛射手榜里，必须靠补充名单认出来（2026-11-04 回测的真实缺口）
    for name in ("梅西", "C罗", "内马尔", "马拉多纳", "贝利", "巴乔",
                 "托蒂", "齐达内", "瓜迪奥拉"):
        cases.append((f"顶流/传奇识别为专名：{name}", extract(f"{name}这场球踢得怎样").has_proper))
    # 对照：title_features.TOP_NAME_PATTERN 的 12 人必须全部被认成专名（两处口径同步）
    cases.append(("与 title_features 的顶流名单同步",
                  all(extract(n).has_proper for n in
                      ("梅西", "C罗", "凯恩", "姆巴佩", "亚马尔", "哈兰德", "贝林厄姆",
                       "维尼修斯", "内马尔", "萨拉赫", "齐达内", "小罗"))))
    # 代称不算专名（「大英帝星」「蓝月亮」「红黑军团」读者能猜，但推荐系统认不出）
    cases.append(("代称不算专名：大英帝星",
                  extract("4场6球！25岁大英帝星复活").has_proper is False))

    # 中文序数（「位列第五」没有量词，但名次可核对）
    cases.append(("「位列第五」算数字", extract("梅西凭什么位列第五").has_number is True))
    cases.append(("「第五轮」算数字", extract("第五轮开打").has_number is True))

    # 与 title_features 的子集关系（数字口径这是收紧版，不能出现「elements 认、features 不认」）
    import title_features
    for t in ("第94分钟丢球，阿森纳遭读秒绝杀", "1平2负只拿0.33分",
              "九人苦撑3-4", "2026金球奖揭晓", "38岁还在每天证明自己"):
        e = extract(t)
        cases.append((f"数字口径是 features 的子集：{t[:12]}",
                      (not e.has_number) or title_features.extract(t).has_number))

    # 缓存不能改变结果（同一标题两次调用等价）
    cases.append(("缓存幂等", extract(top1) == extract(top1)))

    # 空标题不炸
    cases.append(("空标题不抛异常", check("")["ok"] is True))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="标题三要素（数字/对比/专名）判定")
    ap.add_argument("--title", help="判定单条标题")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.title:
        r = check(args.title)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    cov = coverage(["1平2负只拿0.33分，中超这根独苗还能撑到2028吗？",
                    "当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？"])
    print(f"\n覆盖率样例：{cov}")
    print(f"自检 {len(selftest())} 项，失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
