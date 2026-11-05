#!/usr/bin/env python3
"""三级·软性内容蓄水池 · 备货作业（计划 12.1）。

三级内容（人物故事 / 榜单 / 数据对比）无时效压力，可提前量产入库。
本作业把蓄水池补到水位线（TARGET）；只生成缺口数量，控制 LLM 成本。
调度：随批次发布前运行（非阻断），或由独立 workflow 定期运行。

「历史回顾」2026-11-01 起禁发（实测 9 篇篇均 4.7 阅读、最高 15），已从轮换池移除。
判据唯一权威在 reservoir.BANNED_TYPES，本文件只是消费它，不要再自己写一份。

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

# 当日已发标题缓存（疑问收尾限额要看 14 天真实历史，见 _recent_titles）
_RECENT_CACHE = None

# 软性选题轮换池（计划 12.1：人物故事 / 榜单 / 数据对比）
# 注意：这里只能放 reservoir.SOFT_TYPES 里的类型。「历史回顾」已进 BANNED_TYPES，
# 放进来会「生成后又被取用侧过滤」，白烧额度。判据唯一权威在 reservoir.py。
SOFT_ANGLES = [
    {"content_type": "人物故事", "section": "人物故事",
     "brief": "退役或暮年球星的现状与情怀，落点在『爷青回』式的记忆与感慨"},
    {"content_type": "榜单", "section": "战术榜单",
     "brief": "一份历史射手榜/助攻榜/身价榜的榜单，含名次与数据对比"},
    {"content_type": "数据对比", "section": "战术榜单",
     "brief": "两位球星或两支球队的数据对比，用数字说明谁更强、强在哪"},
]

# 启动即校验：轮换池一旦混入禁发类型，宁可当场报错，也不要「生成完再被过滤」白烧额度。
# 用 raise 而非 assert —— assert 在 python -O 下会被优化掉，等于没写。
_banned_in_pool = [a["content_type"] for a in SOFT_ANGLES if reservoir.is_banned(a["content_type"])]
if _banned_in_pool:
    raise SystemExit(
        f"❌ 软性轮换池含禁发类型 {_banned_in_pool}；"
        f"禁发清单见 reservoir.BANNED_TYPES={reservoir.BANNED_TYPES}。"
        f"要恢复请先拿新的绩效证据改那份清单，不要只改这里。")

_TMPL = """你是头条号足球博主"球评人老六"，受众是 41 岁以上男性老球迷。
请写一篇【{content_type}】类的软性内容（无时效压力）：{brief}。

硬性要求：
- 标题 18-30 字，套用公式「具体人名或球队 + 一个可核对数字/过程锚点 + 一处冲突或落差」
- **标题不要以疑问句收尾**（不要「…吗？」「…凭什么？」）：2026-11 实测头部账号疑问句只占 0~20%，
  空问标题中位阅读 8。把「过程」写进标题——第几分钟、连过几人、第几次夺冠
- 正文 600-900 字，含 ≥2 个 ## 小标题，口语化、像老球迷喝酒聊球
- 用情怀、通俗战术、国足记忆或世界杯经典时刻引发共鸣
- 不要编造可核查的具体比分/转会金额等硬事实；用公认的历史数据与常识
- 禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说

**粉丝沉淀钩子（生成后会校验，缺了直接打回）**：
- 正文里必须有一句自然的「关注」引导，融在收尾段落里，例如「关注老六，这类老故事我还存着一柜子」
- 必须有一个读者能直接回答的问题（评论区/你怎么看/你站哪边）
- **禁止**写「👇 评论区见分晓」「各位老铁」这类套话，也不要加分隔线堆在文末——要像人说话

