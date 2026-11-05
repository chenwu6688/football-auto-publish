"""发布闸门分级（v2）：把「哪道闸门硬拦、哪道只记分」收敛成一张表。

## 为什么要有这个模块

2026-11-05 的评估结论：问题不是「审得严」，是**审的姿势错了**。
旧实现把 7 道判据全部串成「与门」，任一道不过就整篇重写；而第 3 道
（一致性校验）是唯一会写熔断计数的——它误报一次，代价是全批停发。

实测：单道通过率 95% 时，7 道串联全过只有 69.8%；收敛为 2 道后是 90.2%。
而 10-12 至 11-05 的 4 次熔断**全是解析误报、零真实违规**。

## 分级依据（不是拍脑袋）

| 判据 | 旧 | v2 | 依据 |
|---|---|---|---|
| 事实矛盾（比分方向/数值、进球数、编造对阵） | 硬 | **硬** | 直接违反「不臆造」，用户核心诉求 |
| 合规（禁发类型、传闻断言、AI 标识） | 硬 | **硬** | 违反即扣分/封号，不可让 |
| 改写保真·名字缺失（没写球员/队名） | 硬 | 记分 | 正文用「他」「主队」指代是正常写法，不是篡改 |
| 反洗稿（相似度/信息增量） | 硬 | 记分 | 只影响「增量」（有没有人看），不伤「存量」（信不信） |
| 标题评分（及格线 60） | 硬 | 记分 | 同上；另有三要素机械兜底 |
| 互动引导 | 硬（末次放行） | 记分 | 已有保存侧模板兜底注入 |
| 字数 | 硬 | 记分 | 生成提示词已约束，硬拦只会逼模型注水 |

## 关键约束

1. **只在真正冲突时拦**：判据来自 `fact_assert`（矛盾校验），不是存在性校验。
2. **旧判据同期保留做对照**：v2 下旧判据照跑，结果写进 `_gate.legacy`，不阻断。
   这样「新判据放过了谁、旧判据本来会拦谁」每天都有数据可查——不是拍脑袋放松。
3. **可一键回滚**：环境变量 `GATE_MODE=strict` 即回到旧的 7 道与门口径，
   不需要改代码、不需要回滚提交。今晚出问题就设这一个变量。
4. **空事实卡不判**：`fact_assert` 在无队名无比分时 `judged=False`，
   此时不拦也不当通过——记 `judged: false`，口径是「未判定 ≠ 通过」。

用法：
    import gate_v2
    g = gate_v2.fact_gate(art, fixture, ctx, subject="选题名",
                          legacy=(passed, issues))
    if g["block"]:      # 只在 mode=v2 且真矛盾时 True
        ...
"""

from __future__ import annotations

import os

#: v2（默认）＝只有 hard 表里的判据阻断；strict＝旧口径，全部阻断。
#: 回滚开关：GATE_MODE=strict。不需要改代码。
DEFAULT_MODE = "v2"

#: 阻断级判据。顺序即提示词里的优先级。
HARD_GATES = ("fact_conflict", "compliance")

#: 只记分（写进 metadata 与 degrade.note，不进打回路径）。
SCORE_GATES = ("fidelity_name", "anti_copy", "title_score",
               "interaction", "word_count", "coverage")

#: strict 模式下额外恢复为阻断的判据（旧口径）。
_STRICT_EXTRA = ("fidelity_name", "anti_copy", "title_score")

#: 旧判据 issues 里的**矛盾类**措辞：命中即视为 v2 硬闸门，不许随「降为记分」一起放过。
#: 为什么必须留：旧判据混着两种东西——「缺少队名」（存在性，正文用「他们」指代很正常）
#: 与「卡外比分 9-0」（数值冲突，就是臆造）。整条降级会把后者一起放掉，
#: 而实测 11-05 的 test_degrade 用例正是拿「卡外比分」当样本，一降就放过去了。
#: **按语义分类，不按判据名分类**——这是本次改造的核心，别在这里偷懒。
LEGACY_CONFLICT_MARKERS = (
    "卡外比分", "比分不一致", "比分不符", "比分方向",
    "进球数", "编造", "不存在", "与事实卡冲突",
)


def mode() -> str:
    """当前闸门模式。v2 = 新口径（默认），strict = 旧口径（回滚用）。"""
    m = (os.environ.get("GATE_MODE") or "").strip().lower()
    return m if m in ("v2", "strict") else DEFAULT_MODE


def blocking(name: str) -> bool:
    """该判据在当前模式下是否阻断发布。未知判据保守视为不阻断。"""
    if name in HARD_GATES:
        return True
    if mode() == "strict" and name in _STRICT_EXTRA:
        return True
    return False


def tier_of(name: str) -> str:
    return "hard" if name in HARD_GATES else (
        "score" if name in SCORE_GATES else "unknown")


def _fact_assert():
    import fact_assert
    return fact_assert


# ── 新判据首日的保险丝 ────────────────────────────────────────────────
# `fact_assert` 是 2026-11-05 才接进生产的新判据，单测覆盖了 4 个历史熔断现场与
# 4 类真矛盾，但生产数据比单测脏。若它把一整批的正常稿都判成「矛盾」，
# 后果和旧熔断一样：全批停发。
#
# 所以加一条**批内配额**：本批被判矛盾的不同选题达到 ``MAX_CONFLICT_SUBJECTS``
# 篇后，剩余稿子改「记分不拦」并在 metadata 留 ``fail_open`` 原因。
# 依据：同一批 3 篇独立选题同时臆造，比「判据系统性误报」的概率低得多；
# 而误报的代价是全批归零，两者不对等。
#
# 注意：**只在 v2 下生效**。GATE_MODE=strict 回滚时保持旧行为（该停就停）。
MAX_CONFLICT_SUBJECTS = 3

