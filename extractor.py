"""抽取器（计划 11.3 素材边界 · 物理隔离）。

架构约定（不可违背）：
    正文 → 抽取器 → 结构化事实三元组 → 生成器
    抽取器「允许」看到正文全文；生成器「永远看不到」正文原文。

因此本模块的输出（结构化事实块 build_facts_block）是生成器唯一可见的素材。
抽取器提示词严格禁止输出任何原文句子，且输出在离开本模块前会再过一道
「逐字比对」闸门（assert_no_verbatim），把任何与源文连续重合 ≥12 字的片段剔除。

抓正文的真实价值在于事实密度（数字、日期、引语细节），而非文本可用性。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 抽取器系统指令：只提取事实，绝不输出原文句子
_EXTRACTOR_SYSTEM = (
    "你是足球资讯抽取器。你的唯一职责是把正文拆解为结构化事实三元组。"
    "硬性规则：1) 只输出结构化字段，禁止输出任何原文句子、原文短语、原文标点序列；"
    "2) 每个事实给出 主体/动作/数值/时间 四个字段，缺失留空字符串；"
    "3) 数值字段只放数字型信息（比分、金额、年限、排名、分钟、进球数等）；"
    "4) 不得推断、不得补充正文没有的信息。只输出 JSON。"
)

_EXTRACTOR_TMPL = """请把下面这段足球资讯正文抽取为结构化事实列表。

正文：
{article_text}

输出纯 JSON（不要任何 markdown 包裹）：
{{"facts": [{{"主体": "球员/球队/教练/机构", "动作": "转会/续约/伤停/表态/处罚/进球/取胜", "数值": "金额/年限/比分/排名（无则空字符串）", "时间": "事实发生时间（无则空字符串）"}}]}}

