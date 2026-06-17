#!/usr/bin/env python3
"""足球自媒体 — 全局常量与配置

所有模块共享的常量、API key、URL、字典映射等。
"""

import os
from pathlib import Path

# --- Paths ---
PROJECT_ROOT = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
GZH_SCRIPT = str(PROJECT_ROOT / "skills" / "gzh-explosive-content-detector" / "scripts" / "fetch_gzh_trends.py")

# --- API keys from env (GitHub Secrets) ---
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DASHSCOPE_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"

# --- WxPusher ---
WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", "")
WXPUSHER_UID = os.environ.get("WXPUSHER_UID", "")

# --- Competition IDs (football-data.org v4) ---
COMPETITION_IDS = {
    "英超": 2021, "西甲": 2014, "意甲": 2019, "德甲": 2002, "法甲": 2015, "欧冠": 2001,
    "世界杯": 2000,
}

# --- GZH (公众号) keyword groups for trending detection ---
GZH_KEYWORD_GROUPS = [
    "足球",
    "世界杯,2026世界杯,世界杯揭幕战,世界杯小组赛",
    "英超,欧冠,转会",
    "梅西,C罗,姆巴佩,哈兰德,内马尔,萨拉赫",
    "足球,冲突,争议,红牌,绝杀,逆转",
    "转会,签约,续约,离队,绯闻,花边,冲突,下课",
    "世界杯,冷门,黑马,淘汰,出线",
]

GZH_TRANSFER_KEYWORDS = [
    "足球转会,重磅签约,天价转会",
    "梅西,C罗,姆巴佩,哈兰德,内马尔,转会绯闻",
    "足球,下课,换帅,新任主帅",
    "续约,离队,解约金,免签",
    "球员花边,场外新闻,女友,冲突",
    "足球八卦,转会流言,传闻",
]

GZH_NOISE_PATTERNS = [
    # Game/virtual football (not real matches)
    "三角洲", "实况足球", "FIFA", "足球经理", "FM", "梦幻足球",
    # Non-football sports
    "乒乓球", "樊振东", "孙颖莎", "王楚钦", "马龙", "国乒",
    "辽篮", "郭艾伦", "赵继伟", "CBA", "男篮", "广东宏远", "华南虎",
    "和平精英", "王者荣耀", "英雄联盟", "LPL",
    # Non-sports
    "纳斯达克", "IPO", "股票", "基金", "利率",
    "GLM-", "AI模型", "大模型",
    # Chinese football drama (not match/tournament analysis)
    "董路", "宋凯",
    # Geopolitics/news (not football)
    "伊朗方面", "伊朗宣布",
    # Marketing/PR analysis (not football content)
    "品牌营销", "营销妙手", "营销格局",
]

# --- Wikipedia / Footyrenders entity mappings ---
WIKI_PLAYERS = {
    "姆巴佩": "Kylian_Mbappé", "梅西": "Lionel_Messi", "C罗": "Cristiano_Ronaldo",
    "c罗": "Cristiano_Ronaldo", "哈兰德": "Erling_Haaland", "内马尔": "Neymar",
    "萨拉赫": "Mohamed_Salah", "德布劳内": "Kevin_De_Bruyne",
    "贝林厄姆": "Jude_Bellingham", "维尼修斯": "Vinícius_Júnior",
    "孙兴慜": "Son_Heung-min", "凯恩": "Harry_Kane",
    "莱万": "Robert_Lewandowski", "莫德里奇": "Luka_Modrić",
    "帕尔默": "Cole_Palmer", "福登": "Phil_Foden", "亚马尔": "Lamine_Yamal", "穆夏拉": "Jamal_Musiala",
}

WIKI_TEAMS = {
    "阿森纳": "Arsenal_F.C.", "曼城": "Manchester_City_F.C.", "利物浦": "Liverpool_F.C.",
    "曼联": "Manchester_United_F.C.", "切尔西": "Chelsea_F.C.", "热刺": "Tottenham_Hotspur_F.C.",
    "巴萨": "FC_Barcelona", "皇马": "Real_Madrid_CF", "马竞": "Atlético_Madrid",
    "拜仁": "FC_Bayern_Munich", "多特": "Borussia_Dortmund", "国米": "Inter_Milan",
    "AC米兰": "AC_Milan", "尤文": "Juventus_FC", "巴黎": "Paris_Saint-Germain_F.C.",
}

