#!/usr/bin/env python3
"""标题特征六维提取 —— 对标与回流共用的**唯一**口径（2026-11-01 头部账号对标）。

为什么单独建一个模块：
  2026-11-01 抓了 4 个头条足球头部号 181 条真实作品（标题 + 阅读量），
  得出的结论要用于「改标题」，就必须和「验收改没改」用同一套判定。
  全仓已经因为「比分口径三处各写各的」（10-24 熔断事故）吃过一次教训，
  所以特征定义只此一份，`scripts/benchmark_peers.py`、`content_mix`、
  未来的标题闸门全部从这里取，禁止各自写正则。

六维特征与实测结论（头部 181 条，中位阅读）：

    数字 + 感叹            n=88   中位 1354   过千 59%
    数字 + 情绪            n=30   中位 1124   过千 53%
    感叹但无数字            n=10   中位  236   过千 20%
    数字但无感叹无情绪        n=39   中位  172   过千 23%
    书面语倾向              n=13   中位   26   过千 23%
    三者都没有              n=35   中位   76   过千 11%

  我方同期 76 篇：无数字无感叹无情绪 44 条（58%）→ 中位阅读 4。

「过程锚点」是本轮最重要的新增维度：头部爆款靠「让3追4」「第94分钟」
「7轮6场」「球迷第80分钟退场」这种**过程反常**制造点开理由；
而我方 76 篇命中数是 0，全是「…他图个啥？」这类没有过程的空问。

纪律：
  - 本模块只做**统计与打标**，不判定合格/不合格（判罚在闸门里，口径另行配置）；
  - 正则一律保守，宁可少认不误认；新增词必须同步补单测用例；
  - 不引入外部依赖，不读配置（特征词表内聚在此，避免配置漂移）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

# ---------------------------------------------------------------- 过程锚点
# 「过程锚点」= 标题里出现了比赛中**过程本身的反常**，而不是结果。
# 每个词后面都标了真实爆款出处，便于以后增删时有据可依。
PROCESS_ANCHOR_PATTERNS: tuple[tuple[str, str], ...] = (
    # 让X追Y / 落后X球翻盘
    ("让球逆转", r"让[一二三四1-4]追[一二三四1-4]"),
    ("追平/逆转", r"(逆转|翻盘|大翻盘|反超再反超)"),
    ("补时/读秒", r"(读秒|补时第?\d+分钟|第\d+分钟(绝杀|丢球|扳平)|终场前)"),
    # 2026-11-03 补「动作词 + 时长」形态：真实标题写的是「场边站满90分钟」「近3场仅踢66分钟」，
    # 旧正则只认带「第」的句式，把这类过程全漏了（与 10-24「0-1 vs 0比1」同源的写法差异）。
    # 前置动作词是刻意的：裸「满90分钟」可能只是描述比赛时长，不是反常。
    ("第N分钟", r"(第\s?\d{1,3}\s?分钟|"
                r"(?:站|踢|打|守|撑|熬|跑|枯坐|坐)\s?满?\s?\d{1,3}\s?分钟)"),
    ("N分钟N球", r"\d{1,3}\s?分钟(内)?(连)?(轰|进|丢|打|灌)\s?\d{1,2}\s?球"),
    ("连丢/连进N球", r"连(丢|进|轰|失)\s?\d{1,2}\s?球"),
    ("N轮不胜/连败", r"\d{1,2}\s?轮(不胜|连败|不败|全胜|首胜)|近\s?\d{1,2}\s?轮"),
    ("积分/分差", r"(领先|落后|甩开|追到|仅差|只差|剩)\s?\d{1,2}\s?分"),
    # 2026-11-03 补：① 中文数字（真实标题「大连九人苦撑3-4」，只认 ASCII 的 9人 会漏）；
    #                  ② 「染红」这一动词形态（「挥拳染红致歉」）。
    # 中文数字必须跟谓词（应战/苦撑/硬扛…），否则「九人轮换」这种正常轮换会被误判成少打一人。
    ("红牌/罚下", r"(红牌|罚下|两黄|染红|"
                 r"(?:9|10|8|九|十|八)\s?人(?:应战|苦撑|硬扛|作战|撑完|踢完)|少打一人)"),
    ("伤退/赛季报销", r"(伤退|赛季报销|重伤|骨折|旧伤复发|缺阵至)"),
    ("替补建功", r"(替补|替身|板凳)(登场)?(建功|绝杀|扳平|破门|双响)"),
    ("换人见效", r"(换人|替补席|临场调整).{0,6}(见效|奏效|改变|翻盘)"),
    # 2026-11-03 新增：旧「换人见效」只收正面结果词，把「换人失误葬送好局」「还没换人他先动了」
    # 这类**过程判断**全漏掉了。换人是过程，做对做错都是读者不知道结局的信息，故独立成类。
    # 独立成类而不是并进「换人见效」，是为了后续能按类做 CTR 归因（做对/做错可能是两种阅读表现）。
    ("换人时机", r"(换人|变阵|临场调整).{0,6}(失误|葬送|太晚|迟到|失败)|"
                r"(?:还没|迟迟不|未|不肯)换人"),
    # 2026-11-03 新增：赛场外过程。真实标题「赛前加练到凌晨」13 类里没有归属。
    # **刻意不收裸「熬夜」**：初版收过，结果「欧冠大戏…这四场不熬夜真亏」被判成有锚点
    # ——那是劝读者别熬夜，不是球队的过程。只保留「通宵」「凌晨+加练/训练/抵达/开会」。
    ("赛前异动", r"(加练|通宵|凌晨.{0,6}(加练|训练|抵达|开会)|赛前.{0,4}(密谈|开会|加练))"),
    ("退场/离场", r"(退场|离场|提前离场|被换下)"),
)

# 曾把「终结…纪录/魔咒/荒」算作锚点，2026-11-01 自检推翻了：
#   「2026金球奖揭晓：凯恩凭73球夺魁，终结英格兰25年奖项荒」实测 4 阅读。
# 它陈述的是**结果**（谁终结了什么），不是过程，与「过程锚点」定义相反，故移出。
# 教训：锚点维度只能装"读者不知道结局"的信息，一旦包含结局，就退化成赛果播报。

# 情绪词：只说「事情有多惨/多炸」，不涉及过程
EMOTION_PATTERN = re.compile(
    r"(大败|崩盘|反水|拉胯|爆冷|崩|怒|惨|耻辱|羞愧|捂脸|王者|史诗|"
    r"彻底|惊人|疯狂|复活|梦碎|失控|无敌|封神|翻车|打脸|离谱|破防|"
    r"绝杀|血洗|屠杀|哑火|黯然|落泪|哭了|神伤)"
)

# 书面语倾向：正式报道体的动词，实测这类标题中位阅读最低（26）
BOOKISH_PATTERN = re.compile(
    r"(降服|闪耀|难求一胜|正名|荣膺|惜败|锁定|领衔|坐镇|出击|多点开花|"
    r"挺进|力克|折桂|问鼎|加冕|官宣|揭幕|收关|收官)"
)

# 顶流人名 / 豪门：实测**必须账号内比较**才有意义
# （全样本 0.32x 负相关，控制账号后 3.53x 正相关 —— Simpson 悖论）
TOP_NAME_PATTERN = re.compile(
    r"(梅西|C罗|凯恩|姆巴佩|亚马尔|哈兰德|贝林厄姆|维尼修斯|内马尔|萨拉赫|"
    r"齐达内|小罗|瓜迪奥拉|穆里尼奥|安切洛蒂|阿尔特塔|"
    r"曼联|皇马|巴萨|拜仁|利物浦|阿森纳|曼城|切尔西|国米|米兰|尤文|巴黎)"
)

_CONFLICT_PATTERN = re.compile(
    r"(炮轰|质疑|怒斥|争议|遭|喊话|反击|回击|道歉|处罚|举报|开炮|不满|"
    r"骂|打脸|拒绝|风波|危机|严惩|点名|互撕|交涉)"
)

_RE_NUM = re.compile(r"\d|[一二三四五六七八九十百千万亿]{1,4}(?=[球分场轮人天年次])")
_RE_EXCL = re.compile(r"[!！]")
_RE_QUES = re.compile(r"[?？]")
_RE_COLON = re.compile(r"[：:]")
_RE_HAN = re.compile(r"[\u4e00-\u9fa5]")


@dataclass(frozen=True)
class TitleFeatures:
    """一条标题的六维特征。字段名即报表列名，改动需同步下游。"""
    han_len: int                # 汉字数
    has_number: bool            # 含具体数字
    has_exclaim: bool           # 含感叹号
    has_question: bool          # 含问号（疑问收尾的高危信号）
    has_colon: bool             # 含冒号（"主体：细节"结构）
    has_emotion: bool           # 含情绪词
    has_conflict: bool          # 含冲突词
    has_top_name: bool          # 含顶流人名/豪门
    is_bookish: bool            # 书面语倾向
    anchor_count: int           # 过程锚点命中数
    anchor_kinds: tuple[str, ...]   # 命中的锚点类别（去重）

    @property
    def has_anchor(self) -> bool:
        return self.anchor_count > 0

    def to_row(self) -> dict:
        d = asdict(self)
        d["anchor_kinds"] = list(self.anchor_kinds)
        d["has_anchor"] = self.has_anchor
        return d


def extract(title: str) -> TitleFeatures:
    """提取一条标题的特征。空标题返回全零，不抛异常（台账里可能有脏数据）。"""
    t = (title or "").strip()
    kinds: list[str] = []
    count = 0
    for kind, pat in PROCESS_ANCHOR_PATTERNS:
        if re.search(pat, t):
            count += 1
            if kind not in kinds:
                kinds.append(kind)
    return TitleFeatures(
        han_len=len(_RE_HAN.findall(t)),
        has_number=bool(_RE_NUM.search(t)),
        has_exclaim=bool(_RE_EXCL.search(t)),
        has_question=bool(_RE_QUES.search(t)),
        has_colon=bool(_RE_COLON.search(t)),
        has_emotion=bool(EMOTION_PATTERN.search(t)),
        has_conflict=bool(_CONFLICT_PATTERN.search(t)),
        has_top_name=bool(TOP_NAME_PATTERN.search(t)),
        is_bookish=bool(BOOKISH_PATTERN.search(t)),
        anchor_count=count,
        anchor_kinds=tuple(kinds),
    )


def bucket(title: str) -> str:
    """把标题归入对标的四类组合，用于报表分档。

    命名刻意对应 2026-11-01 报告里的表，避免"表格一个说法、代码一个说法"。
    """
    f = extract(title)
    if f.has_anchor and f.has_exclaim:
        return "锚点+感叹"          # 头部最优档（预期）
    if f.has_anchor:
        return "仅锚点"
    if f.has_number and f.has_exclaim:
        return "数字+感叹"          # 头部实测中位 1354
    if f.has_number and f.has_emotion:
        return "数字+情绪"          # 头部实测中位 1124
    if f.has_exclaim:
        return "感叹无数字"          # 头部实测中位 236
    if f.has_number:
        return "数字无感叹无情绪"      # 头部实测中位 172
    if f.is_bookish:
        return "书面语"              # 头部实测中位 26
    return "三者都没有"               # 头部实测中位 76（我方中位 4）


BUCKET_ORDER = (
    "锚点+感叹", "仅锚点", "数字+感叹", "数字+情绪",
    "感叹无数字", "数字无感叹无情绪", "书面语", "三者都没有",
)


def summarize(items: list[dict], *, title_key: str = "title", value_key: str = "views") -> dict:
    """对一组作品做特征汇总。供对标报表与标题回流共用。

    items: [{"title": str, "views": int(可缺)}, ...]
    返回 {"n": 有效条数, "buckets": {档位: {n, median, mean, over1000_rate}},
          "features": {特征名: {n, median, mean}}, "anchor_coverage": 比例}
    样本 <5 的档位照样输出但标记 `thin=True`——宁可显示样本不足，也不隐藏。
    """
    import statistics as st

    valid = [x for x in items if (x.get(title_key) or "").strip()]
    out: dict = {"n": len(valid), "buckets": {}, "features": {},
                 "overall": {}, "anchor_coverage": None, "question_rate": None}
    if not valid:
        return out

    def stat(vals: list[int], thin_threshold: int = 5) -> dict:
        return {
            "n": len(vals),
            "median": st.median(vals) if vals else None,
            "mean": round(st.mean(vals), 1) if vals else None,
            "over1000_rate": (round(100 * sum(1 for v in vals if v >= 1000) / len(vals), 1)
                              if vals else None),
            "thin": len(vals) < thin_threshold,
        }

    all_vals = [int(x[value_key]) for x in valid if isinstance(x.get(value_key), (int, float))]
    out["overall"] = stat(all_vals, thin_threshold=1)
    q = sum(1 for x in valid if extract(x[title_key]).has_question)
    out["question_rate"] = round(100 * q / len(valid), 1)

    by_bucket: dict[str, list[int]] = {}
    anchors = 0
    for it in valid:
        b = bucket(it[title_key])
        v = it.get(value_key)
        if isinstance(v, (int, float)):
            by_bucket.setdefault(b, []).append(int(v))
        if extract(it[title_key]).has_anchor:
            anchors += 1
    for b in BUCKET_ORDER:
        if b in by_bucket:
            out["buckets"][b] = stat(by_bucket[b])

    # 逐维命中/未命中对比（同一份样本内部比较，不做跨账号套用）
    dims = {
        "has_number": lambda f: f.has_number,
        "has_exclaim": lambda f: f.has_exclaim,
        "has_question": lambda f: f.has_question,
        "has_anchor": lambda f: f.has_anchor,
        "is_bookish": lambda f: f.is_bookish,
        "has_top_name": lambda f: f.has_top_name,
    }
    for name, fn in dims.items():
        hit, miss = [], []
        for it in valid:
            v = it.get(value_key)
            if not isinstance(v, (int, float)):
                continue
            (hit if fn(extract(it[title_key])) else miss).append(int(v))
        if hit or miss:
            h, m = stat(hit), stat(miss)
            h["miss_median"] = m["median"]
            h["lift"] = (round(h["median"] / m["median"], 2)
                         if h["median"] and m["median"] else None)
            out["features"][name] = h

    out["anchor_coverage"] = round(100 * anchors / len(valid), 1)
    return out


# ------------------------------------------------------------------ 自检
def selftest() -> list[tuple[str, bool]]:
    """回归用例全部来自真实抓取数据，改词表必须同步改这里。"""
    cases = [
        # (标题, 期望命中锚点, 期望档位)
        ("让3追4！拜仁史诗级大逆转，30分钟连轰4球，凯恩替补完成绝杀！", True, "锚点+感叹"),
        ("第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分，曼城看到希望！", True, "锚点+感叹"),
        ("主场0-3大败，利物浦彻底崩盘！近7轮输了6场，球迷第80分钟退场", True, "锚点+感叹"),
        # 反例：4.2 万阅读的爆款，但**没有**过程锚点（"11战仅2胜"是战绩不是过程）。
        # 保留在此防止以后把锚点当成硬必要条件——锚点是加分项，不是门票。
        ("主场0-3大败！意甲劲旅彻底拉胯，11战仅2胜，头号球星带头反水！", False, "数字+感叹"),
        ("红黑军团降服蓝黑军团：莫雷拉闪耀圣西罗，国米八战德比难求一胜", False, "书面语"),
        ("德国杯：拜仁6-1大胜马格德堡，多点开花挺进1/8决赛", False, "数字无感叹无情绪"),
        # 反例：只有 4 阅读，却被旧词表误判为锚点，故「终结…荒」已移出锚点
        ("2026金球奖揭晓：凯恩凭73球夺魁，终结英格兰25年奖项荒", False, None),
        # 我方真实标题：全部没有过程锚点，全部疑问收尾
        ("米兰德比谁先崩？国米22分压着米兰，两位主帅的底牌竟然差在这？", False, None),
        ("当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？", False, None),
        ("马拉多纳的上帝之手，英格兰到底冤不冤呢", False, "三者都没有"),

        # ---- 2026-11-03 补的四类缺口（正例全部来自 11-02/11-03 真实成稿，不是编的）
        # 中文数字 + 动词形态：旧词表只认 `9人`，把「九人苦撑」漏了
        ("德尔加多挥拳染红致歉，大连九人苦撑3-4，绝无冒犯更刺耳！", True, "锚点+感叹"),
        # 动作词 + 裸时长：旧词表只认「第N分钟」
        ("费耶诺德主帅场边站满90分钟，国际米兰还没换人他先动了三次", True, "仅锚点"),
        # 换人负向/时机：旧「换人见效」只收正面结果词
        ("半场零封后崩盘，纽卡换人失误葬送好局，埃弗顿一箭封喉", True, "仅锚点"),
        # 赛场外过程：13 类里原本没有归属
        ("巴萨更衣室还在笑，维拉队长赛前加练到凌晨憋的到底是什么", True, "仅锚点"),

        # ---- 反例：放宽词表最容易误伤的四种写法，必须钉死
        # 「九人轮换」是正常轮换，不是少打一人（中文数字必须跟谓词）
        ("转会窗最后一天九人轮换，曼联中场大洗牌", False, None),
        # 裸「满90分钟」只是时长描述，没有动作词
        ("满90分钟的比赛强度，他早就习惯了", False, None),
        # 「不熬夜真亏」是劝读者别熬夜，不是球队的过程（初版收裸「熬夜」踩过这个坑）
        ("欧冠大戏巴萨曼联拜仁全来了，这四场不熬夜真亏", False, None),
        # 「凌晨」必须跟加练/训练/抵达/开会才算赛场外过程
        ("凌晨三点的机场，他一个人默默发了条动态", False, None),
    ]
    out = []
    for title, want_anchor, want_bucket in cases:
        f = extract(title)
        out.append((f"锚点判定：{title[:24]}", f.has_anchor == want_anchor))
        if want_bucket:
            out.append((f"档位判定：{title[:20]}→{want_bucket}", bucket(title) == want_bucket))
    # 空标题不炸
    out.append(("空标题不抛异常", extract("").han_len == 0))
    return out


def main() -> int:
    import sys
    bad = [(n, ok) for n, ok in selftest() if not ok]
    for n, ok in selftest():
        print(("  ✅ " if ok else "  ❌ ") + n)
    print(f"\n自检 {len(selftest())} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