只输出纯 JSON：
{{"title": "标题(18-30字,陈述句,含具体数字或过程锚点)", "content": "Markdown正文(600-900字,含≥2个##小标题)",
 "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"],
 "golden_lines": ["金句1", "金句2"], "interaction_type": "共鸣式",
 "interaction_bait": "互动问题", "content_type": "{content_type}",
 "ai_perspective": "一句独立判断(≤40字,犀利有态度)"}}"""


def _recent_titles() -> list[str]:
    """当日已发/已成稿的标题（疑问收尾限额要用真历史，否则限额永不触发）。"""
    global _RECENT_CACHE
    if _RECENT_CACHE is None:
        try:
            from orchestrator import recent_titles_for_gate
            _RECENT_CACHE = list(recent_titles_for_gate(datetime.now().strftime("%Y-%m-%d")))
        except Exception:
            _RECENT_CACHE = []
    return _RECENT_CACHE


def _title_gate(obj: dict, content_type: str) -> tuple[bool, str]:
    """入库前过标题闸门（含疑问收尾限额）。

    2026-11-05：此前这里调 `score_title_plan` **不传 recent_titles**，靠「疑问收尾限额」
    判的那条规则永远不会触发，分数看着及格、标题却全是「…凭什么？」的空问；再加上
    转正环节完全不过闸门，这些 10 月存进来的标题就直接发出去了（当天两篇把
    verify_publish_gate 刷红）。现在两处都堵：入库要过、转正要过。
    """
    try:
        from orchestrator import score_title_plan
        s, passed, reasons = score_title_plan({"content_type": content_type},
                                              obj.get("title", ""), content_type,
                                              recent_titles=_recent_titles())
    except Exception as e:
        obj["_title_passed"] = False
        return False, f"标题评分不可用（{type(e).__name__}），按不合格处理"
    obj["_title_score"] = s
    obj["_title_passed"] = passed
    if passed:
        return True, ""
    return False, (f"标题未达及格线（{s} 分）：{'；'.join(reasons)[:120]}。"
                   "请重拟：具体人名/球队 + 一个可核对数字或过程锚点 + 一处对比落差，"
                   "18-30 字，**不要以疑问句收尾**。")


def _make_one(angle: dict, index: int) -> dict:
    """生成一条软性备货，按互动引导与标题两道闸门自检（不过就重试一次）。

    2026-11-01 补：此前这里生成完直接入库，**完全不过闸门**。结果软性稿必然
    缺关注引导，等到保存侧才被兜底强插模板句——10 月的 249 次「缺关注引导」
    几乎全部来自这条路。治本要在生产端就要求，而不是在末端贴膏药。
    2026-11-05 再加标题闸门（同上：治本在生产端）。
    """
    import interaction_bait

    base = _TMPL.format(content_type=angle["content_type"], brief=angle["brief"])
    last_hint = ""
    obj: dict = {}
    for _attempt in range(2):
        user = base + (f"\n\n⚠️ 上一版被退回：{last_hint}\n这次务必修正。\n" if last_hint else "")
        msgs = [
            {"role": "system", "content": "你是足球自媒体作者，只输出 JSON，不输出任何额外文字。"},
            {"role": "user", "content": user},
        ]
        obj, _model = call_llm_json(msgs, LLM_JSON_CANDIDATES, temperature=0.9, max_tokens=3000)
        obj = obj or {}
        bait = interaction_bait.check(obj.get("content", ""),
                                      bait_field=obj.get("interaction_bait", ""))
        bait_ok = bool(bait.get("ok") or bait.get("degraded"))
        title_ok, title_hint = _title_gate(obj, angle["content_type"])
        if bait_ok and title_ok:
            break
        hints = []
        if not bait_ok:
            hints.append("缺 " + "、".join(bait.get("missing") or []) + "："
                         + str(bait.get("hint") or "")
                         + "。请把引导自然融进收尾段落，不要堆套话。")
        if not title_ok:
            hints.append(title_hint)
        last_hint = "；".join(hints)
        print(f"   🔁 [{angle['content_type']}] 备货未过闸门，重试一次：{last_hint[:60]}")

    obj["content_type"] = angle["content_type"]
    obj["_column_name"] = angle["section"]
    try:
        from orchestrator import append_ai_annotation
        append_ai_annotation(obj, obj.get("ai_perspective", ""))
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
        if not art.get("_title_passed"):
            # 入库即合规：标题过不了闸门的条目**不入库**。此前是「入库再说」，
            # 结果 10 月那批空问标题在 11-05 转正后直接发布，当天就刷红发布闸门。
            print(f"   ⛔ 标题不过闸门，不入库（{angle['content_type']}）："
                  f"{art['title'][:30]}（{art.get('_title_score', '-')} 分）")
            continue
        rid = reservoir.stock(art, section=angle["section"], date_str=datetime.now().strftime("%Y-%m-%d"))
        stocked += 1
        print(f"   ✅ 已入库 [{rid}] {art['content_type']} · {art['title'][:36]}"
              f"（标题分 {art.get('_title_score','-')}）")
    print(f"[蓄水池] 本次入库 {stocked} 篇；当前可用 {reservoir.stats()['available']} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
