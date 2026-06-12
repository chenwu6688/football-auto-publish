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
    "三角洲", "实况足球", "FIFA", "足球经理", "FM", "梦幻足球",
    "乒乓球", "樊振东", "孙颖莎", "王楚钦", "马龙", "国乒",
    "和平精英", "王者荣耀", "英雄联盟", "LPL",
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
        "reader_scenario": "午休刷手机，需要可以聊的谈资",
        "overall_tone": "有干货、有观点、适合社交分享",
        "slots": [
            {
                "slot": 0,
                "column_id": "shu-ju-pan-dian",
                "column_name": "数据盘点",
                "icon": "📊",
                "topic_domain": "足球数据排名",
                "topic_guidance": "一个足球相关的排行榜或数据盘点（射手榜、助攻榜、身价榜、跑动距离榜、犯规榜...任何有意思的排名）。每个上榜条目：数据+人话翻译+一句毒舌点评。榜单要有反差感或争议性，不是无聊的数据罗列。",
                "writing_style": "毒舌点评体",
                "style_detail": "排名是有态度的，不是Excel。每个条目3-5句话：排名+核心数据→人话翻译→毒舌点评。点评可以损但不能恶毒，用对比制造笑点。最后一句必须是让人忍不住截图的吐槽。",
                "word_count": [400, 600],
                "interaction_type": "challenge_dare",
                "interaction_guidance": "文末挑战读者：'觉得我排的不对？评论区带数据来辩，说不出来的默认你同意我。'或者'你觉得还有谁该上榜？评论区提名，下期安排。'",
                "data_source_hint": "match_preferred",
            },
            {
                "slot": 1,
                "column_id": "shen-shui-qu",
                "column_name": "深水区",
                "icon": "🌊",
                "topic_domain": "足球深度分析",
                "topic_guidance": "一个值得深入挖掘的足球话题：交易背后的博弈、战术趋势分析、俱乐部管理内幕、联赛格局变化等。要有信息量和洞察力，让读者看完觉得'学到了'。可以是一件转会背后的多方博弈，一个战术趋势的详细解读，或者一个俱乐部管理决策的深层分析。",
                "writing_style": "内行看门道体",
                "style_detail": "像行业内人士在给你讲门道。开篇抛一个反常识或深层的洞察。然后用2-3个层次展开分析，每个层次用通俗语言解释专业概念。必须有一个'只说给你听'的独家感。禁止教科书腔，禁止'首先其次最后'。",
                "word_count": [500, 700],
                "interaction_type": "opinion_poll",
                "interaction_guidance": "文末抛出一个开放性问题，邀请读者发表看法。问题要有讨论空间（不是非黑即白），比如'你觉得XX这笔操作，三年后回头看，是神操作还是败笔？'",
                "data_source_hint": "gzh_preferred",
            },
        ],
    },
    "evening": {
        "name": "晚间",
        "time": "17:30",
        "reader_scenario": "下班通勤/晚饭后，需要情感共鸣",
        "overall_tone": "有温度、有故事、适合沉浸式阅读",
        "slots": [
            {
                "slot": 0,
                "column_id": "ren-wu-zhi",
                "column_name": "人物志",
                "icon": "👤",
                "topic_domain": "足球人物故事",
                "topic_guidance": "一个球员或教练的人物侧写。聚焦一个侧面、一个瞬间、一个选择——不是履历流水账。写他的高光、低谷、争议、选择。让读者看完后对这个人物产生新的理解或共情。适合写：老将的最后一舞、新星的崛起、争议人物的另一面、被遗忘的天才。",
                "writing_style": "人物杂志体",
                "style_detail": "像《人物》杂志的特稿，但更短更有网感。开篇必须是一个具体画面或瞬间。用故事和细节勾画人物，而非数据和荣誉列表。可以有情感但不能煽情。结尾让人回味，不是口号。",
                "word_count": [500, 800],
                "interaction_type": "resonance_sharing",
                "interaction_guidance": "文末引发共鸣：'你印象里关于XX最难忘的一个画面是什么？评论区分享，我先来——[一个具体画面]'。让读者想分享自己的记忆。",
                "data_source_hint": "gzh_only",
            },
            {
                "slot": 1,
                "column_id": "shi-guang-ji",
                "column_name": "时光机",
                "icon": "⏰",
                "topic_domain": "足球历史/记忆/球迷文化",
                "topic_guidance": "一段足球历史、经典时刻回顾、或球迷文化现象。可以是'XX年前的今天发生了什么'、一段让人怀念的足球时代、一个已经消失的足球传统。要有细节和画面感，让老球迷有共鸣，让新球迷觉得有趣。",
                "writing_style": "回忆杀体",
                "style_detail": "从一个具体的记忆触发点切入。用细节营造时代感，不煽情但有温度。可以穿插个人回忆视角。结尾让读者也想分享自己的故事。",
                "word_count": [500, 700],
                "interaction_type": "share_your_story",
                "interaction_guidance": "文末邀请读者分享：'你和足球最难忘的第一次是什么？第一次看球、第一次穿球衣、第一次为足球哭——评论区说说，看看有没有同一年入坑的。'",
                "data_source_hint": "gzh_only",
            },
        ],
    },
}

# Map new column names to legacy content types for metadata compatibility
CONTENT_TYPE_TO_COLUMN = {
    "晨读快讯": "热点球评",
    "话题擂台": "八卦趣事",
    "数据盘点": "排行榜",
    "深水区": "战术解析",
    "人物志": "八卦趣事",
    "时光机": "八卦趣事",
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
