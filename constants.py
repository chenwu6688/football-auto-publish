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
}

# --- GZH (公众号) keyword groups for trending detection ---
GZH_KEYWORD_GROUPS = [
    "足球",
    "英超,欧冠,转会",
    "梅西,C罗,姆巴佩,哈兰德,内马尔,萨拉赫",
    "足球,冲突,争议,红牌,绝杀,逆转",
    "转会,签约,续约,离队,绯闻,花边,冲突,下课",
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

# --- Batch content type assignments ---
BATCH_TYPES = {
    "morning": ["热点球评", "八卦趣事"],
    "noon": ["转会资讯", "排行榜"],
    "evening": ["战术解析", "八卦趣事"],
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
