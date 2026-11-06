#!/usr/bin/env python3
"""互动引导闸门 —— 「互动引导」判定 + 「诱导话术」拦截（2026-11-06 合规修正）。

背景（R11 头条真实数据，2026-10-13）：
  「粉丝阅读占比中位 0%（成熟号 20-40%），互动率仅健康号 1/3，账号只吃推荐流量」。
  此前只写在提示词里，没有任何生成后校验。

**2026-11-06 修正（方向反了）**：
  旧实现在上面这条增长诉求上加了「必须有关注引导」，不达标就打回重写，
  最后由保存侧模板注入「关注老六，这波后续我替你盯着」。而《信用分机制》明列
  「诱导交互 20 分／违规涨粉 20 分／引导互粉 20 分」——也就是说，
  这套闸门在**为了让稿子涨粉，批量生产平台扣分的话术**。
  现在：`关注引导必需: false`（索要关注不再是达标项），并新增 `check_inducement()`
  把索要关注/扣1/站队/投票/回关互粉/博彩术语判成违规。

本模块两个职责（都在配置 `config/interaction_bait.yaml` 里）：
  1. ``check()``           —— 「是否提出了可讨论的问题」（质量项，缺了只记分）；
  2. ``check_inducement()`` —— 「是否出现诱导/博彩话术」（**合规项，命中即打回**）。

纪律（与全仓一致）：
  - 匹配**必须带引导语境**，避免「值得关注」这类事实性用词被误判成钩子；
  - 配置不可用 → **放行并置 degraded**，绝不因读不到配置而阻塞发布（计划九）；
  - 判定与兜底不改事实、不介入选题。

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
# 2026-11-06：`关注引导必需` 默认 false —— 索要关注是扣分项，不是达标项。
_FALLBACK = {
    "版本": "fallback",
    "关注引导必需": False,
    "关注引导": [r"(点|加|戳|求|记得|别忘|顺手|不妨)[^。！？!?\n]{0,6}(关注|追更|订阅|收藏)",
             r"(关注|追更|订阅)[^。！？!?\n]{0,10}(不迷路|后续|老六|我)", r"不迷路"],
    "互动引导": [r"评论区", r"留言",
             r"(说说|聊聊|告诉|发表|亮出)[^。！？!?\n]{0,4}(你的|你|看法|想法|选择)",
             r"(你|您|大家|各位)(觉得|怎么看|认为|选|站)", r"换你(当|来|做|上)"],
    "诱导话术": {
        "索要关注": [r"(点|加|戳|求|记得|别忘|顺手|不妨)[^。！？!?\n]{0,6}关注",
                 r"关注[^。！？!?\n]{0,6}(不迷路|我|老六|我们|本号)", r"不迷路"],
        "互关互粉": [r"(回关|互关|互粉|关注我我就|你关我我关你)"],
        "诱导互动": [r"(抠|扣)1(?!\d)|扣一|扣个1", r"(站队|来辩|不服来|投个票|投票|打个1)"],
        "博彩": [r"(赌球|博彩|下注|押注|庄家|盘口|让球盘|滚球|买球|外围盘|赔率盘)"],
    },
    "判罚": {"分数": 0, "动作": "打回重写",
             "提示": "正文不要索要关注，也不要用「扣1」「站队」「下注」这类话术。"},
    "兜底模板": {
        "关注": ["（已停用：索要关注属诱导交互，2026-11-06 起不再自动注入）"],
        "互动": {"": "你怎么看？"},
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

    # 2026-11-06：关注引导默认**不再是必须项**（索要关注＝诱导交互 20 分）。
    # 配置里 `关注引导必需: true` 可回滚到旧口径，代码不用动。
    follow_required = bool(cfg.get("关注引导必需", False))

    missing = []
    if follow_required and not follow:
        missing.append("关注引导")
    if not comment:
        missing.append("互动引导")

    ok = not missing
    return {
        "ok": ok, "degraded": False, "missing": missing,
        "follow_required": follow_required,
        "hits": {"follow": follow, "comment": comment},
        "hint": "" if ok else str(penalty.get("提示") or _FALLBACK["判罚"]["提示"]),
        "penalty_score": int(penalty.get("分数") or 0),
        "detail": ([f"互动引导 {len(comment)} 处，达标 ✅"
                    + (f"；关注引导 {len(follow)} 处" if follow else "")]
                   if ok else
                   [f"缺 {'、'.join(missing)}（成稿未给读者留下可回复的入口）",
                    f"互动引导命中 {len(comment)} 处"]),
    }


def check_inducement(content: str, *, cfg: dict | None = None) -> dict:
    """判定正文是否出现「诱导交互 / 违规涨粉 / 引导互粉 / 博彩」话术（合规项）。

    这是 2026-11-06 新增的判据，对应信用分扣分表的三条 20 分档与一条 70 分档：
      诱导交互 20 ｜ 违规涨粉 20 ｜ 引导互粉 20 ｜ 宣扬赌博（违反法律法规 70）

    返回 dict：
      ok        True = 干净；False = 命中诱导话术（**必须打回重写**）
      degraded  True = 配置不可用，放行（不阻塞）
      hits      [{"group": "索要关注", "pattern": "..."}]
      hint      打回时回灌给 LLM 的指引
    """
    content = (content or "").strip()
    raw = cfg if cfg is not None else load_config()
    if raw is None:
        if cfg is None:
            return {"ok": True, "degraded": True, "hits": [], "hint": "",
                    "detail": ["配置不可用，本闸门放行"]}
        raw = {}
    groups = raw.get("诱导话术") or _FALLBACK["诱导话术"]

    hits: list[dict] = []
    for name, pats in (groups or {}).items():
        for pat in _compile(pats or []):
            if pat.search(content):
                hits.append({"group": str(name), "pattern": pat.pattern})

    if not content:
        # 空正文不在这里判（那是 TT-10 格式低质的事），避免两个判据都报空
        return {"ok": True, "degraded": False, "hits": [], "hint": "",
                "detail": ["正文为空，不判诱导话术"]}

    if not hits:
        return {"ok": True, "degraded": False, "hits": [], "hint": "",
                "detail": ["未命中诱导话术 ✅"]}

    penalty = raw.get("判罚") or _FALLBACK["判罚"]
    return {
        "ok": False, "degraded": False, "hits": hits,
        "hint": str(penalty.get("提示") or _FALLBACK["判罚"]["提示"]),
        "detail": [f"命中诱导话术 {len(hits)} 处：" +
                   "、".join(sorted({h["group"] for h in hits}))],
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
    """保存侧兜底：把缺失的那一类互动引导补进正文（不编造事实、不覆盖已有引导）。

    2026-11-01 改：**去掉模板外壳**。此前一律拼成
      `---\n**💬 各位老铁：{话术}**\n👇 评论区见分晓！`
    结果全站每篇结尾一模一样，读者一眼看出机器味（10 月 249 篇命中，等于每篇都贴）。
    现在只把话术本身接到结尾，不加分隔线、不加 emoji 前缀、不加固定感叹句。

    **注意**：本函数是兜底，不是主路径。真正的根治在生成侧 prompt（见
    scripts/refill_reservoir.py 的 _make_one 自检 + prompts/rewrite_article.txt）。
    若某类内容长期高频命中这里，说明它的生产 prompt 没写要求，要去补 prompt，
    而不是把兜底话术改得更花。

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
        block.append(comment_txt)
    if not block:
        return {"changed": False, "injected": [], "result": res}

    art["content"] = content.rstrip() + "\n\n" + " ".join(block) + "\n"
    art["interaction_bait_source"] = "template_fallback"
    art["interaction_bait_gate"] = {"injected": need, "before": res["hits"]}
    after = check(art["content"], bait_field=art.get("interaction_bait", ""), cfg=cfg)
    return {"changed": True, "injected": need, "result": after}