FOOTYRENDERS_PLAYERS = {
    "messi": "lionel-messi", "ronaldo": "cristiano-ronaldo", "mbappe": "kylian-mbappe",
    "haaland": "erling-braut-haaland", "neymar": "neymar-jr", "salah": "mohamed-salah",
    "debruyne": "kevin-de-bruyne", "bellingham": "jude-bellingham",
    "vinicius": "vinicius-junior", "vini": "vinicius-junior",
}

# --- Batch content type assignments (Deprecated v1, kept for test compat) ---
BATCH_TYPES = {
    "morning": ["热点球评", "八卦趣事"],
    "noon": ["转会资讯", "排行榜"],
    "evening": ["战术解析", "八卦趣事"],
}

# --- Batch Column Configuration (v2: Column System) ---
# Each batch has 2 unique "columns" (栏目). A column defines the complete
# reader-facing identity: topic domain, writing format, tone, word count,
# and interaction pattern. Six columns, zero overlap across all batches.
#
# DATA SOURCE HINT per column:
#   "match_preferred" = try match data first, fall back to GZH
#   "gzh_preferred"   = try GZH first, enrich with match context if available
#   "gzh_only"         = always use GZH pool regardless of match availability

BATCH_CONFIG = {
    "morning": {
        "name": "晨读",
        "time": "08:00",
        "reader_scenario": "通勤/早咖啡，需要快速了解发生了什么",
        "overall_tone": "轻快、信息密度高、适合碎片化阅读",
        "slots": [
            {
                "slot": 0,
                "column_id": "chen-du-kuai-xun",
                "column_name": "晨读快讯",
                "icon": "📰",
                "topic_domain": "足球快讯",
                "topic_guidance": "3-5条最新足球短消息加一句话辣评。可以是一条比赛结果+一条转会动态+一条花边/争议新闻。像微信群里的足球老哥早上发的第一条消息。每条独立成块，短句快节奏。",
                "writing_style": "群聊播报体",
                "style_detail": "每条消息3-5句，格式为【事件概述】+一句话老六辣评。短句、快节奏、有信息量但不啰嗦。像一个人肉RSS但有态度。禁止长篇大论，每条独立成块。",
                "word_count": [300, 500],
                "interaction_type": "prediction_poll",
                "interaction_guidance": "文末放一个关于文中某条消息的预测性投票（比如'你觉得XX这笔转会能成吗？'），选项A/B，自己先给判断。",
                "data_source_hint": "match_preferred",
            },
            {
                "slot": 1,
                "column_id": "hua-ti-lei-tai",
                "column_name": "话题擂台",
                "icon": "🥊",
                "topic_domain": "足球争议话题",
                "topic_guidance": "一个有争议的足球话题，列出正反两方观点，最后给出老六的站队并说明理由。话题要能让读者看完后立刻想去评论区表态。适合的话题：'XX该不该下课''XX转会到底值不值''VAR到底该不该废除'等。",
                "writing_style": "烧烤摊辩论体",
                "style_detail": "先摆出争议话题（一句话定调）→ 正方观点（2-3个论据）→ 反方观点（2-3个论据）→ 老六站队（亮明态度+核心论据）→ 邀请读者站队。语气像烧烤摊上和朋友抬杠，可以激动但不说脏话。必须给双方都有说话的机会，但最后必须有你自己的态度。",
                "word_count": [400, 600],
                "interaction_type": "side_taking_vote",
                "interaction_guidance": "文末明确说'评论区站队——支持XX的扣1，反对的扣2，我先来：我站[1/2]，因为...'",
                "data_source_hint": "gzh_preferred",
            },
        ],
    },
    "noon": {
        "name": "午间",
        "time": "12:00",
        "reader_scenario": "午休刷手机，需要快速了解上午发生了什么",
        "overall_tone": "信息密度高、快节奏、适合午休碎片阅读",
        "slots": [
            {
                "slot": 0,
                "column_id": "wu-jian-kuai-xun",
                "column_name": "午间快讯",
                "icon": "⚡",
                "topic_domain": "足球快讯",
                "topic_guidance": "上午最新足球消息精选：比赛结果、转会动态、突发事件。用短句快节奏呈现，每条独立成块，每条包含事件概述+一句话老六辣评。像中午在食堂打开手机扫一眼就知道上午发生了什么。",
                "writing_style": "群聊播报体",
                "style_detail": "3-5条消息，每条3-5句。格式：【事件概述】+ 一句话老六辣评。短句、快节奏、有信息量但不啰嗦。每条独立成块。",
                "word_count": [300, 500],
                "interaction_type": "prediction_poll",
                "interaction_guidance": "文末放一个投票：'今天上午最劲爆的消息是？A.XX B.XX C.XX，我选A，你呢？'",
                "data_source_hint": "match_preferred",
            },
            {
                "slot": 1,
                "column_id": "re-dian-su-ping",
                "column_name": "热点速评",
                "icon": "🔥",
                "topic_domain": "足球热点评论",
                "topic_guidance": "从上午的比赛/新闻中选一个最值得聊的话题，快速给出观点和点评。不是深度分析，是'看完比赛后和朋友说的第一句话'。可以是一场精彩比赛的快速复盘，也可以是一个转会传闻的犀利点评。",
                "writing_style": "赛后快刀体",
                "style_detail": "直接砸观点不铺垫 → 2-3个论据支撑 → 亮明立场。像从沙发上跳起来说的第一句话，锋利不留余地。500字以内，短平快。",
                "word_count": [400, 600],
                "interaction_type": "side_taking_vote",
                "interaction_guidance": "文末说：'觉得我说得对的扣1，觉得我太极端的扣2，我先来：我扣1，因为...'",
                "data_source_hint": "match_preferred",
            },
        ],
    },
    "evening": {
        "name": "晚间",
        "time": "17:30",
        "reader_scenario": "下班通勤/晚饭后刷手机，需要爽感和谈资",
        "overall_tone": "有观点、有情绪、适合截图转发和评论区站队",
        "slots": [
            # Slots are dynamically selected from EVENING_COLUMN_POOL at runtime.
            # Defaults below act as fallback if dynamic selection fails.
            {
                "slot": 0,
                "column_id": "world-cup-daily",
                "column_name": "世界杯日报",
                "icon": "📰",
                "topic_domain": "世界杯日报",
                "topic_guidance": "当天世界杯比赛的高光时刻、冷门结果、名场面汇总。像赛后集锦的文字版，给下班没看球的读者补课。覆盖当天世界杯关键节点，每场2-3句话抓重点，开头一句话总结今天的'主旋律'。",
                "writing_style": "快讯集锦体",
                "style_detail": "像在看赛后集锦的文字版。按比赛分块，每块2-3句话+一个记忆点。快节奏有信息量有观点。开头一句话总结今天世界杯的主旋律。非世界杯期间可改为当日俱乐部比赛集锦。",
                "word_count": [400, 600],
                "interaction_type": "prediction_poll",
                "interaction_guidance": "文末预测明天比赛：'明天XX对XX，你觉得谁能赢？评论区下注，我先来——...'",
                "data_source_hint": "gzh_only",
            },
            {
                "slot": 1,
                "column_id": "laoliu-hot-take",
                "column_name": "老六辣评",
                "icon": "🔥",
                "topic_domain": "足球热点辣评",
                "topic_guidance": "当天最热的一个足球话题，给出老六的犀利观点。可以怼人、可以拆台、可以力排众议。但要基于事实，怼得有道理。适合：某队的迷之操作、某球员的争议表现、某教练的神奇换人、某媒体的双标报道。",
                "writing_style": "脱口秀吐槽体",
                "style_detail": "像足球吐槽大会的单人版。开篇直接开火，用事实当子弹。可以有情绪但不能只有情绪——每句吐槽后面跟一句事实依据。节奏像脱口秀，有铺垫有爆点。结尾让人想截图转发。",
                "word_count": [400, 600],
                "interaction_type": "side_taking_vote",
                "interaction_guidance": "文末站队：'同意我的扣1，觉得我在瞎说的扣2，评论区见——别光扣数字，带理由来辩。'",
                "data_source_hint": "gzh_only",
            },
        ],
    },
}

