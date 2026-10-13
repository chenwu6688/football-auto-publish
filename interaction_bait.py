#!/usr/bin/env python3
"""互动引导闸门 —— 「关注引导 + 互动引导」硬校验（R11 内容诊断 2026-10-13 建议5）。

背景（R11 头条真实数据，2026-10-13）：
  「粉丝阅读占比中位 0%（成熟号 20-40%），互动率仅健康号 1/3，账号只吃推荐流量」。
  这是唯一能打破「低推荐 → 低点击 → 更低推荐」死循环的抓手，但此前只写在提示词里
  （prompts/rewrite_article.txt「粉丝沉淀钩子·必须满足」），**没有任何生成后校验**。

本模块唯一职责：判定一篇成稿是否具备「关注引导」与「互动引导」，供生成侧打回重写、
供保存侧兜底注入后复核。

纪律（与全仓一致）：
  - 判据与话术模板来自 config/interaction_bait.yaml；
  - 匹配**必须带引导语境**，避免「值得关注」这类事实性用词被误判成钩子；
  - 配置不可用 → **放行并置 degraded**，绝不因读不到配置而阻塞发布（计划九）；
  - 「缺互动引导」只打回重写/兜底注入，**绝不因此丢弃整篇成稿**（正文事实才是主体）；
  - 本模块只判定与兜底，不改事实、不介入选题。

命令行：
  python3 interaction_bait.py --selftest
  python3 interaction_bait.py --file output/2026-10-13/article-1-x.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "interaction_bait.yaml"

# 兜底仅用于「配置存在但字段缺失」；配置整体不可用则放行（见 check）。
_FALLBACK = {
    "版本": "fallback",
    "关注引导": [r"(点|加|戳|求|记得|别忘|顺手|不妨)[^。！？!?\n]{0,6}(关注|追更|订阅|收藏)",
             r"(关注|追更|订阅)[^。！？!?\n]{0,10}(不迷路|后续|老六|我)", r"不迷路"],
    "互动引导": [r"评论区", r"留言", r"(说说|聊聊|告诉|发表|亮出)[^。！？!?\n]{0,4}(你的|你|看法|想法|选择)",
             r"(你|您|大家|各位)(觉得|怎么看|认为|选|站)", r"(抠|扣)1", r"(站队|来辩|不服来|投票)"],
    "判罚": {"分数": 0, "动作": "打回重写",
             "提示": "成稿必须同时具备「关注引导」与「互动引导」。"},
    "兜底模板": {
        "关注": ["关注老六，这波后续我替你盯着。"],
        "互动": {"": "你怎么看？评论区聊聊。"},
    },
}

_CACHE: dict = {}


def load_config(path: Path | str | None = None) -> dict | None:
    """读取配置；不可用返回 None（调用方据此放行，不阻塞）。"""
    p = Path(path or CONFIG_PATH)
    key = str(p)
    if key in _CACHE:
        return _CACHE[key]
    cfg = None
    try:
        import yaml
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                cfg = data
    except Exception:
        cfg = None
    _CACHE[key] = cfg
    return cfg


def _compile(pats) -> list[re.Pattern]:
    out = []
    for pat in (pats or []):
        try:
            out.append(re.compile(str(pat)))
        except re.error:
            continue
    return out


def _hits(content: str, pats: list[re.Pattern]) -> list[str]:
    return [p.pattern for p in pats if p.search(content)]


def check(content: str, *, bait_field: str = "", cfg: dict | None = None) -> dict:
    """判定成稿是否同时具备「关注引导」与「互动引导」。

    content    —— 最终正文（含保存侧注入的互动钩子）
    bait_field —— article["interaction_bait"]，作为「互动引导」的补充证据
                  （即使正文尚未注入，字段非空也算已具备互动设计）

    返回 dict：
      ok        True = 两类都具备（放行）；False = 缺任一类（打回/兜底）
      degraded  True = 配置不可用，已放行（不阻塞）
      missing   ["关注引导"] / ["互动引导"] / 两者
      hits      {"follow": [...], "comment": [...]} 命中到的模式
      hint      打回重写时回灌给 LLM 的指引
    """
    content = (content or "").strip()
    bait_field = (bait_field or "").strip()

    raw = cfg if cfg is not None else load_config()
    degraded = raw is None
    cfg = raw or _FALLBACK

    follow_pats = _compile(cfg.get("关注引导")) or _compile(_FALLBACK["关注引导"])
    comment_pats = _compile(cfg.get("互动引导")) or _compile(_FALLBACK["互动引导"])
    penalty = cfg.get("判罚") or {}

    if degraded:
        return {"ok": True, "degraded": True, "missing": [], "hits": {"follow": [], "comment": []},
                "hint": "", "penalty_score": 0,
                "detail": ["配置不可用（config/interaction_bait.yaml 缺失或不可解析），本闸门放行"]}

    if not content:
        return {"ok": False, "degraded": False, "missing": ["正文"],
                "hits": {"follow": [], "comment": []},
                "hint": str(penalty.get("提示") or _FALLBACK["判罚"]["提示"]),
                "penalty_score": int(penalty.get("分数") or 0),
                "detail": ["正文为空，无法判定互动引导"]}

    follow = _hits(content, follow_pats)
    comment = _hits(content, comment_pats)
    # interaction_bait 字段是「互动设计已完成」的直接证据（保存侧会把字段注入正文）
    if not comment and bait_field and len(bait_field) >= 6:
        comment = ["field:interaction_bait"]

    missing = []
    if not follow:
        missing.append("关注引导")
    if not comment:
        missing.append("互动引导")

    ok = not missing
    return {
        "ok": ok, "degraded": False, "missing": missing,
        "hits": {"follow": follow, "comment": comment},
        "hint": "" if ok else str(penalty.get("提示") or _FALLBACK["判罚"]["提示"]),
        "penalty_score": int(penalty.get("分数") or 0),
        "detail": ([f"关注引导 {len(follow)} 处、互动引导 {len(comment)} 处，达标 ✅"] if ok
                   else [f"缺 {'、'.join(missing)}（成稿未完成粉丝沉淀/评论区运营）",
                         f"关注引导命中 {len(follow)} 处、互动引导命中 {len(comment)} 处"]),
    }


def fallback_texts(interaction_type: str = "", *, cfg: dict | None = None) -> tuple[str, str]:
    """按 interaction_type 取兜底话术 (关注引导, 互动引导)；缺配置用最小集。"""
    cfg = cfg if cfg is not None else (load_config() or _FALLBACK)
    tpl = cfg.get("兜底模板") or _FALLBACK["兜底模板"]
    follows = [str(x) for x in (tpl.get("关注") or []) if str(x).strip()] or _FALLBACK["兜底模板"]["关注"]
    inter = tpl.get("互动") or {}
    if not isinstance(inter, dict):
        inter = {}
    comment = str(inter.get(interaction_type or "") or inter.get("") or "").strip()
    if not comment:
        comment = _FALLBACK["兜底模板"]["互动"][""]
    return follows[0], comment


def inject(art: dict, *, cfg: dict | None = None) -> dict:
    """保存侧兜底：把缺失的那一类互动引导模板化注入正文（不编造事实、不覆盖已有引导）。

    返回 {"changed": bool, "injected": ["关注引导", ...]}；不改动 art 之外的任何状态。
    """
    content = art.get("content", "") or ""
    res = check(content, bait_field=art.get("interaction_bait", ""), cfg=cfg)
    if res["ok"] or res["degraded"]:
        return {"changed": False, "injected": [], "result": res}

    need = list(res["missing"])
    follow_txt, comment_txt = fallback_texts(art.get("interaction_type", ""), cfg=cfg)
    block = []
    if "关注引导" in need:
        block.append(follow_txt)
    if "互动引导" in need:
        prefix = {"站队式": "🗣️ 说说你的看法", "投票式": "📊 来投个票",
                  "预测式": "🔮 你的预测是", "共鸣式": "💬 有没有同感的",
                  "挑战式": "🤔 不服来辩", "调侃式": "😏 你们说呢"}
        p = prefix.get(art.get("interaction_type", ""), "💬 各位老铁")
        block.append(f"**{p}：{comment_txt}**\n👇 评论区见分晓！")
    if not block:
        return {"changed": False, "injected": [], "result": res}

    art["content"] = content.rstrip() + "\n\n---\n" + "\n".join(block) + "\n"
    art["interaction_bait_source"] = "template_fallback"
    art["interaction_bait_gate"] = {"injected": need, "before": res["hits"]}
    after = check(art["content"], bait_field=art.get("interaction_bait", ""), cfg=cfg)
    return {"changed": True, "injected": need, "result": after}


def selftest() -> list[tuple[str, bool]]:
    """冒烟自检：正例必过、缺任一类必拦、事实性用词不误判、降级必放行。"""
    cases: list[tuple[str, bool]] = []

    full = ("这球换你当裁判，吹不吹？评论区告诉我。\n---\n关注老六，这转会窗的瓜我替你盯着。")
    cases.append(("正例：关注+互动都有 → 达标", check(full)["ok"] is True))

    cases.append(("缺关注引导 → 不达标",
                  check("这球太离谱了。你怎么看？评论区聊聊。")["ok"] is False))
    cases.append(("缺互动引导 → 不达标",
                  check("这场球老六全程没缓过来。点个关注，后续我替你盯着。")["ok"] is False))
    cases.append(("两类都缺 → 不达标",
                  check("这是一篇干巴巴的赛报，没有引导。")["ok"] is False))

    # 事实性用词不得误判成钩子（「值得关注」不是引导）
    r = check("这笔引援值得关注，事态还在发酵。")
    cases.append(("「值得关注」不算关注引导", r["ok"] is False and "关注引导" in r["missing"]))

    # interaction_bait 字段可补足互动引导证据
    cases.append(("字段 interaction_bait 补足互动引导",
                  check("点个关注，后续我替你盯着。",
                        bait_field="你觉得谁会先换帅？")["ok"] is True))

    # 兜底注入后可达标
    art = {"content": "一篇没有任何引导的正文。", "interaction_type": "站队式"}
    out = inject(art)
    cases.append(("兜底注入后达标",
                  out["changed"] is True and check(art["content"])["ok"] is True))
    cases.append(("已有引导时不重复注入",
                  inject({"content": full, "interaction_type": "站队式"})["changed"] is False))
    cases.append(("首页已达标不注入（关注在正文）",
                  inject({"content": "点个关注不迷路。\n评论区见。"})["changed"] is False))

    # 降级：配置不可用必须放行，绝不阻塞
    _orig = globals()["load_config"]
    globals()["load_config"] = lambda *a, **k: None
    try:
        d = check("什么都没有。", cfg=None)
    finally:
        globals()["load_config"] = _orig
    cases.append(("配置不可用 → 放行且标记 degraded", d["ok"] is True and d["degraded"] is True))

    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="互动引导闸门（关注引导 + 互动引导）")
    ap.add_argument("--file", help="判定一个成稿 markdown 文件")
    ap.add_argument("--text", help="判定一段正文文本")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.file or args.text:
        text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
        r = check(text)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    cfg = load_config() or _FALLBACK
    print(f"互动引导闸门 v{cfg.get('版本')}")
    print(f"  口径出处：{cfg.get('口径出处', '')}")
    print(f"  判罚：{cfg.get('判罚')}")
    print(f"  关注引导模式 {len(cfg.get('关注引导') or [])} 条；"
          f"互动引导模式 {len(cfg.get('互动引导') or [])} 条")
    print("\n冒烟自检：")
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
