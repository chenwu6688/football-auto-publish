"""发布前唯一的人工确认闸门：争议指控。

只回答一个问题——**这篇稿子有没有断言某真人/俱乐部涉违法或丑闻**。

为什么只留这一条（对齐头条号扣分条款）
    平台对「虚假/失实」的处置是 70 / 20 / 10 三档，是全部 27 个扣分项里最重的：
    谣言 70 分（接近封号），不实 20 分，失实表述 10 分。关键词闸门守不住
    「推断错」「数据错」，唯一守得住的就是「凭空给真人扣罪名」这一档。
    其余判断（伤病、合同金额、战术对错）平台根本不扣分，属于内容质量范畴，
    由出口的合规审核（scripts/verify_compliance.py）按事实层判，这里不再重复。

刻意不做的事（2026-11-11 精简，只为好维护）
    1. 不再判「伤病」「合同金额」：伤停名单、续约年限、纪律处罚是足球稿的常规背景，
       一篇正常球评几乎不可能不提。拿「出现关键词」当「造谣风险」，命中率接近
       100%，结果就是当天 0 篇——11-10 晚 / 11-11 晨连续两批 4/4 篇全被拦，
       而那两篇的真实拦截原因是「伤病(伤停)」「合同金额(合同)」。
    2. 不再分 high_risk / blocking / severity 三层：只有「拦」与「不拦」两种结果。
    3. 不再维护 data/pending_review.json 队列：该文件在 .gitignore 里，CI runner
       跑完即销毁，队列在流水线上从未真正存在过。人工确认的唯一产物是
       output/<date>/review/*.md（由 orchestrator 落盘，随 artifact 下载）。
"""

from __future__ import annotations

# 判定词表：只有「断言违法违规」的词，不含「禁赛 / 处罚 / 罚款 / 调查」这类
# 日常报道词（累积黄牌停赛、俱乐部被罚、足协调查青训——都是正常新闻，拦了就误伤）。
ACCUSATION_WORDS = (
    "指控", "起诉", "诉讼", "立案", "被捕", "拘留",
    "假球", "赌球", "操纵比赛", "禁药", "兴奋剂",
    "性侵", "种族歧视", "受贿", "行贿", "逃税", "丑闻",
)


def blocked_reason(article: dict) -> str:
    """命中即返回拦截原因（含命中词），未命中返回空串。

    唯一入口——不再有 assess() / is_blocked() / severity 之类并行的判定函数，
    一处判定，一处可改。
    """
    if not isinstance(article, dict):
        return ""
    blob = f"{article.get('title', '') or ''}\n{article.get('content', '') or ''}"
    hits = [w for w in ACCUSATION_WORDS if w in blob]
    return f"争议指控(命中：{'、'.join(hits)})" if hits else ""
