"""失败与降级设计（计划第九章）。

选题层插在发布之前，因此绝不能阻塞发布。每个环节都必须定义降级路径，
宁可当天少发，不可当天全空。

本模块把计划第九章的六条降级路径做成机器可读的策略表，并提供一次运行内的
降级事件记录器（degrade.note / drain），最终落到批次 metadata 的 degrade_events，
便于复盘「今天为什么少发了」。
"""

from __future__ import annotations

# 计划第九章六条降级路径（机器可读版）
DEGRADE_POLICY = {
    "赛程源": {
        "策略": "退化为纯新闻流驱动，选题池仅由事实卡装配",
        "阻塞发布": False,
    },
    "事实层": {
        "策略": "跳过该条事实，用剩余事实装配，不足则减少当日篇数",
        "阻塞发布": False,
    },
    "标题评分": {
        "策略": "评分服务超时：按上一版配比直接生成，不阻塞发布",
        "阻塞发布": False,
    },
    "评分连续不过": {
        "策略": "丢弃该选题并由候选池补位",
        "阻塞发布": False,
    },
    "一致性校验": {
        "策略": "重新生成一次，仍不过则降级为低风险板块",
        "阻塞发布": False,
    },
    "调度层": {
        "策略": "沿用现有主调度加兜底的既有结构，本计划不改动",
        "阻塞发布": False,
    },
}

# 低风险板块（计划：一致性打回后降级目标）——无硬事实/时效依赖
LOW_RISK_SECTIONS = ("人物故事", "战术榜单")

_EVENTS: list[dict] = []


def note(stage: str, reason: str, action: str = "", *, count: int = 1) -> dict:
    """记录一次降级事件（不阻塞）。返回事件 dict。"""
    ev = {
        "环节": stage,
        "原因": reason,
        "处置": action or DEGRADE_POLICY.get(stage, {}).get("策略", ""),
        "数量": count,
    }
    _EVENTS.append(ev)
    print(f"   🧯 降级[{stage}]：{reason} → {ev['处置']}")
    return ev


def drain() -> list[dict]:
    """取出并清空本次运行的降级事件（供写入 metadata）。"""
    global _EVENTS
    out, _EVENTS = _EVENTS, []
    return out


def peek() -> list[dict]:
    return list(_EVENTS)


def policy_for(stage: str) -> dict:
    return DEGRADE_POLICY.get(stage, {"策略": "未定义（按非阻塞处理）", "阻塞发布": False})


def pick_low_risk_section() -> str:
    """一致性/合规打回后的降级板块（取第一个低风险板块）。"""
    return LOW_RISK_SECTIONS[0]


def print_policy() -> None:
    for stage, p in DEGRADE_POLICY.items():
        print(f"- {stage}｜{p['策略']}（阻塞发布：{'是' if p['阻塞发布'] else '否'}）")


if __name__ == "__main__":
    print("计划第九章 · 降级路径：")
    print_policy()