_conflict_subjects: list[str] = []


def reset_batch() -> None:
    """开批时清空批内计数（CI 里每批一个新进程，这里是给同进程多批的调用方用）。"""
    _conflict_subjects.clear()


def conflict_subjects() -> list[str]:
    return list(_conflict_subjects)


def _quota_exhausted(subject: str) -> bool:
    """记一次矛盾选题；返回 True 表示本批配额已用尽（应改记分）。"""
    key = str(subject or "").strip() or "(无选题)"
    if key not in _conflict_subjects:
        _conflict_subjects.append(key)
    # 严格大于：阈值是「已经拦下的篇数」。第 MAX 篇仍要拦，第 MAX+1 篇才放。
    # 写成 >= 会让第 3 篇（也就是阈值本身那篇）被放过，保险丝提前一道生效。
    return len(_conflict_subjects) > MAX_CONFLICT_SUBJECTS


def fact_gate(article: dict, fixture: dict, match_context=None, *,
              subject: str = "", legacy=None) -> dict:
    """事实闸门：v2 用「矛盾校验」，strict 用旧判据。

    参数 ``legacy`` 是 ``(passed, issues)``——旧判据（存在性）的结果。
    **两种模式下都会跑**（调用方负责跑），这里只决定谁说话：

    - v2：矛盾才拦；旧判据的 issues 全部降为记录。
    - strict：旧判据说了算（回滚口径）。

    返回::

        {"block": bool, "hint": str, "record": {...}}

    ``record`` 恒含 ``mode`` / ``judged`` / ``contradictions`` /
    ``suspicious`` / ``missing`` / ``coverage`` / ``legacy_issues``，
    直接存进 metadata 的 ``_gate`` 字段，供「新判据放过了谁」复盘。
    """
    rec = {"mode": mode(), "judged": False, "contradictions": [],
           "suspicious": [], "missing": [], "coverage": None,
           "legacy_issues": []}
    if subject:
        rec["subject"] = str(subject)[:60]
    if legacy:
        passed, issues = legacy
        rec["legacy_issues"] = list(issues or []) if not passed else []

    try:
        res = _fact_assert().verify(article, fixture, match_context=match_context)
    except Exception as e:  # 判据本身炸了不许阻断发布（埋点不是闸门）
        rec["error"] = f"{type(e).__name__}: {e}"
        res = None

    hint = ""
    block = False
    if res is not None:
        rec.update({k: res.get(k) for k in
                    ("judged", "contradictions", "suspicious", "missing", "coverage")})
        if res.get("contradictions"):
            # hint 会进 retry_hint 交给模型重写。直接 str(dict) 模型看不懂，
            # 重试等于白跑一次（实测日志里就是 `{'type': 'score_direction', ...}`）。
            # 取其中人可读的 detail，拼成一句能照着改的话。
            hint = "；".join(
                str(c.get("detail") or c) if isinstance(c, dict) else str(c)
                for c in res["contradictions"][:3])

    if mode() == "strict":
        if rec["legacy_issues"]:
            block, hint = True, "；".join(rec["legacy_issues"][:3])
        else:
            # 旧判据过了也不算完：新判据仍可拦（回滚只恢复旧口径的严格度，
            # 不是关掉事实校验）。此前写成 `elif not hint` 是错的——
            # fact_assert 有矛盾时 hint 已非空，那个分支永远进不去，等于漏拦。
            block = bool(rec["contradictions"]) or (
                res is not None and res.get("ok") is False)
    else:
        block = bool(rec["contradictions"])
        # 旧判据里的矛盾类措辞（卡外比分/进球数造假…）在 v2 下仍是硬闸门。
        # 这类判据 fact_assert 覆盖不到全部形态（如事实卡缺比分时它判不了），
        # 所以不能让「整条降为记分」把它们一起放掉。
        rec["legacy_conflicts"] = [x for x in rec["legacy_issues"]
                                   if any(m in str(x) for m in LEGACY_CONFLICT_MARKERS)]
        if rec["legacy_conflicts"] and not block:
            block = True
            hint = "；".join(rec["legacy_conflicts"][:3])
        # 新判据首日保险丝：本批矛盾选题已满额 → 改记分，交人工复盘
        if block and _quota_exhausted(rec.get("subject", "")):
            block = False
            rec["fail_open"] = (
                f"本批事实矛盾已涉及 {len(_conflict_subjects)} 篇选题"
                f"（阈值 {MAX_CONFLICT_SUBJECTS}），疑判据误报，本篇改记分不拦截")
            try:
                print(f"   ⚠️ {rec['fail_open']}：{hint[:60]}")
            except Exception:
                pass

    if block and not hint:
        hint = "事实与本场数据冲突"
    return {"block": block, "hint": hint, "record": rec}


def attach(article: dict, record: dict, key: str = "fact") -> None:
    """把闸门记录挂到成稿上（只进 metadata，不进正文）。"""
    if not isinstance(article, dict):
        return
    gate = article.setdefault("_gate", {})
    if isinstance(gate, dict):
        gate[key] = record


def summary(articles) -> dict:
    """整批闸门汇总，供 metadata 与验收读取。"""
    n = {"articles": 0, "fact_blocked": 0, "legacy_would_block": 0,
         "unjudged": 0}
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        n["articles"] += 1
        g = (a.get("_gate") or {}).get("fact") or {}
        if g.get("contradictions"):
            n["fact_blocked"] += 1
        if g.get("legacy_issues"):
            n["legacy_would_block"] += 1
        if g.get("judged") is False:
            n["unjudged"] += 1
    n["mode"] = mode()
    return n
