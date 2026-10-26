#!/usr/bin/env python3
"""一级·确定性供给 · 提前量产入库作业（计划 12.1）。

计划 12.1：一级·确定性占供给 30%，来源含「转会窗截止日 / 赛季节点 / 开赛倒计时」，
特征「可提前 7 天预生成，无时效风险」，出稿要求「提前量产入库」。

本作业把窗口内的节点选题（config/season_calendar.yaml）提前量产成稿并入库存
data/deterministic/items.json；发布日由 orchestrator 直接取用，不再调用 LLM。

纪律（对齐计划 4/11.3）：
  - 节点日期是公开日历事实，不编造；正文只做「节点 + 情绪/看点」表达，
    不虚构具体比分、转会金额、伤停等可核查硬事实。
  - 未登记实体不进生成环节（节点选题以赛事/窗口为对象，不涉及未映射球队）。

用法:
  python scripts/pregen_deterministic.py            # 补窗口内节点（默认 HARD_MAX_GEN=4）
  python scripts/pregen_deterministic.py --dry-run  # 只看缺口
  python scripts/pregen_deterministic.py --days 7 --max-gen 3
"""

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deterministic_supply as ds  # noqa: E402
from constants import LLM_JSON_CANDIDATES  # noqa: E402
from utils import call_llm_json  # noqa: E402

CST = timezone(timedelta(hours=8))
HARD_MAX_GEN = 4

_TMPL = """你是头条号足球博主"球评人老六"，受众是 41 岁以上男性老球迷。
现在要写一篇【{node_type}】类的确定性内容（提前备好、到点发；正文不要出现「还剩 X 天」
这类会过期的表述，也不要写成过期旧闻）。
节点事件：{label}（节点日期 {node_date}）
看点提示：{conflict}
{facts_block}
硬性要求：
- 标题 26-30 字，套用公式「**具体的人或球队** + **悬念词**（没想到／原来／竟然／居然／反转）
  + 疑问收尾」——R11 回测：悬念词点击率 +87%；「写人」比「写事」高 3-10 倍点击
- **落点在人的处境**：写主帅/老将/核心球员面对这个节点在想什么、要赌什么，
  不要写成「窗口还剩几天」的纯信息播报或纯对阵罗列
- 正文 600-900 字，含 ≥2 个 ## 小标题，口语化、像老球迷喝酒聊球
- 必须围绕该节点本身展开（时间节点、窗口/赛程的意义、球迷该盯什么）
- 严禁编造可核查的具体比分、转会金额、伤停名单等硬事实；只用公认常识与赛制规则
- **若无【已核实事实】区块**：一律不写具体数字/比分/名单，只用赛制规则与公认常识表达
- **若有【已核实事实】区块**：数字、比分、名单必须与区块**完全一致**，不得改写、不得四舍五入、
  不得「顺手加一句」区块里没有的荣誉或数据；区块里没提到的人，不要给他安任何战绩
- 严禁在正文写死「还剩 X 天」这类具体天数（发布日与撰写日可能不同），
  改用「即将到来」「进入最后阶段」「离得不远了」等表述
- 禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说

只输出纯 JSON：
{{"title": "标题(26-30字,人+悬念词开头,疑问收尾)", "content": "Markdown正文(600-900字,含≥2个##小标题)",
 "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"],
 "golden_lines": ["金句1", "金句2"], "interaction_type": "共鸣式",
 "interaction_bait": "互动问题", "content_type": "{content_type}",
 "ai_perspective": "一句独立判断(≤40字,犀利有态度)"}}"""


def _facts_block(topic: dict) -> str:
    """把日历里人工核实的事实渲染成提示词区块；没有就返回空串（行为与旧版一致）。

    为什么需要这个：预生成长期只敢写「窗口还剩几天」这类赛制常识，因为一旦碰具体
    数字就有编造风险。结果是节点稿空泛得像信息播报。把**人工核实过**的事实作为
    唯一可用事实源喂进去，才能既具体又不编造——边界写死「区块之外一律不许出现」。
    """
    facts = [str(f).strip() for f in (topic.get("facts") or []) if str(f).strip()]
    if not facts:
        return ""
    lines = "\n".join(f"  {i}. {f}" for i, f in enumerate(facts, 1))
    src = (topic.get("fact_source") or {}).get("url") or ""
    return (
        "\n【已核实事实（只能用下面这些；区块之外的具体数字/比分/名单一律不许出现）】\n"
        f"{lines}\n"
        + (f"（来源：{src}，已人工交叉核对）\n" if src else "")
    )


def _make_one(topic: dict) -> dict:
    label = topic.get("conflict", "") or topic.get("node_type", "")
    msgs = [
        {"role": "system", "content": "你是足球自媒体作者，只输出 JSON，不输出任何额外文字。"},
        {"role": "user", "content": _TMPL.format(
            node_type=topic.get("node_type", "赛季节点"),
            label=label, node_date=topic.get("node_date", ""),
            days_to=topic.get("days_to", 0), conflict=topic.get("conflict", ""),
            facts_block=_facts_block(topic),
            content_type=topic.get("content_type") or topic.get("node_type", "赛季节点"))},
    ]
    obj, _model = call_llm_json(msgs, LLM_JSON_CANDIDATES, temperature=0.9, max_tokens=3000)
    obj["content_type"] = topic.get("content_type") or topic.get("node_type", "赛季节点") or "战术榜单"
    obj["_column_name"] = topic.get("板块", "")
    try:
        from orchestrator import append_ai_annotation, score_title_plan
        append_ai_annotation(obj, obj.get("ai_perspective", ""))
        s, passed, _reasons = score_title_plan({"content_type": obj["content_type"]},
                                               obj.get("title", ""), obj["content_type"])
        obj["_title_score"] = s
        obj["_title_passed"] = passed
    except Exception:
        pass
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description="一级·确定性节点成稿量产入库（计划 12.1）")
    ap.add_argument("--days", type=int, default=ds.DEFAULT_HORIZON_DAYS, help="前向天数")
    ap.add_argument("--max-gen", type=int, default=HARD_MAX_GEN, help="单次最多生成数量")
    ap.add_argument("--dry-run", action="store_true", help="只看缺口，不生成")
    args = ap.parse_args()

    expired = ds.expire()
    pending = ds.pending_nodes(horizon_days=args.days)
    print(f"[一级·确定性] 回收过期 {expired} 条；窗口 {args.days} 天内待入库节点 {len(pending)} 个")
    for t in pending:
        print(f"   · [{t['days_to']:>2}天] {t['node_date']} {t['node_type']} {t['conflict'][:40]}")
    if not pending:
        print("[一级·确定性] 窗口内无待入库节点，无需量产")
        return 0
    if args.dry_run:
        return 0

    to_gen = pending[:max(0, args.max_gen)]
    stocked = 0
    for topic in to_gen:
        try:
            art = _make_one(topic)
        except Exception as e:
            print(f"   ❌ 量产失败（{topic['node_type']}）: {type(e).__name__}: {str(e)[:120]}")
            break
        if not art or not art.get("title") or len(art.get("content", "")) < 200:
            print(f"   ⚠️ 量产内容不合格，跳过（{topic['node_type']}）")
            continue
        rid = ds.stock(art, topic)
        stocked += 1
        print(f"   ✅ 已入库 [{rid}] {topic['node_type']} · {art['title'][:36]}"
              f"（标题分 {art.get('_title_score', '-')}，截止 {topic['publish_by']}）")
    st = ds.stats()
    print(f"[一级·确定性] 本次入库 {stocked} 篇；当前可用 {st['available']} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
