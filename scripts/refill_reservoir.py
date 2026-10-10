#!/usr/bin/env python3
"""三级·软性内容蓄水池 · 备货作业（计划 12.1）。

三级内容（人物故事 / 榜单 / 数据对比 / 历史回顾）无时效压力，可提前量产入库。
本作业把蓄水池补到水位线（TARGET）；只生成缺口数量，控制 LLM 成本。
调度：随批次发布前运行（非阻断），或由独立 workflow 定期运行。

用法:
  python scripts/refill_reservoir.py            # 补到默认水位（8）
  python scripts/refill_reservoir.py --target 12 --max-gen 4
  python scripts/refill_reservoir.py --dry-run  # 只看缺口，不生成
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reservoir  # noqa: E402
from constants import LLM_JSON_CANDIDATES  # noqa: E402
from utils import call_llm_json  # noqa: E402

DEFAULT_TARGET = 8
HARD_MAX_GEN = 6

# 软性选题轮换池（计划 12.1：人物故事 / 榜单 / 数据对比 / 历史回顾）
SOFT_ANGLES = [
    {"content_type": "人物故事", "section": "人物故事",
     "brief": "退役或暮年球星的现状与情怀，落点在『爷青回』式的记忆与感慨"},
    {"content_type": "榜单", "section": "战术榜单",
     "brief": "一份历史射手榜/助攻榜/身价榜的榜单，含名次与数据对比"},
    {"content_type": "数据对比", "section": "战术榜单",
     "brief": "两位球星或两支球队的数据对比，用数字说明谁更强、强在哪"},
    {"content_type": "历史回顾", "section": "人物故事",
     "brief": "一场经典战役或世界杯名局的历史回顾，带时代背景"},
]

_TMPL = """你是头条号足球博主"球评人老六"，受众是 41 岁以上男性老球迷。
请写一篇【{content_type}】类的软性内容（无时效压力）：{brief}。

硬性要求：
- 标题 26-30 字，套用公式「具体人名或球队 + 冲突或反差 + 疑问收尾」
- 正文 600-900 字，含 ≥2 个 ## 小标题，口语化、像老球迷喝酒聊球
- 用情怀、通俗战术、国足记忆或世界杯经典时刻引发共鸣
- 不要编造可核查的具体比分/转会金额等硬事实；用公认的历史数据与常识
- 禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说

只输出纯 JSON：
{{"title": "标题(26-30字,疑问收尾)", "content": "Markdown正文(600-900字,含≥2个##小标题)",
 "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"],
 "golden_lines": ["金句1", "金句2"], "interaction_type": "共鸣式",
 "interaction_bait": "互动问题", "content_type": "{content_type}",
 "ai_perspective": "一句独立判断(≤40字,犀利有态度)"}}"""


def _make_one(angle: dict, index: int) -> dict:
    msgs = [
        {"role": "system", "content": "你是足球自媒体作者，只输出 JSON，不输出任何额外文字。"},
        {"role": "user", "content": _TMPL.format(content_type=angle["content_type"], brief=angle["brief"])},
    ]
    obj, _model = call_llm_json(msgs, LLM_JSON_CANDIDATES, temperature=0.9, max_tokens=3000)
    obj["content_type"] = angle["content_type"]
    obj["_column_name"] = angle["section"]
    try:
        from orchestrator import append_ai_annotation, score_title_plan
        append_ai_annotation(obj, obj.get("ai_perspective", ""))
        s, passed, reasons = score_title_plan({"content_type": angle["content_type"]},
                                              obj.get("title", ""), angle["content_type"])
        obj["_title_score"] = s
        obj["_title_passed"] = passed
    except Exception:
        pass
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description="软性内容蓄水池备货（计划 12.1）")
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET, help="目标水位（可用条数）")
    ap.add_argument("--max-gen", type=int, default=HARD_MAX_GEN, help="单次最多生成数量")
    ap.add_argument("--dry-run", action="store_true", help="只看缺口，不生成")
    args = ap.parse_args()

    expired = reservoir.expire()
    avail = reservoir.available()
    gap = max(0, args.target - len(avail))
    print(f"[蓄水池] 现有可用 {len(avail)} 条，回收过期 {expired} 条，缺口 {gap} 条（水位 {args.target}）")
    if gap == 0:
        print("[蓄水池] 已满足水位，无需备货")
        return 0
    if args.dry_run:
        return 0

    to_gen = min(gap, max(0, args.max_gen))
    print(f"[蓄水池] 本次备货 {to_gen} 篇（上限 {args.max_gen}）")
    stocked = 0
    for i in range(to_gen):
        angle = SOFT_ANGLES[(datetime.now().timetuple().tm_yday + i) % len(SOFT_ANGLES)]
        try:
            art = _make_one(angle, i + 1)
        except Exception as e:
            print(f"   ❌ 备货失败（{angle['content_type']}）: {type(e).__name__}: {str(e)[:120]}")
            break
        if not art or not art.get("title") or len(art.get("content", "")) < 200:
            print(f"   ⚠️ 备货内容不合格，跳过（{angle['content_type']}）")
            continue
        rid = reservoir.stock(art, section=angle["section"], date_str=datetime.now().strftime("%Y-%m-%d"))
        stocked += 1
        print(f"   ✅ 已入库 [{rid}] {art['content_type']} · {art['title'][:36]}"
              f"（标题分 {art.get('_title_score','-')}）")
    print(f"[蓄水池] 本次入库 {stocked} 篇；当前可用 {reservoir.stats()['available']} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