def selftest() -> list[tuple[str, bool]]:
    """冒烟自检：正例必过、缺互动必拦、事实性用词不误判、降级必放行、诱导必拦。"""
    cases: list[tuple[str, bool]] = []

    full = "这球换你当裁判，吹不吹？评论区告诉我。"
    cases.append(("正例：提出可讨论的问题 → 达标", check(full)["ok"] is True))

    cases.append(("只索要关注、没抛问题 → 不达标",
                  check("点个关注，明天赛后第一时间来复盘。")["ok"] is False))
    cases.append(("两类都缺 → 不达标", check("这是一篇干巴巴的赛报，没有引导。")["ok"] is False))

    # 2026-11-06：关注引导不再是必须项（索要关注本身是扣分项）
    r = check("点个关注，后续我替你盯着。你怎么看？")
    cases.append(("关注引导非必需：有关注也照样达标", r["ok"] is True))
    cases.append(("关注引导非必需：没关注也达标",
                  r["follow_required"] is False
                  and check("你怎么看？")["ok"] is True))

    # 事实性用词不得误判成钩子（「值得关注」不是引导）
    r = check("这笔引援值得关注，事态还在发酵。")
    cases.append(("「值得关注」不算关注引导", r["ok"] is False and "互动引导" in r["missing"]))

    # interaction_bait 字段可补足互动引导证据
    cases.append(("字段 interaction_bait 补足互动引导",
                  check("就这样。",
                        bait_field="你觉得谁会先换帅？")["ok"] is True))

    # 兜底注入后可达标；且**不再注入关注话术**
    art = {"content": "一篇没有任何引导的正文。", "interaction_type": "站队式"}
    out = inject(art)
    cases.append(("兜底注入后达标",
                  out["changed"] is True and check(art["content"])["ok"] is True))
    cases.append(("兜底不再注入「关注」话术",
                  "关注" not in art["content"].split("正文。", 1)[-1]))
    cases.append(("已有引导时不重复注入",
                  inject({"content": full, "interaction_type": "站队式"})["changed"] is False))

    # ── 诱导话术黑名单（2026-11-06 新增，对应扣分表 20 分档 / 70 分档）──
    for bad, name in (
        ("关注老六，这波后续我替你盯着。", "索要关注"),
        ("关注我，我必回关。", "互关互粉"),
        ("同意的扣1。", "诱导互动"),
        ("这单我押注主队。", "博彩"),
        ("打开盘口看看水位。", "博彩"),
    ):
        d = check_inducement(bad)
        cases.append((f"诱导话术「{name}」必拦", d["ok"] is False))
    cases.append(("干净正文不误报诱导", check_inducement(full)["ok"] is True))
    cases.append(("事实性用词不误报诱导",
                  check_inducement("这笔引援值得关注，事态还在发酵。")["ok"] is True))

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