# --- Evening Column Pool (晚间栏目池) ---
# The evening batch dynamically picks 2 columns from this pool based on daily
# GZH trending data. An LLM call scores each column against todayʼs hot topics
# and selects the two with the richest source material.
EVENING_COLUMN_POOL = [
    {
        "slot": -1,  # assigned at runtime
        "column_id": "world-cup-daily",
        "column_name": "世界杯日报",
        "icon": "📰",
        "topic_domain": "世界杯日报",
        "topic_guidance": "当天世界杯比赛的高光时刻、冷门结果、名场面汇总。像赛后集锦的文字版，给下班没看球的读者补课。覆盖当天世界杯关键节点，每场2-3句话抓重点，开头一句话总结今天的'主旋律'。非世界杯期间可改为当日俱乐部比赛集锦。",
        "writing_style": "快讯集锦体",
        "style_detail": "像在看赛后集锦的文字版。按比赛分块，每块2-3句话+一个记忆点。快节奏有信息量有观点。开头一句话总结今天的主旋律。",
        "word_count": [400, 600],
        "interaction_type": "prediction_poll",
        "interaction_guidance": "文末预测明天比赛：'明天XX对XX，你觉得谁能赢？评论区下注，我先来——...'",
        "data_source_hint": "gzh_only",
    },
    {
        "slot": -1,
        "column_id": "laoliu-hot-take",
        "column_name": "老六辣评",
        "icon": "🔥",
        "topic_domain": "足球热点辣评",
        "topic_guidance": "当天最热的一个足球话题，给出老六的犀利观点。可以怼人、可以拆台、可以力排众议。但要基于事实，怼得有道理。适合：某队的迷之操作、某球员的争议表现、某教练的神奇换人、某媒体的双标报道。",
        "writing_style": "脱口秀吐槽体",
        "style_detail": "像足球吐槽大会的单人版。开篇直接开火，用事实当子弹。可以有情绪但不能只有情绪——每句吐槽后面跟一句事实依据。节奏像脱口秀，有铺垫有爆点。结尾让人想截图转发。",
        "word_count": [400, 600],
        "interaction_type": "side_taking_vote",
        "interaction_guidance": "文末站队：'同意我的扣1，觉得我在瞎说的扣2，评论区见——别光扣数字，带理由来辩。'",
        "data_source_hint": "gzh_only",
    },
    {
        "slot": -1,
        "column_id": "var-debate",
        "column_name": "争议裁判室",
        "icon": "🟥",
        "topic_domain": "裁判争议与规则讨论",
        "topic_guidance": "近期引发争议的裁判判罚、VAR介入、红黄牌决策。分析判罚对错、规则依据、对比赛结果的影响。要有视频回放式的细节描述，让读者仿佛看了慢动作。可以对比不同联赛的判罚尺度，让读者有参与感。",
        "writing_style": "慢镜回放体",
        "style_detail": "像在VAR房间里看回放。先描述争议画面（细节到动作、角度、接触点），然后分析规则依据，最后给出判断。可以有立场但必须讲清规则逻辑。结尾问读者：'如果你是裁判，你怎么判？'",
        "word_count": [400, 600],
        "interaction_type": "side_taking_vote",
        "interaction_guidance": "文末投票：'你觉得这是点球吗？是扣1，不是扣2，我先来——我站[X]，因为...'",
        "data_source_hint": "gzh_only",
    },
    {
        "slot": -1,
        "column_id": "transfer-radar",
        "column_name": "转会雷达",
        "icon": "📡",
        "topic_domain": "转会传闻与球员身价",
        "topic_guidance": "近期最热的转会绯闻、球员身价变动、豪门引援目标。不只说'谁要转会'，更要分析转会背后的逻辑：球队为什么需要他、他能带来什么、转会费是否合理、对各方的影响。区分消息级别：官宣 > 权威记者 > 传闻。",
        "writing_style": "内幕分析体",
        "style_detail": "像球队经理在评估一笔交易。每条转会传闻按'消息来源 → 球员分析 → 球队需求 → 转会可能性 → 影响评估'的结构展开。用'据[来源]''按这个逻辑''如果成了的话'区分消息级别。不确定的就说不知道，不硬装大明白。",
        "word_count": [400, 600],
        "interaction_type": "prediction_poll",
        "interaction_guidance": "文末预测：'你觉得XX这笔转会能成吗？能扣1，不能扣2，评论区说说你的理由。'",
        "data_source_hint": "gzh_only",
    },
    {
        "slot": -1,
        "column_id": "fan-life",
        "column_name": "球迷众生相",
        "icon": "🎭",
        "topic_domain": "球迷文化与场外趣事",
        "topic_guidance": "世界杯期间的球迷反应、社交媒体神评论、看台趣事、球员场外生活。选题要有'人味'——让人笑、让人感动、让人有共鸣。适合：球迷的神回复合集、看台上的感人瞬间、球员和球迷的互动、世界杯带来的趣事。",
        "writing_style": "人间观察体",
        "style_detail": "像在球场边观察人间百态。用细节和画面说话，少评论多展示。可以有幽默感但不能嘲笑球迷的真情实感。每个故事要有画面感，让读者觉得'我也在现场就好了'。",
        "word_count": [400, 600],
        "interaction_type": "share_your_story",
        "interaction_guidance": "文末邀请分享：'你在现场看过最难忘的一场比赛是什么？评论区说说，看看谁的回忆最绝。'",
        "data_source_hint": "gzh_only",
    },
]

