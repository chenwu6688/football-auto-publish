#!/usr/bin/env python3
"""合规审核唯一入口（2026-11-06 立）。

## 它解决什么问题

在此之前，「会不会被头条扣分」这件事没有一个能回答的地方：
47 个验收脚本、约 402 个判项混着四类完全不同的东西（扣分风险 / 质量偏好 /
增长指标 / 工程健康），共用同一套「硬项-告警项」语义。结果是：
真正与合规有关的十几条判据，被埋在 400 条里，既看不出重点，也挡不住任何东西
（发布前 8 个验收里有 7 个带 `|| echo`）。

本脚本只做一件事：**按 `config/toutiao_rules.yaml` 逐条判定「这篇稿子会不会被扣分」**，
输出「能不能发 + 潜在扣分合计 + 命中的是哪条规则」。

## 三条纪律（与全仓一致，不是本项目新发明的）

1. **未判定 ≠ 通过**。判据需要正文而正文读不到（跨 runner 的旧批次就是这样），
   判 ``not_judged``，绝不拿标题或摘要冒充正文。
2. **同一量只有一处定义**。判定一律调用现有模块（``interaction_bait`` /
   ``image_relevance`` / ``rumor_wording`` / ``title_gate`` / ``data_chart``），
   本脚本不重写任何判定逻辑——否则就是「两套实现只改一处」的老毛病。
3. **判据必须真的在判**。``--selftest`` 拿合成样本做负向验证：
   每条 block 级规则都必须能被打红，否则说明它是恒真的摆设。

## 退出码

    0  没有 block 级违规
    1  有 block 级违规（不该发）
    2  一条都没判定出来（无产物 / 正文不可读）—— **不等于通过**
    3  逐篇剔除后一篇不剩（「全丢」，要人查规则；与「上游空转」不是一回事）

用法：
    python3 scripts/verify_compliance.py             # 近 7 天
    python3 scripts/verify_compliance.py --days 1 --quiet
    python3 scripts/verify_compliance.py --days 1 --drop-violations --fail-on-block-only
                                                    # 发布前：逐篇剔除，其余照发
    python3 scripts/verify_compliance.py --selftest  # 负向自检
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RULES_PATH = ROOT / "config" / "toutiao_rules.yaml"
AUDIT_DIR = ROOT / "data" / "audit"
OUT_PATH = AUDIT_DIR / "compliance_latest.json"

#: 规则表生效日。之前的批次跑的是旧口径（关注引导必填、反洗稿只记分），
#: 拿它们判新规则只会得到一条永远红的线。旧样本仍会计入 `legacy` 打印。
SINCE = "2026-11-06"

PASS, FAIL, UNJ, NA = "pass", "fail", "not_judged", "n/a"

#: 会被平台判定为「无资质发布时政内容」的词。刻意取窄：
#: 宁可漏一个真事，也不要把「球员参加国家队集训」这类正常稿误杀。
_BOUNDARY = (
    "总书记", "中共中央", "政治局", "国务院", "外交部", "国防部", "人大", "政协",
    "意识形态", "维稳", "台独", "港独", "疆独", "藏独", "修宪", "领导人",
)

#: 重度洗稿阈值：取在告警线之上，正常改写不会触发。
SEVERE_RUN = 200
SEVERE_GRAM = 0.50

#: 平台在《社区规范·标题低质》里**点名**的耸动词（原文举例「震惊」「重磅」
#: 「全世界XX」「根治」）。刻意贴原文取词，不自造近义词表——
#: 词表一旦自由发挥就会变成「判据短串误报」的又一次重演。
_SENSATIONAL = (
    "震惊", "重磅", "炸裂", "惊天", "全世界都", "史上最", "绝无仅有",
    "彻底炸了", "全网都在", "不看后悔", "万万没想到", "颠覆认知",
)


# ──────────────────────────────────────────────────────────── 基础
def load_rules(path=None) -> dict:
    import yaml
    p = Path(path or RULES_PATH)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("rules"), list) or not data["rules"]:
        raise ValueError(f"规则表为空或格式不对：{p}")
    # 把顶层 `publish_scope` 挂到每条规则上：判据拿得到，又不必自己再读一遍
    # 规则表。多开一条读取路径就多一个「两处定义」的口子。
    scope = data.get("publish_scope") or {}
    for r in data["rules"]:
        if isinstance(r, dict):
            r["_publish_scope"] = scope
    return data


def iter_items(days: int, since: str = SINCE) -> list[dict]:
    """读近 N 天的成稿记录（只读 metadata，正文按需懒加载）。"""
    out: list[dict] = []
    today = date.today()
    for i in range(max(1, days)):
        ds = (today - timedelta(days=i)).isoformat()
        if ds < since:
            continue
        p = ROOT / "output" / ds / "metadata.json"
        if not p.exists():
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for art in (meta.get("articles") or []):
            if isinstance(art, dict):
                out.append({"date": ds, "dir": p.parent, "art": art})
    return out


def body_of(item: dict) -> str | None:
    """读成稿正文。读不到返回 None —— **绝不拿标题冒充正文**。

    metadata 里的 ``path`` 是生成它的 runner 上的绝对路径
    （``/home/runner/work/_temp/...``），跨批次读不到是常态。
    这里按 basename 与 slug 在本地同类目录找，找不到就是找不到。
    """
    if "_body" in item:
        return item["_body"]
    art = item.get("art") or {}
    d = Path(item["dir"])
    cands: list[Path] = []
    p = str(art.get("path") or "")
    if p:
        cands.append(d / Path(p).name)
    slug = str(art.get("slug") or "")
    if slug:
        cands.extend(sorted(d.glob(f"*{slug}*.md")))
    for c in cands:
        try:
            if c.is_file():
                item["_body"] = c.read_text(encoding="utf-8")
                return item["_body"]
        except Exception:
            continue
    item["_body"] = None
    return None


def _res(status: str, detail: str, hits=None) -> dict:
    return {"status": status, "detail": detail, "hits": list(hits or [])}


# ──────────────────────────────────────────────────────────── 判据
def j_compliance_and_gambling(item: dict, rule: dict) -> dict:
    """TT-01 违法违规：人工熔断状态 + 博彩话术。"""
    try:
        import compliance_guard
        st = compliance_guard.status() or {}
        if st.get("suspended") or st.get("is_suspended"):
            return _res(FAIL, f"合规已熔断：{st.get('reason') or '见 data/audit/compliance_state.json'}")
    except Exception as exc:
        note = f"（合规状态不可读：{type(exc).__name__}）"
    else:
        note = ""

    body = body_of(item)
    if body is None:
        return _res(UNJ, "正文不可读，博彩话术无法判定" + note)
    import interaction_bait
    r = interaction_bait.check_inducement(body)
    gamb = [h for h in (r.get("hits") or []) if h.get("group") == "博彩"]
    if gamb:
        return _res(FAIL, "正文含博彩术语：" + "、".join(h["pattern"] for h in gamb),
                    [f"赌博/博彩·{rule['credit']}分档"])
    if r.get("degraded"):
        return _res(UNJ, "互动话术配置不可用，未判定" + note)
    return _res(PASS, "无博彩话术、合规未熔断" + note)


def j_fact_conflict(item: dict, rule: dict) -> dict:
    """TT-02 / TT-05 事实矛盾：读 ``_gate.fact``（不另立判据）。"""
    g = (item["art"].get("_gate") or {}).get("fact")
    if not isinstance(g, dict):
        return _res(UNJ, "metadata 无 _gate.fact（这篇没经过 v2 闸门或被清空）")
    contra = list(g.get("contradictions") or []) + list(g.get("legacy_conflicts") or [])
    if contra:
        return _res(FAIL, "事实矛盾：" + "；".join(str(c)[:60] for c in contra[:3]),
                    [str(c)[:60] for c in contra[:3]])
    if not g.get("judged"):
        return _res(UNJ, "闸门判 judged=False（空事实卡，未判定 ≠ 通过）")
    return _res(PASS, f"无事实矛盾（覆盖率 {g.get('coverage')}）")


def j_rumor_tier(item: dict, rule: dict) -> dict:
    """TT-02 / TT-04 传闻当官宣：分级未知 → 未判定。"""
    import rumor_wording as rw
    art = item["art"]
    tier = rw.tier_of(art)
    if not tier:
        return _res(NA, "本篇事实分级未知（非传闻类），不适用")
    body = body_of(item) or ""
    r = rw.check(tier, str(art.get("title") or ""), body)
    if r.get("hard"):
        return _res(FAIL, "传闻当官宣：" + "；".join(r["hard"][:2]), r["hard"][:2])
    return _res(PASS, f"分级 {tier}，措辞合规")


def j_freshness(item: dict, rule: dict) -> dict:
    """TT-03 时效：只判「有证据的」那一半。

    素材侧 36h 闸门在 ``hot_sources`` 里（生成前），成稿侧这里只查
    metadata 自带的 ``publish_by`` / ``source_age_hours``。
    两者都没有 → 未判定，**不许拿「篇数正常」当通过**。
    """
    art = item["art"]
    pb = str(art.get("publish_by") or "").strip()
    if pb:
        try:
            if datetime.fromisoformat(pb.replace("Z", "")) < datetime.fromisoformat(item["date"]):
                return _res(FAIL, f"publish_by={pb} 早于成稿日 {item['date']}（内容已过期）")
            return _res(PASS, f"publish_by={pb} 有效")
        except Exception:
            pass
    age = art.get("source_age_hours")
    if isinstance(age, (int, float)):
        if age > 36:
            return _res(FAIL, f"素材时效 {age}h 超过 36h 闸门")
        return _res(PASS, f"素材时效 {age}h")
    return _res(UNJ, "无 publish_by / source_age_hours 证据；素材侧由 hot_sources 36h 闸门覆盖")


def j_title_gate(item: dict, rule: dict) -> dict:
    """TT-04 标题党：只判**平台明列的三类**里的两类。

    平台对「标题低质」的定义是三类：① 夸张耸动 ② 把传闻/不实消息说成确定信息
    ③ 标题与内容原意有偏差。能判的是 ①②：
      - ② 复用 ``rumor_wording``（与 TT-02 同一实现，不另写一套）；
      - ① 用平台在细则里**点名**的耸动词表（震惊/重磅/炸裂/全世界…）。
    ③ 需要「标题 ↔ 正文」语义比对，当前没有判据 → 在规则表登记为
    ``coverage_gap``，由 ``verify_rule_coverage.py`` 报出来，不在这里假装判过。

    **刻意不用 `score_title_plan` 的 0 分**：那个 0 分里混着三要素、赛果播报、
    疑问收尾限额——全是提点击率的内部质量规则（见规则表 out_of_scope.quality）。
    把质量分当红线，就是「审得严但审错对象」本身。
    """
    title = str(item["art"].get("title") or "").strip()
    if not title:
        return _res(NA, "无标题")

    import rumor_wording as rw
    tier = rw.tier_of(item["art"])
    r = rw.check(tier, title, "")
    if r.get("hard"):
        return _res(FAIL, "把传闻写成确定信息：" + "；".join(r["hard"][:2]), r["hard"][:2])

    sens = [w for w in _SENSATIONAL if w in title]
    if sens:
        return _res(FAIL, "标题夸张耸动：" + "、".join(sens[:4]), sens[:4])

    gap = " ".join(str(rule.get("coverage_gap") or "").split())
    return _res(PASS, "无耸动词、无传闻断言" + (f"（未覆盖：{gap[:40]}…）" if gap else ""))


def j_plagiarism(item: dict, rule: dict) -> dict:
    """TT-06 抄袭侵权：轻度记分、**重度必拦**。

    2026-11-05 把反洗稿降为「只记分」，理由是「只影响有没有人看」——
    在平台规则面前不成立：洗稿对应「抄袭侵权」（社区规范·平台不允许），
    首发计划还明列「非原创非首发每篇扣 5 分」。
    这里只拦重度（连续重合 >200 字 或 5-gram >50%），正常改写不会触发。
    """
    art = item["art"]
    sim = art.get("_similarity") or {}
    ac = art.get("_anti_copy") or {}
    run = int(sim.get("max_run") or 0)
    gram = float(sim.get("gram_ratio") or 0.0)
    issues = list(ac.get("issues") or [])
    if not sim and not ac:
        return _res(UNJ, "无相似度/信息增量记录（未走改写路径或未记分）")
    if run > SEVERE_RUN or gram > SEVERE_GRAM:
        return _res(FAIL, f"重度洗稿：连续重合 {run} 字 / 5-gram {gram:.0%}",
                    [f"连续重合{run}字", f"5-gram{gram:.0%}"])
    if issues:
        return _res(PASS, f"轻度相似（{run} 字 / {gram:.0%}），只记分：" + "；".join(issues[:2]))
    return _res(PASS, f"相似度 {run} 字 / {gram:.0%}，未达重度线")


def j_inducement(item: dict, rule: dict) -> dict:
    """TT-07 / TT-08 诱导交互与违规涨粉。"""
    body = body_of(item)
    if body is None:
        return _res(UNJ, "正文不可读，诱导话术无法判定")
    import interaction_bait
    r = interaction_bait.check_inducement(body)
    if r.get("degraded"):
        return _res(UNJ, "配置不可用，未判定")
    hits = r.get("hits") or []
    if hits:
        return _res(FAIL, "命中诱导话术：" + "、".join(sorted({h["group"] for h in hits})),
                    [h["pattern"] for h in hits[:5]])
    return _res(PASS, "无诱导话术")


def j_image_relevance(item: dict, rule: dict) -> dict:
    """TT-09 图文不符：逐张图核 anchor（复用 data_chart.entity_hits 单一口径）。"""
    anchors = item["art"].get("image_anchors") or []
    if not anchors:
        return _res(NA, "本篇无数据图锚点（只有信息图/新闻图），本条不适用")
    body = body_of(item)
    if body is None:
        return _res(UNJ, "正文不可读，无法核图是否对文")
    import image_relevance
    bad = []
    for a in anchors:
        anchor = (a or {}).get("anchor") if isinstance(a, dict) else None
        r = image_relevance.check(anchor or {}, {"content": body})
        if not r.get("ok"):
            bad.append(f"{a.get('file', '?')}：{r.get('reason')}")
    if bad:
        return _res(FAIL, "图文不符：" + "；".join(bad[:2]), bad[:2])
    return _res(PASS, f"{len(anchors)} 张数据图实体/数字全部命中正文")


def j_structure(item: dict, rule: dict) -> dict:
    """TT-10 格式低质：段落数 / 乱码 / 相邻重复段（全是确定性判定）。"""
    body = body_of(item)
    if body is None:
        return _res(UNJ, "正文不可读，格式无法判定")
    text = body.strip()
    if not text:
        return _res(FAIL, "正文为空")
    paras = [ln.strip() for ln in text.splitlines() if ln.strip()]
    problems = []
    if len(paras) < 4:
        problems.append(f"段落仅 {len(paras)} 段（<4，疑似一句话成稿）")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]", text):
        problems.append("含乱码/控制字符")
    dup = set()
    for i in range(1, len(paras)):
        if len(paras[i]) >= 30 and paras[i] == paras[i - 1]:
            dup.add(paras[i][:24])
    if dup:
        problems.append(f"相邻段落重复 {len(dup)} 处")
    if problems:
        return _res(FAIL, "格式低质：" + "；".join(problems), problems)
    return _res(PASS, f"{len(paras)} 段，无乱码与重复段")


def j_ai_annotation(item: dict, rule: dict) -> dict:
    """TT-11 失实表述·AI 未标识。

    优先看正文里的显式标识；正文不可读时退到 metadata 标记
    （``append_ai_annotation`` 在写入同一段文字时置位，属同一事实的证据，
    不是拿别的东西冒充）。两个都没有 → 未判定。
    """
    body = body_of(item)
    if body is not None:
        if "本文由 AI 生成" in body or "AI 生成" in body:
            return _res(PASS, "正文含 AI 标识")
        return _res(FAIL, "正文缺「本文由 AI 生成」标识（平台明列失实表述 10 分）")
    if item["art"].get("ai_generated") or item["art"].get("ai_annotation_added"):
        return _res(PASS, "正文不可读，但 metadata 标记 ai_generated（同一事实的旁证）")
    return _res(UNJ, "正文不可读且无 ai_generated 标记")


def j_first_publish(item: dict, rule: dict) -> dict:
    """TT-12 首发：判「有没有可能在别的平台先发过」。

    这一条刻意**不写死**「我们只发头条」。那个事实属于配置
    （``publish_scope.platforms``），不属于代码：写死在代码里，哪天接了
    公众号分发，判据会继续判 pass —— 就是「规则靠谁记得」的又一次重演，
    而这次代价是 30 天内最多扣 50 分。

    三态划分：
      - 只有头条一个平台 → pass（客观满足 72 小时独家）；
      - 有第二个平台 → 未判定（窗口要人去核，脚本不猜）；
      - 配置缺失/为空 → 未判定（**不默认当首发**）。

    原创那一半不在本判据里，由 TT-06 抄袭侵权判（见规则表 depends_on）。
    """
    scope = rule.get("_publish_scope") or {}
    plats = [str(x).strip().lower() for x in (scope.get("platforms") or []) if str(x).strip()]
    if not plats:
        return _res(UNJ, "publish_scope.platforms 未配置：无法确认是否首发，不默认当首发")
    if len(plats) > 1:
        return _res(UNJ, "多平台分发（" + "/".join(plats) + "）：需人工核 72 小时窗口")
    if plats[0] != "toutiao":
        return _res(UNJ, f"唯一平台是 {plats[0]}：非头条，本条应改 n/a")
    return _res(PASS, "单平台（仅头条号）：72 小时内仅在头条发布客观成立")


def j_topic_boundary(item: dict, rule: dict) -> dict:
    """TT-13 无资质发布时政内容：标题 + 正文负面词表。"""
    title = str(item["art"].get("title") or "")
    body = body_of(item) or ""
    text = title + "\n" + body
    hit = [w for w in _BOUNDARY if w in text]
    if hit:
        return _res(FAIL, "涉时政/敏感表述：" + "、".join(hit[:4]), hit[:4])
    if body_of(item) is None:
        return _res(UNJ, "正文不可读，仅查了标题")
    return _res(PASS, "无时政类表述")


def j_low_quality_aigc(items: list[dict], rule: dict) -> dict:
    """TT-14 低质 AIGC（账号级）：结尾话术重复率 + 相邻篇句子重合。

    只 warn：平台没有公开阈值，拿单一数字硬拦会误杀正常稿。
    但必须被看见——它没有分值，却会直接导致限流/禁言。
    """
    tails, bodies = [], []
    for it in items:
        b = body_of(it)
        if not b:
            continue
        paras = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if paras:
            tails.append(paras[-1][:40])
        bodies.append(b)
    if len(tails) < 5:
        return _res(UNJ, f"正文可读样本仅 {len(tails)} 篇（<5），样本不足")
    from collections import Counter
    top, n = Counter(tails).most_common(1)[0]
    rate = n / len(tails)
    if rate >= 0.5:
        return _res(FAIL, f"结尾话术重复率 {rate:.0%}（「{top[:20]}」出现 {n}/{len(tails)} 篇）",
                    [f"{rate:.0%} 结尾相同"])
    return _res(PASS, f"结尾话术重复率 {rate:.0%}，样本 {len(tails)} 篇")


#: 规则 id → 判据。批量级判据接收 items 列表，单篇级接收 item。
_SINGLE = {
    "compliance_and_gambling": j_compliance_and_gambling,
    "fact_conflict": j_fact_conflict,
    "rumor_tier": j_rumor_tier,
    "freshness": j_freshness,
    "title_gate": j_title_gate,
    "plagiarism": j_plagiarism,
    "inducement": j_inducement,
    "image_relevance": j_image_relevance,
    "structure": j_structure,
    "ai_annotation": j_ai_annotation,
    "first_publish": j_first_publish,
    "topic_boundary": j_topic_boundary,
}
_BATCH = {"low_quality_aigc": j_low_quality_aigc}

#: 规则表的 judge 字段可以是 `a + b`（一条规则两截判据），按这个拆。
_SPLIT = re.compile(r"\s*\+\s*")


def _judge_names(rule: dict) -> list[str]:
    raw = str(rule.get("judge") or "").strip()
    return [x for x in _SPLIT.split(raw) if x]


def _merge(results: list[dict], total: int = 0) -> dict:
    """一条规则多截判据的合并：任一 FAIL → FAIL；否则任一无判 → 未判定。

    细节去重并按「几篇命中/未判定」计数 —— 每篇重复同一句
    「正文不可读」是没人愿意读的报告。
    """
    if not results:
        return _res(UNJ, "规则未绑定判据")
    n = total or len(results)

    def _detail(sub: list[dict]) -> str:
        uniq: list[str] = []
        for r in sub:
            if r["detail"] not in uniq:
                uniq.append(r["detail"])
        return "；".join(uniq[:3]) + ("…" if len(uniq) > 3 else "")

    fails = [r for r in results if r["status"] == FAIL]
    if fails:
        return _res(FAIL, f"{len(fails)}/{n} 篇命中：{_detail(fails)}",
                    [h for r in fails for h in r["hits"]])
    unj = [r for r in results if r["status"] == UNJ]
    if unj:
        return _res(UNJ, f"{len(unj)}/{n} 篇未判定：{_detail(unj)}")
    if all(r["status"] == NA for r in results):
        return _res(NA, results[0]["detail"])
    return _res(PASS, _detail([r for r in results if r["status"] == PASS]))


#: 出口处置：命中即**丢这一篇**的档位。
#: 为什么 rewrite 也丢：发布前的 `verify_compliance` 把 block 与 rewrite 一并算进
#: blocking（都「会被扣分」）。出口若放行 rewrite，发布前那层就会停发**整批**——
#: 一篇格式问题放大成整批 0 篇（11-06 午间就是这么来的）。逐篇丢永远优于整批停。
_DROP_TIERS = ("block", "rewrite")


def audit_article(art: dict, date_str: str, rules: dict | None = None,
                  body: str | None = None) -> dict:
    """出口**单篇**合规审核（2026-11-06 立，唯一逐篇处置点）。

    为什么要有这一层：合规闸门原先只装在 ``_rewrite_with_retry`` 一条通道上，
    其余 6 个 ``generate_*``（一级确定性供给 4 个 + 紧急球评 + 前瞻）一个都没有，
    于是「一半稿件从生成到发布，合规判定为零」，全压到发布前那一层——而那一层
    命中就是**停发整批**。收口在出口而不是逐个生成函数，是因为出口是所有通道的
    唯一汇聚点（同 ``save_articles_local`` 里三要素兜底先例）。

    判据**不重写**：一律调本模块的 ``j_*``，与发布前、每日层是同一份实现，
    规则清单也从 ``config/toutiao_rules.yaml`` 读，不在代码里再列一遍。

    返回结构（会被原样落到 metadata 的 ``_compliance``，成功也要写——
    「成功就不写字段」正是 11-06 认定审核空转的根因）：

        {"drop": bool, "blocked_by": [规则id], "risk_score": int,
         "judged": 判定出的条目数, "total": 应判条目数, "verdicts": [...]}
    """
    rules = rules or load_rules()
    # dir 指向当天的输出目录：正文由调用方直接给（body），不给才回去找文件。
    item = {"date": str(date_str), "dir": ROOT / "output" / str(date_str), "art": art}
    if body is not None:
        item["_body"] = body

    verdicts: list[dict] = []
    blocked: list[dict] = []
    for rule in rules["rules"]:
        tier = str(rule.get("tier") or "warn")
        if tier == "n/a":
            continue
        results: list[dict] = []
        for name in _judge_names(rule):
            fn = _SINGLE.get(name)
            if fn:
                results.append(fn(item, rule))
            elif name in _BATCH:
                # 批量级判据要 ≥5 篇可比样本，出口只有一篇。**登记为未判定**，
                # 不静默跳过——跳过就等于「出口说没问题」，而它根本没看。
                results.append(_res(UNJ, "批量级判据（需 ≥5 篇可比样本），出口单篇不判；"
                                         "由发布前 verify_compliance 与每日层负责"))
            else:
                results.append(_res(UNJ, f"规则绑定的判据 `{name}` 不存在（规则表写错了）"))
        merged = _merge(results, 1)
        # `_merge` 是给「N 篇」用的，单篇会得到「1/1 篇命中：…」这种前缀。
        # 出口的结论是「这一篇命中」，不是「1 篇命中 1 篇」——去掉前缀再落盘，
        # 否则 metadata 里每篇都挂一句废话。
        detail = merged["detail"]
        for _p in ("1/1 篇命中：", "1/1 篇未判定："):
            if detail.startswith(_p):
                detail = detail[len(_p):]
                break
        row = {"id": rule["id"], "name": rule["name"], "tier": tier,
               "credit": int(rule.get("credit") or 0),
               "status": merged["status"], "detail": detail,
               "hits": (merged.get("hits") or [])[:5]}
        verdicts.append(row)
        if merged["status"] == FAIL and tier in _DROP_TIERS:
            blocked.append(row)

    return {
        "drop": bool(blocked),
        "blocked_by": [b["id"] for b in blocked],
        "risk_score": sum(b["credit"] for b in blocked),
        "judged": len([v for v in verdicts if v["status"] in (PASS, FAIL)]),
        "total": len(verdicts),
        "verdicts": verdicts,
    }


def drop_violations(date_str: str, rules: dict | None = None) -> dict:
    """发布前**逐篇剔除**：命中 block/rewrite 的成稿移出发布集合（不停整批）。

    为什么需要它（2026-11-06）：``verify_compliance`` 原本只会说「本批有违规」，
    而 workflow 拿到这句话只能停发整批 —— 一篇的图不对 / 一句话涉赌，代价是
    当批 0 篇（11-06 午间实证）。出口审核（``audit_article``）已经在生成侧
    逐篇丢过一次，这一层是给两类漏网情形兜底：
      · 出口审核抛异常被放行（`_compliance.error` 有值）；
      · 成稿不是经 ``save_articles_local`` 出去的。

    剔除方式：``output/<date>/article-*.md`` 移进 ``output/<date>/dropped/``。
    发布侧（``publisher.py``）是 glob 顶层 ``article-*.md``，移进子目录即自然
    移出发布集合，不必改发布器；metadata 里的记录**保留**并加
    ``dropped_by_compliance``，否则事后又变成「这篇为什么没发」的黑箱。

    幂等：重复调用只是再移一次已空集合，不会二次破坏。
    """
    rules = rules or load_rules()
    date_dir = ROOT / "output" / str(date_str)
    meta_path = date_dir / "metadata.json"
    if not meta_path.exists():
        return {"date": str(date_str), "dropped": [], "kept": 0,
                "note": "无 metadata.json（本批未产出）"}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"date": str(date_str), "dropped": [], "kept": 0,
                "note": f"metadata 不可读：{type(exc).__name__}: {exc}"}

    arts = [a for a in (meta.get("articles") or []) if isinstance(a, dict)]
    dropped_dir = date_dir / "dropped"
    dropped: list[dict] = []
    for art in arts:
        if art.get("dropped_by_compliance"):
            # 已经剔过的不再判第二次：md 已被移走，重判只会拿不到正文、
            # 把原来那条「为什么被剔」的证据覆盖成「正文不可读」——
            # 幂等操作不许毁掉第一次的结论。
            continue
        item = {"date": str(date_str), "dir": date_dir, "art": art}
        body = body_of(item)
        res = audit_article(art, date_str, rules=rules, body=body)
        art["_compliance"] = res
        if not res["drop"]:
            continue
        moved = ""
        p = str(art.get("path") or "")
        cand = date_dir / Path(p).name if p else None
        if cand and cand.is_file():
            try:
                dropped_dir.mkdir(parents=True, exist_ok=True)
                os.replace(cand, dropped_dir / cand.name)
                moved = str(dropped_dir / cand.name)
            except OSError as exc:
                moved = f"（移出失败：{type(exc).__name__}: {exc}）"
        art["dropped_by_compliance"] = {
            "blocked_by": res["blocked_by"], "risk_score": res["risk_score"],
            "moved_to": moved,
            "verdicts": [v for v in res["verdicts"] if v["id"] in res["blocked_by"]],
        }
        dropped.append({"title": art.get("title", ""),
                        "blocked_by": res["blocked_by"],
                        "risk_score": res["risk_score"], "moved_to": moved})

    if dropped:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    out = {"date": str(date_str), "generated_at": datetime.now().isoformat(timespec="seconds"),
           "dropped": dropped,
           # kept 要按**残留标记**算，不能按「这轮剔了几篇」算：重复调用时本轮
           # 剔 0 篇，但发布集合里确实少了一篇，报 kept=2 就是在骗下游。
           "kept": len([a for a in arts if not a.get("dropped_by_compliance")]),
           "total": len(arts)}
    try:
        (AUDIT_DIR / f"compliance_dropped_{date_str}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return out


def verify(days: int = 7, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    items = iter_items(days)
    report: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": days,
        "since": SINCE,
        "articles": len(items),
        "rules": [],
        "blocking": [],
        "risk_score": 0,
    }

    for rule in rules["rules"]:
        names = _judge_names(rule)
        tier = str(rule.get("tier") or "warn")
        if tier == "n/a":
            report["rules"].append({
                "id": rule["id"], "name": rule["name"], "credit": rule.get("credit", 0),
                "tier": tier, "status": NA, "hits": [],
                "detail": "规则表标记不适用",
            })
            continue

        results = []
        for name in names:
            fn = _SINGLE.get(name)
            if fn:
                results.extend(fn(it, rule) for it in items)
            elif name in _BATCH:
                results.append(_BATCH[name](items, rule))
            else:
                results.append(_res(UNJ, f"规则绑定的判据 `{name}` 不存在（规则表写错了）"))

        merged = _merge(results, len(items)) if items else _res(UNJ, "窗口内无成稿记录")
        failed = [r for r in results if r["status"] == FAIL]
        entry = {
            "id": rule["id"], "name": rule["name"], "credit": rule.get("credit", 0),
            "tier": tier, "status": merged["status"],
            "violations": len(failed), "detail": merged["detail"],
            "hits": merged["hits"][:8],
        }
        report["rules"].append(entry)
        if merged["status"] == FAIL and tier in ("block", "rewrite"):
            report["blocking"].append(rule["id"])
            report["risk_score"] += int(rule.get("credit") or 0)

    return report


# ──────────────────────────────────────────────────────────── 负向自检
def selftest() -> list[tuple[str, bool]]:
    """每条 block 级规则都必须能被打红 —— 否则它就是恒真的摆设。"""
    rules = {r["id"]: r for r in load_rules()["rules"]}
    cases: list[tuple[str, bool]] = []

    clean = ("这是一段正常的正文，讲的是那不勒斯客场的事。\n\n"
             "第二段说说中场的变化。\n\n第三段讲教练的调整。\n\n"
             "第四段收尾。你怎么看？\n\n> 本文由 AI 生成\n")

    def _it(body, **art):
        base = {"title": "正常的标题，讲那不勒斯的中场", "publish_by": "2026-11-07T00:00:00",
                "_gate": {"fact": {"judged": True, "contradictions": []}},
                "ai_generated": True, "_similarity": {"max_run": 30, "gram_ratio": 0.12}}
        base.update(art)
        return {"date": date.today().isoformat(), "dir": ROOT / "output", "art": base,
                "_body": body}

    ok = _it(clean)
    cases.append(("干净样本：诱导话术不误报", j_inducement(ok, rules["TT-07"])["status"] == PASS))
    cases.append(("干净样本：结构不误报", j_structure(ok, rules["TT-10"])["status"] == PASS))
    cases.append(("干净样本：AI 标识已具备", j_ai_annotation(ok, rules["TT-11"])["status"] == PASS))
    cases.append(("干净样本：时效有效", j_freshness(ok, rules["TT-03"])["status"] == PASS))

    bad = _it(clean + "\n\n点个关注，后续我替你盯着。")
    cases.append(("索要关注 → TT-07 必红", j_inducement(bad, rules["TT-07"])["status"] == FAIL))
    cases.append(("索要关注 → TT-08 必红", j_inducement(bad, rules["TT-08"])["status"] == FAIL))

    cases.append(("博彩术语 → TT-01 必红",
                  j_compliance_and_gambling(_it("这单我押注主队，盘口很好看。"),
                                            rules["TT-01"])["status"] == FAIL))
    cases.append(("事实矛盾 → TT-05 必红",
                  j_fact_conflict(_it(clean, _gate={"fact": {"judged": True,
                                                             "contradictions": ["比分写反"]}}),
                                  rules["TT-05"])["status"] == FAIL))
    cases.append(("闸门未判 → TT-05 未判定（不是通过）",
                  j_fact_conflict(_it(clean, _gate={"fact": {"judged": False}}),
                                  rules["TT-05"])["status"] == UNJ))
    cases.append(("重度洗稿 → TT-06 必红",
                  j_plagiarism(_it(clean, _similarity={"max_run": 320, "gram_ratio": 0.61}),
                               rules["TT-06"])["status"] == FAIL))
    cases.append(("轻度相似 → TT-06 不拦（只记分）",
                  j_plagiarism(_it(clean, _similarity={"max_run": 60, "gram_ratio": 0.2}),
                               rules["TT-06"])["status"] == PASS))
    cases.append(("一句话成稿 → TT-10 必红",
                  j_structure(_it("就一句。"), rules["TT-10"])["status"] == FAIL))
    cases.append(("缺 AI 标识 → TT-11 必红",
                  j_ai_annotation(_it("正常正文，但没有标识。"), rules["TT-11"])["status"] == FAIL))
    cases.append(("图不对文 → TT-09 必红",
                  j_image_relevance(
                      _it(clean, image_anchors=[{"file": "a.jpg",
                                                 "anchor": {"entities": ["国际米兰"]}}]),
                      rules["TT-09"])["status"] == FAIL))
    cases.append(("涉时政 → TT-13 必红",
                  j_topic_boundary(_it("球员谈到国务院的政策。"), rules["TT-13"])["status"] == FAIL))
    cases.append(("耸动词 → TT-04 必红",
                  j_title_gate(_it(clean, title="震惊！重磅消息，那不勒斯要换帅"),
                               rules["TT-04"])["status"] == FAIL))
    cases.append(("三要素不足但无耸动词 → TT-04 不拦（那是质量分）",
                  j_title_gate(_it(clean, title="领跑，那不勒斯这一关，队长憋的劲该往哪使"),
                               rules["TT-04"])["status"] == PASS))
    cases.append(("正文读不到 → 一律未判定",
                  j_inducement({"date": "2026-11-06", "dir": ROOT / "nope",
                                "art": {"title": "x"}, "_body": None},
                               rules["TT-07"])["status"] == UNJ))

    # TT-12 首发：三种平台配置必须给出三种答案，pass 不能靠写死
    def _tt12(platforms):
        return j_first_publish(_it(clean), {"_publish_scope": {"platforms": platforms}})["status"]
    cases.append(("单平台（仅头条）→ TT-12 判 pass", _tt12(["toutiao"]) == PASS))
    cases.append(("多平台分发 → TT-12 未判定（不是 pass）",
                  _tt12(["toutiao", "wechat"]) == UNJ))
    cases.append(("平台清单为空 → TT-12 未判定（不默认当首发）", _tt12([]) == UNJ))
    cases.append(("唯一平台不是头条 → TT-12 未判定", _tt12(["wechat"]) == UNJ))
    return cases


def render(report: dict) -> str:
    lines = [f"合规审核（近 {report['window_days']} 天 · {report['articles']} 篇 · 规则表 {report['since']} 起）", ""]
    icon = {PASS: "✅", FAIL: "❌", UNJ: "⚠️", NA: "—"}
    for r in report["rules"]:
        lines.append(f"  {icon.get(r['status'], '?')} {r['id']} {r['name']}"
                     f"（{r['credit']} 分档/{r['tier']}）")
        lines.append(f"      {r['detail']}")
    lines.append("")
    block_rules = [r for r in report["rules"] if r["tier"] in ("block", "rewrite")]
    judged = [r for r in block_rules if r["status"] in (PASS, FAIL)]
    if report["blocking"]:
        lines.append(f"❌ 不该发布：命中 {len(report['blocking'])} 条规则 "
                     f"{report['blocking']}，潜在扣分合计 {report['risk_score']} 分")
    else:
        unj = [r for r in report["rules"] if r["status"] == UNJ]
        lines.append(f"✅ 无 block 级违规（block 级规则判定 {len(judged)}/{len(block_rules)} 条 / "
                     f"未判定 {len(unj)} 条 / 不适用 "
                     f"{len([r for r in report['rules'] if r['status'] == NA])} 条）")
        if unj:
            lines.append("   ⚠️ 未判定不等于通过：" + "、".join(r["id"] for r in unj))
        if len(judged) * 2 < len(block_rules):
            lines.append("   ⚠️ 过半 block 级规则未判定，本次**不能**视为合规通过（rc=2）。"
                         "常见原因：正文留在了生成它的 runner 上。")
    return "\n".join(lines)


def _reason_of(detail: str) -> str:
    """把「未判定」的原因归成可数的桶——**只用于展示分组，不参与任何判定**。

    不按规则 id 硬编码映射：那等于把规则清单在本文件里再列一遍，规则表一改就
    对不上（2026-11-06 已经因为这个交付过两次事故）。按 detail 里的特征词归类，
    归不进去的落 other 并把原话打出来，宁可显示「other」也不猜。
    """
    d = str(detail or "")
    if "批量级判据" in d:
        return "batch"          # 需 ≥5 篇样本，出口单篇本来就不判
    if "正文" in d and ("不可读" in d or "读不到" in d or "找不到" in d):
        return "no_body"        # 环境问题：正文留在生成它的 runner 上
    if any(k in d for k in ("无 _gate", "无 publish_by", "无相似度", "无…证据",
                            "证据不足", "未记录", "元数据")):
        return "field_missing"  # 代码问题：CI 里照样未判定，必须修
    return "other"


def evidence_report(date_str: str | None = None, days: int = 1) -> dict:
    """批次后核对：**证据到底落没落**、没落的是哪一类。

    为什么单独做这个视图：``verify()`` 回答的是「有没有违规」，而批次刚跑完时
    真正该问的是「这次到底判了几条」。两者混在一起会得出「无 block 级违规」
    这种看着绿、其实什么都没判的结论（2026-11-06 的 rc=0 假绿就是这么来的）。

    不设 PASS/FAIL：这是诊断视图，不是闸门。硬要给它一个通过线，就等于又
    发明了一套阈值。
    """
    d = date_str or date.today().isoformat()
    day_dir = ROOT / "output" / str(d)
    meta_path = day_dir / "metadata.json"
    out: dict = {"date": str(d), "articles": 0, "has_meta": meta_path.is_file(),
                 "per_article": [], "no_compliance_field": [],
                 "undecided": {}, "reasons": {}, "dropped": []}
    if not out["has_meta"]:
        return out

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    arts = meta.get("articles") or []
    out["articles"] = len(arts)
    for a in arts:
        c = a.get("_compliance")
        title = str(a.get("title") or "")[:30]
        if not isinstance(c, dict):
            # 老产物 / 出口审核没跑到这一篇。**不算通过**，也不算未判定——
            # 它连「判过」这个事实都没有，混进分母会把两次改动的效果搅在一起。
            out["no_compliance_field"].append(title)
            continue
        out["per_article"].append({"title": title, "judged": c.get("judged"),
                                   "total": c.get("total"), "drop": c.get("drop"),
                                   "error": c.get("error")})
        for v in c.get("verdicts") or []:
            if v.get("status") != UNJ:
                continue
            rid = v.get("id") or "?"
            out["undecided"].setdefault(rid, []).append(title)
            bucket = _reason_of(v.get("detail"))
            out["reasons"].setdefault(bucket, {}).setdefault(rid, v.get("detail", ""))

    for x in meta.get("compliance_dropped") or []:
        out["dropped"].append({"title": str(x.get("title") or "")[:30],
                               "blocked_by": x.get("blocked_by") or []})
    return out


def render_evidence(rep: dict) -> str:
    d = rep["date"]
    if not rep["has_meta"]:
        return f"本批证据核对（{d}）：⚠️ 没有 output/{d}/metadata.json（批次没产出或还没跑）"
    if rep.get("error"):
        return f"本批证据核对（{d}）：❌ metadata 读不了：{rep['error']}"

    lines = [f"本批证据核对（{d} · {rep['articles']} 篇成稿）", ""]
    rows = rep["per_article"]
    if rows:
        lines.append("  逐篇判定数（judged/total）：")
        for r in rows:
            flag = "⛔" if r["drop"] else ("⚠️" if r["error"] else "  ")
            err = f"  异常：{r['error']}" if r["error"] else ""
            lines.append(f"   {flag} {r['judged']}/{r['total']}  {r['title']}{err}")
    if rep["no_compliance_field"]:
        lines.append(f"  ⚠️ {len(rep['no_compliance_field'])} 篇没有 _compliance 字段"
                     f"（出口审核没跑到，多半是旧产物或已剔稿）："
                     f"{'、'.join(rep['no_compliance_field'])}")
    if rep["dropped"]:
        lines.append("")
        lines.append(f"  被合规剔除 {len(rep['dropped'])} 篇（不进发布集合）：")
        for x in rep["dropped"]:
            lines.append(f"   ⛔ {x['title']} ← {'、'.join(x['blocked_by'])}")

    lines.append("")
    if not rows:
        # 「没有未判定项」和「一条都没判」在刚才那个写法里是同一句话 —— 4 篇
        # 全缺 _compliance 时会打印「✅ 每条规则都真的判过了」。**那正是本项目
        # 反复栽的「拿缺失当通过」**，这里必须把两种情形分开。
        lines.append("  ❌ 0 篇有判定记录：这一批等于**没审过**，不能视为通过")
        lines.append("     说明出口审核没跑到这些篇上（旧产物 / 改动前的批次 / 审核异常）")
        return "\n".join(lines)
    if not rep["undecided"]:
        lines.append(f"  ✅ {len(rows)} 篇均有判定记录，且没有未判定项："
                     f"这一批每条规则都真的判过了")
        return "\n".join(lines)

    label = {"field_missing": "证据缺失（代码问题 · CI 里照样未判定，必须修）",
             "no_body": "正文不可读（环境问题 · 跨 runner，CI 里应显著减少）",
             "batch": "批量级（需 ≥5 篇样本，出口单篇按设计不判）",
             "other": "未归类（看下面的原话，别猜）"}
    lines.append(f"  未判定 {len(rep['undecided'])} 条规则：")
    for bucket in ("field_missing", "no_body", "batch", "other"):
        got = rep["reasons"].get(bucket) or {}
        if not got:
            continue
        lines.append(f"    · {label[bucket]}")
        for rid, detail in got.items():
            n = len(rep["undecided"].get(rid) or [])
            lines.append(f"        {rid} {n} 篇：{str(detail)[:88]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="合规审核唯一入口（对齐头条号发文规则）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true", help="打印完整 JSON")
    ap.add_argument("--fail-on-block-only", action="store_true",
                    help="只有命中 block 级违规才返回 1；「未判定」不拦。"
                         "发布前用这个档（否则正文读不到就会像 11-06 noon 那样整批 0 篇），"
                         "每日层不要用——那时「未判定」必须暴露。")
    ap.add_argument("--drop-violations", action="store_true",
                    help="发布前逐篇剔除：命中 block/rewrite 的成稿移出发布集合"
                         "（md 移进 output/<date>/dropped/），只留真正合规的篇。"
                         "处置原则「丢该篇、不停整批」——一篇的问题不该让整批 0 篇。")
    ap.add_argument("--evidence", action="store_true",
                    help="批次后核对：逐篇 judged/total + 未判定原因分类。"
                         "回答「这次到底判了几条」，不回答「有没有违规」。"
                         "不写 compliance_latest.json（这是诊断视图，不是闸门）。")
    ap.add_argument("--date", help="配合 --evidence 用，默认今天")
    args = ap.parse_args()

    if args.evidence:
        erep = evidence_report(args.date, args.days)
        print(render_evidence(erep))
        # 「没有产物」和「有产物但一篇都没判过」都是未判定（rc=2），与项目
        # 「未判定 ≠ 通过」契约一致。有判定记录 → 0：这是诊断视图，给它设通过线
        # 等于又发明一套阈值。
        if not erep["has_meta"] or erep.get("error"):
            return 2
        return 0 if erep["per_article"] else 2

    if args.selftest:
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n负向自检：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
        return 1 if bad else 0

    _all_dropped = False
    if args.drop_violations:
        # 先逐篇剔除，再对**剩下的**判一次。这样「有违规」不再等于「整批停发」：
        # 命中 block/rewrite 的篇被移出发布集合，其余照发。
        d = drop_violations(date.today().isoformat())
        if d.get("dropped"):
            print(f"⛔ 逐篇剔除 {len(d['dropped'])} 篇（不停整批）：")
            for x in d["dropped"]:
                print(f"   - {x['title'][:28]} ← {'、'.join(x['blocked_by'])}"
                      f"（{x['risk_score']} 分）")
        elif d.get("note"):
            print(f"ⓘ 逐篇剔除跳过：{d['note']}")
        # 「全丢」必须与「本来就没稿」分开报：两者下游都是 0 篇，但一个是
        # 规则问题要人来查，一个是上游空转要重跑。混在一起就没人查得清。
        _all_dropped = bool(d.get("total")) and d.get("kept") == 0
        if _all_dropped:
            print(f"❌ 本批 {d['total']} 篇全部被合规剔除，发布集合 0 篇"
                  f"（这是「全丢」，不是「正常空批」）")

    report = verify(args.days)
    try:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(render(report))

    if report["blocking"]:
        return 1
    if _all_dropped:
        # 3 = 全批被剔除。刻意不复用 1（那是「批量级违规，该停」）也不复用 2
        # （那是「未判定」）：三种终态对应三种处置，合并就等于又回到
        # 「以前三种终态共用一个 exit 1」导致批次永久丢失的老路上。
        return 3
    if args.fail_on_block_only:
        # 发布前档：只有真的违规才停发。「未判定」不拦——但也不当通过，
        # 报告里已经写明「不能视为合规通过」，每日层裸 rc=2 会把它抓出来。
        return 0
    # 未判定 ≠ 通过：block 级规则过半都没真正判过，不能报绿。
    # 本机常见（正文在 runner 的 _temp 上，跨批次读不到），CI 里应显著更低。
    block_rules = [r for r in report["rules"] if r["tier"] in ("block", "rewrite")]
    judged = [r for r in block_rules if r["status"] in (PASS, FAIL)]
    if report["articles"] == 0 or len(judged) * 2 < len(block_rules):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
