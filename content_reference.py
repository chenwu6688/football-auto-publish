#!/usr/bin/env python3
"""内容参照层（计划 13.3「抄方法论，不抄选题」）。

计划 13.3 的核心判断：垂直社区（懂球帝 / 直播吧）与推荐流（头条）的用户认知水位不同，
照搬其选题必然因实体陌生而划走——「78 篇欧冠只拿 852 阅读」即由此而来。因此：

  应当参照（方法论）      信源标注规范 / 标题信息结构 / 话题时效判断 / 栏目化与内容分层
  不得参照（具体选题）    中下游球队转会细节 / 欧战赛前前瞻 / 二线联赛赛事稿 / 三千字深度长文

本模块是 13.3 的**单一事实来源**：
  - ``reference_guidance()``：把「参照项 + 五家可取 + 风险提示」拼成生成提示片段，注入生成环节；
  - ``exclusion_guidance()``：把排除项拼成禁令片段，注入生成提示；
  - ``check_topic()`` / ``is_excluded()``：选题层（ranker L1）据此剔除命中排除项的候选。

三条纪律（绝不越界）：
  1. 参照对象只作**方法论**使用，绝不把参照内容作为素材写入正文（计划 3.2 / 11.3）；
  2. 机器匹配一律**保守**，宁可漏拦不误杀：未命中即放行，交由下游认知度门槛 / 一致性校验继续判；
  3. 命中排除项**只**在「标题 / 话题标签 / 板块」层面判定，绝不对正文全文匹配（避免正文提及即误杀）。

命令行：
  python3 content_reference.py --selftest            # 跑规则自检
  python3 content_reference.py --guidance            # 打印生成提示片段
  python3 content_reference.py --check "xxx前瞻：皇马vs曼城"  # 判定单条选题
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "content_reference.yaml"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import yaml
except Exception:  # pragma: no cover - yaml 缺失时降级为空配置
    yaml = None

_cfg_cache: dict | None = None


# ------------------------------------------------------------
# 配置加载
# ------------------------------------------------------------
def _empty_cfg() -> dict:
    return {"参照项": [], "排除项": [], "五家媒体": [], "风险提示": []}


def load_config(path: Path | str | None = None) -> dict:
    """读 config/content_reference.yaml；缺文件 / 坏文件 → 空配置（不猜测）。"""
    if yaml is None:
        return _empty_cfg()
    p = Path(path) if path else CONFIG_PATH
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return _empty_cfg()
    cfg = _empty_cfg()
    for k in cfg:
        if isinstance(data.get(k), list):
            cfg[k] = data[k]
    return cfg


def config(path: Path | str | None = None) -> dict:
    """带缓存的配置。"""
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = load_config(path)
    return _cfg_cache


def reset_cache() -> None:
    global _cfg_cache
    _cfg_cache = None


def reference_items(path: Path | str | None = None) -> list:
    """参照项列表（方法论）。"""
    return list(config(path).get("参照项") or [])


def exclusion_items(path: Path | str | None = None) -> list:
    """排除项列表（不得参照的具体选题）。"""
    return list(config(path).get("排除项") or [])


def media_items(path: Path | str | None = None) -> list:
    """五家媒体侧重。"""
    return list(config(path).get("五家媒体") or [])


# ------------------------------------------------------------
# 提示片段
# ------------------------------------------------------------
def reference_guidance(path: Path | str | None = None) -> str:
    """生成环节应参照的方法论片段（计划 13.3「应当参照的部分」）。"""
    cfg = config(path)
    lines = ["【可参照的方法论（只借方法，不搬选题）】"]
    for it in cfg.get("参照项") or []:
        lines.append(f"- {it.get('key')}：{it.get('desc')}")
    media = cfg.get("五家媒体") or []
    if media:
        lines.append("【头部媒体各自侧重（仅借方法）】")
        for m in media:
            lines.append(f"- {m.get('name')}（{m.get('侧重')}）：可取 {m.get('可取')}")
    risk = cfg.get("风险提示") or []
    if risk:
        lines.append("【风险提示】")
        for r in risk:
            lines.append(f"- {r}")
    return "\n".join(lines)


def exclusion_guidance(path: Path | str | None = None) -> str:
    """生成环节的排除禁令片段（计划 13.3「不得参照的部分」）。"""
    lines = ["【严禁照搬的选题（13.3 排除项，一律不做）】"]
    for it in exclusion_items(path):
        lines.append(f"- {it.get('key')}：{it.get('reason')}")
    return "\n".join(lines)


def guidance(path: Path | str | None = None) -> str:
    """参照 + 排除合并片段，供生成提示整体注入。"""
    return reference_guidance(path) + "\n" + exclusion_guidance(path)


# ------------------------------------------------------------
# 选题校验（保守匹配：只在标题 / 话题标签 / 板块层面）
# ------------------------------------------------------------
def _topic_text(topic) -> str:
    """取用于匹配的文本：**标题 / 话题标签**。

    只取选题的**上层标识**（标题、话题标签、label），绝不取正文、也不取 content_type/板块——
    否则「转会资讯」这类栏目名会与排除项词表误撞（如「升班马」×「转会」栏目）造成误杀（纪律 3）。
    兼容 {title/label/标题/话题} 与 {card:{...}} 两种形态。
    """
    if isinstance(topic, str):
        return topic
    if not isinstance(topic, dict):
        return ""
    card = topic.get("card") if isinstance(topic.get("card"), dict) else {}
    parts = [
        topic.get("title"), topic.get("label"), topic.get("标题"),
        topic.get("话题"), topic.get("topic"),
        card.get("标题"), card.get("title"),
    ]
    return " ".join(str(p) for p in parts if p)


def _declared_len(topic) -> int | None:
    """取宣称的目标字数（若有），用于「三千字深度长文」判定。"""
    if not isinstance(topic, dict):
        return None
    for k in ("target_chars", "target_length", "字数", "目标字数", "length"):
        v = topic.get(k)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


def check_topic(topic, path: Path | str | None = None) -> list:
    """判定一条选题是否命中排除项，返回违规列表（空列表 = 未命中，放行）。

    每条违规：``{"key", "reason", "matched"}``。
    """
    text = _topic_text(topic)
    violations = []
    for it in exclusion_items(path):
        key, reason = it.get("key"), it.get("reason")
        match_any = it.get("match_any") or []
        match_all = it.get("match_all") or []
        max_chars = it.get("max_chars")

        hit_any = [k for k in match_any if k and k in text]
        hit_all = []
        if match_all:
            groups = [[k for k in grp if k and k in text] for grp in match_all]
            if groups and all(groups):
                hit_all = [x for grp in groups for x in grp]

        matched = hit_any + hit_all
        # 三千字深度长文：宣称字数达阈值也算命中
        if max_chars:
            dl = _declared_len(topic)
            if dl is not None and dl >= max_chars:
                matched = matched + [f"字数{dl}≥{max_chars}"]
        if matched:
            violations.append({"key": key, "reason": reason, "matched": matched})
    return violations


def is_excluded(topic, path: Path | str | None = None) -> bool:
    return bool(check_topic(topic, path))


def filter_topics(topics, path: Path | str | None = None):
    """批量过滤，返回 ``(kept, dropped)``；dropped 里带 ``_ref_violations``。"""
    kept, dropped = [], []
    for t in (topics or []):
        v = check_topic(t, path)
        if v:
            if isinstance(t, dict):
                t["_ref_violations"] = v
            dropped.append((t, "；".join(f"{x['key']}({','.join(x['matched'])})" for x in v)))
            continue
        kept.append(t)
    return kept, dropped


# ------------------------------------------------------------
# 表驱动规则自检（计划 13.3 排除项的可执行副本）
# ------------------------------------------------------------
#: (题干, 期望是否命中排除项)
RULE_CASES = (
    # 欧战赛前前瞻 —— 命中
    ("皇马vs曼城前瞻：安帅的最后一搏？", True),
    ("赛前分析：拜仁能否终结连败", True),
    ("欧冠赛事前瞻：巴黎客战", True),
    # 中下游球队转会细节 —— 命中（须同时有中下游标记 + 转会词）
    ("保级队锋线引援：300万欧签下旧将", True),
    ("升班马转会细节曝光：中场全换血", True),
    # 仅出现「升班马/保级队」但无转会词 —— 不命中（强队对决，绝不误杀）
    ("升班马教曼联做人！0-2开门黑老六直呼离谱", False),
    ("英超烧钱40亿欧！升班马比西甲三强还能花？", False),
    ("莱斯特城0-4惨败牛津联，英超冠军沦落英甲", False),
    # 二线联赛赛事稿（联赛名 + 赛事词）—— 命中
    ("英冠战报：利兹联绝杀登顶", True),
    ("西乙赛果：武磊旧主战平", True),
    # 三千字深度长文 —— 命中
    ("深度长文：解析曼城十年王朝", True),
    ("曼城十年史", {"target_chars": 3200}, True),
    # 正常选题 —— 不命中
    ("罗马诺：皇马4000万欧报价遭拒，中场补强为何卡壳？", False),
    ("姆巴佩伤停三周，巴黎锋线谁来扛？", False),
    ("英冠联赛的商业版图（转会视角）", False),   # 无赛事词，不误杀
    ("拜仁回顾：三冠王的那个赛季", False),                # 回顾非前瞻
    ("曼城十年史", {"target_chars": 1200}, False),
)


def selftest() -> list:
    """跑表驱动用例，返回不通过项（空列表 = 全部符合 13.3）。"""
    bad = []
    for case in RULE_CASES:
        if len(case) == 2:
            topic, want = case
        else:
            label, extra, want = case
            topic = {"title": label, **(extra or {})}
        got = is_excluded(topic)
        if got != want:
            bad.append({"topic": topic, "want": want, "got": got})
    return bad


def render_selftest(bad=None) -> str:
    bad = selftest() if bad is None else bad
    L = ["# 内容参照层规则自检（计划 13.3）", "",
         f"- 用例数：{len(RULE_CASES)}　不通过：{len(bad)}",
         f"- 结论：**{'通过 ✅' if not bad else '未通过 ❌'}**", ""]
    if bad:
        for b in bad:
            L.append(f"- ❌ {b['topic']}：期望命中={b['want']}，实得={b['got']}")
    else:
        L.append("排除项（前瞻 / 中下游 / 二线联赛赛事稿 / 三千字长文）命中判定全部符合；"
                 "正常选题零误杀。")
    return "\n".join(L)


def _cli() -> int:
    ap = argparse.ArgumentParser(description="内容参照层（计划 13.3）")
    ap.add_argument("--selftest", action="store_true", help="跑规则自检")
    ap.add_argument("--guidance", action="store_true", help="打印生成提示片段")
    ap.add_argument("--check", metavar="TOPIC", help="判定单条选题")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    if args.selftest:
        bad = selftest()
        print(render_selftest(bad))
        return 0 if not bad else 1

    if args.guidance:
        print(guidance())
        return 0

    if args.check:
        v = check_topic(args.check)
        if args.json:
            print(json.dumps(v, ensure_ascii=False, indent=2))
        else:
            print(f"命中排除项：{len(v)}")
            for x in v:
                print(f"- {x['key']}：{x['reason']}（{','.join(x['matched'])}）")
        return 1 if v else 0

    print(f"参照项 {len(reference_items())}｜排除项 {len(exclusion_items())}｜"
          f"媒体 {len(media_items())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