# Map new column names to legacy content types for metadata compatibility
CONTENT_TYPE_TO_COLUMN = {
    "晨读快讯": "热点球评",
    "话题擂台": "八卦趣事",
    "数据盘点": "排行榜",
    "深水区": "战术解析",
    # Evening pool (动态选择)
    "世界杯日报": "热点球评",
    "老六辣评": "八卦趣事",
    "争议裁判室": "八卦趣事",
    "转会雷达": "转会资讯",
    "球迷众生相": "八卦趣事",
}

# --- Data availability fallback ---
FALLBACK_MAP = {
    "热点球评": "战术解析",
    "排行榜": "八卦趣事",
}

# --- All content types ---
ALL_CONTENT_TYPES = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]

# --- Weekly Column Rotation (周一=0, 周日=6) ---
# Each day has a column theme that layers on top of one article in the batch
WEEKLY_COLUMNS = {
    0: {"slug": "du-she-bang", "name": "毒舌榜", "icon": "🔪",
        "description": "带排名的犀利点评，不是Excel是态度",
        "best_with": ["排行榜", "热点球评"],
        "style": "排名体：每个条目3-5句话，毒舌但不刻薄，用对比制造笑点，最后一句必须是让人忍不住截图的吐槽"},
    1: {"slug": "zhan-shu-hei-ban", "name": "战术黑板", "icon": "📋",
        "description": "把复杂战术翻译成球迷能吹牛的大白话",
        "best_with": ["战术解析"],
        "style": "教书体：先抛一个反常识的战术发现，然后用生活类比解释（'就像打游戏选错装备一样'），最后给一个能记住的结论"},
    2: {"slug": "zhuan-hui-cha-shui-jian", "name": "转会茶水间", "icon": "☕",
        "description": "转会传闻的八卦解读，不只说发生了什么，要说这意味着什么",
        "best_with": ["转会资讯", "八卦趣事"],
        "style": "吃瓜体：像和同事在茶水间聊八卦，有消息来源但不说教，用'据说''按这趋势''老六推测'区分消息级别，结尾必带一句损人的调侃"},
    3: {"slug": "hui-yi-sha", "name": "老六回忆杀", "icon": "📼",
        "description": "勾起球迷共同记忆的怀旧故事",
        "best_with": ["八卦趣事", "热点球评"],
        "style": "故事体：从一个具体的画面或瞬间切入（'我还记得那天穿着谁的球衣'），用细节勾回忆，以怀旧但不煽情的语气收尾，让读者在评论区晒自己的记忆"},
    4: {"slug": "zhou-mo-yu-re", "name": "周末预热", "icon": "🔥",
        "description": "本周末最值得关注的比赛，制造期待感",
        "best_with": ["热点球评", "战术解析"],
        "style": "预告体：像在群里约球友看球，列出'为什么这场必看'的3个理由（必须有一个跟数据无关、跟情绪有关的理由），结尾号召评论区晒看球计划"},
    5: {"slug": "sai-hou-kuai-dao", "name": "赛后快刀", "icon": "⚡",
        "description": "比赛结束后的第一时间犀利点评",
        "best_with": ["热点球评", "八卦趣事"],
        "style": "快刀体：开头直击最刺激的30秒画面，不铺垫不废话，观点锋利不留余地，像刚看完球从沙发上跳起来说的第一句话"},
    6: {"slug": "zhou-mo-fu-pan", "name": "周末复盘", "icon": "🔍",
        "description": "周末比赛的整体回顾和趋势洞察",
        "best_with": ["战术解析", "排行榜"],
        "style": "复盘体：从一个被大多数人忽略的数据或画面切入，串联周末多场比赛提炼一个共同趋势，让读者感觉'这个角度我怎么没想到'"},
}