要求：最多 {max_facts} 条；只保留有信息量的事实；禁止输出任何原文句子。"""

# 常见动作词（正则兜底用）
_ACTION_WORDS = (
    "转会", "加盟", "签约", "续约", "解约", "租借", "报价", "要价", "谈判",
    "伤停", "受伤", "缺阵", "复出", "报销", "赛季报销",
    "表态", "表示", "回应", "炮轰", "否认", "确认", "官宣",
    "处罚", "禁赛", "罚款", "指控", "调查",
    "取胜", "战平", "不敌", "击败", "逆转", "绝杀", "梅开二度", "帽子戏法",
    "进球", "破门", "助攻", "破纪录", "创纪录",
)

_FACT_NUM_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?\s*(?:%|％|万欧|万欧元|亿欧|亿欧元|万|亿|岁|分钟|分|球|场|次|名|位|年))"
)
_SCORE_RE = re.compile(r"\d{1,2}\s*[-:比]\s*\d{1,2}")
_DATE_RE = re.compile(r"(\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2})")
_ZH_NAME_RE = re.compile(r"[一-鿿]{2,6}")

# 主体抽取的停用词（时间/连接/语气，不能当主体）
_SUBJECT_STOP = {
    "北京时间", "今天", "昨天", "昨日", "目前", "据悉", "此役过后", "赛后",
    "上半场", "下半场", "开场", "终场", "本轮", "本场", "最终", "分钟", "此前",
    "报道", "消息", "记者", "球队", "俱乐部", "双方", "对方", "全场",
}

_ENTITY_NAMES: set[str] | None = None


def _entity_names() -> set[str]:
    """加载中文实体词典（config/entity_map.json），用于锚定主体。"""
    global _ENTITY_NAMES
    if _ENTITY_NAMES is None:
        names: set[str] = set()
        try:
            from constants import PROJECT_ROOT
            p = Path(PROJECT_ROOT) / "config" / "entity_map.json"
            d = json.loads(p.read_text(encoding="utf-8"))
            ent = d.get("entities", {})
            it = ent.values() if isinstance(ent, dict) else (ent or [])
            for v in it:
                if not isinstance(v, dict):
                    continue
                for k in ("name_zh", "name_en"):
                    if v.get(k):
                        names.add(str(v[k]))
                for a in (v.get("aliases") or []):
                    names.add(str(a))
        except Exception:
            names = set()
        _ENTITY_NAMES = names
    return _ENTITY_NAMES


def _pick_subject(sent: str, action: str) -> str:
    """保守取主体：优先命中实体词典；否则取动作词前的 2-4 字中文名并过滤停用词。"""
    names = _entity_names()
    hit = sorted((n for n in names if n and n in sent), key=len, reverse=True)
    if hit:
        return hit[0]
    head = sent.split(action)[0] if action else sent
    for cand in reversed(_ZH_NAME_RE.findall(head)):
        if cand in _SUBJECT_STOP:
            continue
        return cand
    return ""


def _norm(s) -> str:
    if s is None:
        return ""
    if isinstance(s, (int, float)):
        return str(s)
    return re.sub(r"\s+", " ", str(s)).strip()


def _regex_facts(article_text: str, source_name: str, max_facts: int) -> list[dict]:
    """无 LLM 时的降级抽取：按句子切分，抓含动作词或数字的句子→结构化字段。

    注意：这里仍然只输出结构化字段，绝不把整句原文放进任何字段。
    """
    facts = []
    # 按中文句读切句
    sentences = re.split(r"[。！？；\n]+", article_text or "")
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 8:
            continue
        action = next((w for w in _ACTION_WORDS if w in sent), "")
        nums = list(_SCORE_RE.findall(sent)) + _FACT_NUM_RE.findall(sent)
        dates = _DATE_RE.findall(sent)
        if not action and not nums:
            continue
        # 主体：优先命中实体词典；否则取动作词前的中文名（保守，不臆测）
        subject = _pick_subject(sent, action)
        facts.append({
            "主体": subject,
            "动作": action,
            "数值": "、".join(dict.fromkeys(nums)) if nums else "",
            "时间": dates[0] if dates else "",
            "来源": source_name,
        })
        if len(facts) >= max_facts:
            break
    return facts


def extract_facts(article_text: str, source_meta: dict | None = None, *,
                  call_llm=None, max_facts: int = 40) -> list[dict]:
    """把正文抽为结构化事实列表（抽取器唯一出口）。

    返回 [{"主体","动作","数值","时间","来源"}, ...]。抽取器允许看原文；
    但输出字段只承载结构化信息，调用方必须用 build_facts_block() 送进生成器。
    """
    source_name = (source_meta or {}).get("source", "") or "未知来源"
    text = (article_text or "").strip()
    if not text:
        return []

    facts: list[dict] = []
    if call_llm is not None:
        try:
            from constants import LLM_JSON_CANDIDATES  # 延迟导入，避免顶层依赖
            msgs = [
                {"role": "system", "content": _EXTRACTOR_SYSTEM},
                {"role": "user", "content": _EXTRACTOR_TMPL.format(
                    article_text=text[:6000], max_facts=max_facts)},
            ]
            obj, _model = call_llm(msgs, LLM_JSON_CANDIDATES,
                                   temperature=0.1, max_tokens=2048)
            raw = obj.get("facts") if isinstance(obj, dict) else None
            if isinstance(raw, list):
                for f in raw:
                    if not isinstance(f, dict):
                        continue
                    facts.append({
                        "主体": _norm(f.get("主体")),
                        "动作": _norm(f.get("动作")),
                        "数值": _norm(f.get("数值")),
                        "时间": _norm(f.get("时间")),
                        "来源": source_name,
                    })
        except Exception as e:  # 抽取失败不阻断，降级为正则抽取
            print(f"   ⚠️ 抽取器 LLM 失败，降级正则抽取: {type(e).__name__}")

    if not facts:
        facts = _regex_facts(text, source_name, max_facts)

    # 去重（同主体+同动作+同数值视为一条）
    seen, uniq = set(), []
    for f in facts:
        key = (f.get("主体", ""), f.get("动作", ""), f.get("数值", ""))
        if key in seen or not (f.get("主体") or f.get("动作") or f.get("数值")):
            continue
        seen.add(key)
        uniq.append(f)
    return uniq[:max_facts]


def facts_from_fixture(fixture: dict | None) -> list[dict]:
    """从比赛 fixture 生成事实（比分/对阵），确定性，无原文文本。"""
    if not fixture:
        return []
    out = []
    home = _norm(fixture.get("home_team"))
    away = _norm(fixture.get("away_team"))
    hs, as_ = fixture.get("home_score"), fixture.get("away_score")
    league = _norm(fixture.get("league"))
    date = _norm(fixture.get("utc_date"))[:10]
    src = _norm(fixture.get("source")) or "赛程库"
    if home and away:
        score = f"{hs}-{as_}" if hs is not None and as_ is not None else ""
        out.append({"主体": f"{home} vs {away}", "动作": "对阵", "数值": score,
                    "时间": date, "来源": src})
        if league:
            out.append({"主体": league, "动作": "赛事", "数值": "", "时间": date,
                        "来源": src})
    return out


def _longest_shared_run(a: str, b: str) -> str:
    """返回 a 与 b 的最长公共连续子串（≤64 字，控制成本）。"""
    if not a or not b:
        return ""
    a2, b2 = a[:4000], b[:4000]
    n = min(64, len(a2))
    for L in range(n, 11, -1):  # 只关心 >11 字的重合
        seen = {a2[i:i + L] for i in range(len(a2) - L + 1)}
        for i in range(len(b2) - L + 1):
            seg = b2[i:i + L]
            if seg in seen:
                return seg
    return ""


def assert_no_verbatim(block: str, sources, max_run: int = 12) -> tuple[bool, str]:
    """硬闸门：确认结构化事实块与任何源文没有连续 ≥max_run 字的逐字重合。

    只要发现一处，即判定隔离失效（返回 False 与命中片段）。
    """
    sources = [s for s in (sources or []) if isinstance(s, str) and len(s) >= max_run]
    for s in sources:
        run = _longest_shared_run(block, s)
        if len(run) >= max_run:
            return False, run
    return True, ""


def build_facts_block(facts, sources=None, source_meta: dict | None = None) -> str:
    """把结构化事实渲染为生成器可见的「事实块」。

    事实块只承载结构化字段；渲染后会再过 assert_no_verbatim，
    若与源文出现 ≥12 字逐字重合，则剔除该行（隔离兜底）。
    """
    if not facts:
        return "（抽取器未产出结构化事实）"
    lines = []
    for f in facts:
        parts = [p for p in (f.get("主体"), f.get("动作"), f.get("数值"), f.get("时间")) if p]
        if not parts:
            continue
        line = "- " + " | ".join(parts)
        if f.get("来源"):
            line += f" （来源：{f['来源']}）"
        lines.append(line)
    block = "结构化事实（抽取器输出，禁止逐句复述）：\n" + "\n".join(lines)

    if sources:
        ok, hit = assert_no_verbatim(block, sources)
        if not ok:
            # 逐行剔除命中行
            clean = []
            for ln in lines:
                bad, _ = assert_no_verbatim(ln, sources)
                if bad:
                    clean.append(ln)
            block = "结构化事实（抽取器输出，禁止逐句复述）：\n" + "\n".join(clean)
    return block


def extract_fact_cards(source: dict, *, call_llm=None, max_facts: int = 40) -> list[dict]:
    """便捷入口：从改写路径的 source 结构产出结构化事实（含 fixture 事实）。"""
    if not isinstance(source, dict):
        return []
    texts, meta = [], {}
    fixture = source.get("fixture") or {}

    art_text = source.get("article_text") or source.get("content") or ""
    if isinstance(art_text, str) and len(art_text) >= 50:
        texts.append(art_text)
        meta["source"] = fixture.get("source") or source.get("source") or ""

    ftext = fixture.get("article_text") if isinstance(fixture, dict) else None
    if isinstance(ftext, str) and len(ftext) >= 50 and ftext not in texts:
        texts.append(ftext)
        meta.setdefault("source", fixture.get("source") or "")

    facts = facts_from_fixture(fixture)
    for t in texts:
        facts.extend(extract_facts(t, meta, call_llm=call_llm, max_facts=max_facts))

    # 去重
    seen, uniq = set(), []
    for f in facts:
        key = (f.get("主体", ""), f.get("动作", ""), f.get("数值", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq[:max_facts]


def build_source_facts_block(source: dict, *, call_llm=None) -> tuple[str, list[dict]]:
    """给定 source，返回 (生成器可见的事实块, 结构化事实列表)。

    生成器只应使用这里返回的事实块作为素材，绝不接触 source 里的原文。
    """
    facts = extract_fact_cards(source, call_llm=call_llm)
    sources = []
    if isinstance(source, dict):
        for k in ("article_text", "content", "text"):
            v = source.get(k)
            if isinstance(v, str) and len(v) >= 50:
                sources.append(v)
        fx = source.get("fixture")
        if isinstance(fx, dict) and isinstance(fx.get("article_text"), str):
            sources.append(fx["article_text"])
    block = build_facts_block(facts, sources=sources)
    return block, facts
