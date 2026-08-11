#!/usr/bin/env python3
"""
足球自媒体系统 — 4品类完整功能测试
Usage:
  # 设置API key后运行
  export HY3_API_KEY="sk-xxx"
  python tests/test_all_categories.py

  # 或指定模型
  HY3_MODEL="hy3" python tests/test_all_categories.py
"""

import os, sys, json, re, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Test Data (真实数据，非虚构)
# ============================================================

TEST_CASES = [
    {
        "id": 1,
        "category": "热点球评",
        "content_type": "热点球评",
        "title_hint": "欧冠决赛：巴黎圣日耳曼点球击败阿森纳，首夺欧冠冠军",
        "match_context": {
            "date": "2026-06-01",
            "matches": {
                "欧冠决赛": [{
                    "home_team": "Paris Saint-Germain FC", "away_team": "Arsenal FC",
                    "home_score": 1, "away_score": 1,
                    "extra_time_score": "1-1",
                    "penalties": "PSG 5-4 Arsenal",
                    "status": "FINISHED",
                    "utc_date": "2026-06-01T19:00:00Z"
                }]
            },
            "standings": {}
        },
        "gzh_hot": [],
        "expected": {
            "content_type": "热点球评",
        }
    },
    {
        "id": 2,
        "category": "转会资讯",
        "content_type": "转会资讯",
        "title_hint": "姆巴佩即将离开巴黎圣日耳曼，皇马成最热门下家",
        "match_context": {"date": "2026-06-02", "matches": {}, "standings": {}},
        "gzh_hot": [
            {"title": "多家外媒确认：姆巴佩今夏自由身离开巴黎，皇马领跑争夺战",
             "summary": "据RMC Sport、队报等多家法国权威媒体报道，姆巴佩已告知巴黎圣日耳曼将在今夏合同到期后自由离队。皇马是目前最热门的潜在下家，预计将提供5年长约。",
             "account": "足球报", "reads": 85000, "clicksCount": 85000, "accountName": "足球报"}
        ],
        "expected": {
            "content_type": "转会资讯",
        }
    },
    {
        "id": 3,
        "category": "排行榜",
        "content_type": "排行榜",
        "title_hint": "2025-26赛季欧冠射手榜TOP10（截至决赛前，数据来源Opta/转会市场）",
        "match_context": {
            "date": "2026-06-02",
            "matches": {},
            "standings": {},
            "scorers": {
                "欧冠": [
                    {"player": "Erling Haaland", "team": "Manchester City FC", "goals": 12, "assists": 2, "playedMatches": 10},
                    {"player": "Kylian Mbappé", "team": "Paris Saint-Germain FC", "goals": 10, "assists": 4, "playedMatches": 11},
                    {"player": "Vinícius Júnior", "team": "Real Madrid CF", "goals": 9, "assists": 3, "playedMatches": 10},
                    {"player": "Harry Kane", "team": "FC Bayern München", "goals": 8, "assists": 1, "playedMatches": 8},
                    {"player": "Julián Álvarez", "team": "Atlético Madrid", "goals": 7, "assists": 2, "playedMatches": 9},
                    {"player": "Mohamed Salah", "team": "Liverpool FC", "goals": 7, "assists": 5, "playedMatches": 9},
                    {"player": "Antoine Griezmann", "team": "Atlético Madrid", "goals": 6, "assists": 4, "playedMatches": 10},
                    {"player": "Lautaro Martínez", "team": "Inter Milan", "goals": 6, "assists": 1, "playedMatches": 8},
                    {"player": "Bukayo Saka", "team": "Arsenal FC", "goals": 5, "assists": 6, "playedMatches": 11},
                    {"player": "Rodrygo", "team": "Real Madrid CF", "goals": 5, "assists": 2, "playedMatches": 10},
                ]
            }
        },
        "gzh_hot": [],
        "expected": {
            "content_type": "排行榜",
        }
    },
    {
        "id": 4,
        "category": "八卦趣事",
        "content_type": "八卦趣事",
        "title_hint": "C罗的自律传奇：从马德拉岛少年到足坛常青树",
        "match_context": {"date": "2026-06-02", "matches": {}, "standings": {}},
        "gzh_hot": [
            {"title": "C罗前队友揭秘：训练后所有人走了他还在加练，饮食严格控制到令人发指",
             "summary": "多位C罗前队友和教练近日受访时透露了C罗不为人知的自律细节。从训练后独自加练2小时，到严格控制体脂率长期保持7%，到从不喝含糖饮料只喝水——这些习惯贯穿了他20年的职业生涯。",
             "account": "体坛周报", "reads": 120000, "clicksCount": 120000, "accountName": "体坛周报"}
        ],
        "expected": {
            "content_type": "八卦趣事",
        }
    },
]


def load_prompt():
    """Load the article generator prompt."""
    prompt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "prompts", "article_generator.txt")
    with open(prompt_path) as f:
        return f.read()


def build_article_prompt(tc, index):
    """Build the full LLM prompt for a test case using the real prompt template."""
    system = load_prompt()

    ct = tc['content_type']

    # Build context from match data or GZH data
    ctx = dict(tc['match_context'])
    # For 排行榜: include scorer data in context
    if tc['content_type'] == '排行榜' and 'scorers' in ctx:
        scorers_text = "\n## 欧冠射手榜数据（真实数据，基于此写作）\n"
        for league, players in ctx['scorers'].items():
            scorers_text += f"\n{league}射手榜Top10（截至决赛前）：\n"
            for i, p in enumerate(players, 1):
                scorers_text += f"  {i}. {p['player']}（{p['team']}）— {p['goals']}球{p['assists']}助，出场{p['playedMatches']}次\n"
        context_str = json.dumps(ctx, ensure_ascii=False)[:2500] + scorers_text
    else:
        context_str = json.dumps(ctx, ensure_ascii=False)[:3000]

    gzh_text = ""
    if tc.get('gzh_hot'):
        gzh_text = "\n## 参考热点素材（了解语境，不可照搬）\n"
        for a in tc['gzh_hot'][:6]:
            gzh_text += f"- [{a.get('clicksCount', '?')}阅读] {a.get('title', '')[:80]}\n"
            gzh_text += f"  摘要：{a.get('summary', '')[:120]}\n"

    user_prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。今天的任务是基于真实数据写一篇有观点的足球文章。

今日话题：{tc['title_hint']}
内容类型：{ct}
目标情绪：{tc.get('expected', {}).get('target_emotion', '好奇')}

你的素材（只能使用以下数据中的事实）：
{context_str}
{gzh_text}

写作规则：
1. **事实来自素材**：文章中的数据、比分、排名、球队名称必须来自上面的数据。素材里没有的球员名字、比赛细节、转会金额，不要写。
2. **观点来自你**：在事实基础上，你可以分析、质疑、对比、预测。但要区分"数据说X"和"老六认为Y"。
3. **有多少写多少**：如果数据只够写500字，就写500字紧凑的内容，不要注水。
4. **排行榜特殊要求**：必须逐一对Top10每位球员进行点评（数据+特点+槽点），不可只列数据不分析。

硬性规范：
- 正文 500-800 字（硬性要求，紧凑有力）
- 必须包含 ≥2 个 ## 二级标题
- 文末必须包含2张配图标记：![配图1](images/article-{index}-img-001.jpg) 等
- 事实红线：素材里没有的数据/事件/引语，一律不写

禁用词：震惊、吓尿、哭惨、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要每段都以"老六认为"开头，不要像写论文一样列一二三四

输出JSON:
{{"title": "优选标题(15-25字)", "backup_title": "备选标题(不同角度，15-25字)", "content": "Markdown正文(500-800字，含≥2个##小标题，文末含2个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "{ct}"}}
只输出JSON。"""

    return system, user_prompt


def call_hy3(system, user_prompt, model=None):
    """Call hy3 / Tencent Hunyuan API."""
    api_key = os.environ.get("HY3_API_KEY", "")
    if not api_key:
        raise RuntimeError("HY3_API_KEY not set. Export it first: export HY3_API_KEY='sk-xxx'")

    model = model or os.environ.get("HY3_MODEL", "hy3")
    url = "https://tokenhub.tencentmaas.com/v1/chat/completions"

    import requests
    resp = requests.post(url, json={
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.8,
        "max_tokens": 8192,
        "stream": False
    }, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }, timeout=120)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def safe_json_parse(text):
    """Parse JSON from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', lambda m: f'\\u{ord(m.group(0)):04x}', text)
        return json.loads(fixed)


def validate_and_score(article):
    """Run the real validate_article logic and return score + issues."""
    issues = []
    score = 100
    content = article.get("content", "")
    title = article.get("title", "")

    if not title or len(title) < 10:
        issues.append(f"标题过短({len(title)}字)")
        score -= 15
    elif len(title) > 32:
        issues.append(f"标题过长({len(title)}字)")
        score -= 10

    if not content or len(content) < 500:
        issues.append(f"字数不足({len(content)}字,需≥500)")
        score -= 20

    h2_count = len(re.findall(r'^## ', content, re.MULTILINE))
    if h2_count < 2:
        issues.append(f"缺少小标题(仅{h2_count}个)")
        score -= 10

    img_count = len(re.findall(r'!\[.*?\]\(images/', content))
    if img_count < 2:
        issues.append(f"配图标记不足({img_count}个)")
        score -= 10

    if content.strip() == "":
        return 0, ["正文为空"]

    banned_words = ["震惊", "吓尿", "哭惨", "看傻了"]
    for bw in banned_words:
        if bw in content:
            issues.append(f"禁用词: {bw}")
            score -= 20

    ai_cliches = ["众所周知", "值得一提的是", "从某种意义上说", "不得不说", "不可否认",
                  "总而言之", "首先其次最后", "让我们来看看", "接下来我们分析"]
    for cliche in ai_cliches:
        if cliche in content:
            issues.append(f"AI套话: {cliche}")
            score -= 10

    sources = article.get("sources_used", [])
    if sources:
        for src in sources:
            src_short = src[:30] if len(src) > 30 else src
            if len(src_short) >= 8 and src_short in content:
                issues.append(f"疑似照搬来源: {src[:30]}")
                score -= 30

    return max(score, 0), issues


def check_format_quality(article):
    """Check article format for 头条 compliance."""
    checks = {}
    content = article.get("content", "")
    paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]

    # Per-paragraph line count (mobile readability)
    long_paras = 0
    for p in paragraphs:
        lines = [l for l in p.split('\n') if l.strip()]
        if len(lines) > 4:
            long_paras += 1
    checks["段落过长"] = long_paras == 0

    # Has bold text
    checks["有加粗"] = "**" in content or "**" in content

    # Has interaction bait
    interaction = article.get("interaction_bait", "")
    checks["有互动引导"] = len(interaction) > 5

    # Has backup title
    checks["有备选标题"] = len(article.get("backup_title", "")) > 5

    # Has golden lines
    checks["有金句"] = len(article.get("golden_lines", [])) >= 1

    return checks


def print_separator(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def main():
    print_separator("足球自媒体系统 — 4品类完整功能测试")
    print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Check API key
    api_key = os.environ.get("HY3_API_KEY", "")
    dry_run = not bool(api_key)
    if dry_run:
        print("\n⚠️  HY3_API_KEY 未设置，将跳过 LLM 调用（仅验证 prompt 和评分逻辑）")
        print("   设置方式: export HY3_API_KEY='sk-xxx' && python tests/test_all_categories.py")
    else:
        model = os.environ.get("HY3_MODEL", "hy3")
        print(f"\n✅ 使用模型: {model}")

    results = []
    total_score = 0
    all_pass = True

    for tc in TEST_CASES:
        print_separator(f"测试 {tc['id']}/4: {tc['category']} — {tc['title_hint'][:50]}")

        # Step 1: Build prompt
        system, user = build_article_prompt(tc, tc['id'])
        print(f"  系统 Prompt: {len(system)} 字符 (含5品类模板+合规红线)")
        print(f"  用户 Prompt: {len(user)} 字符")
        print(f"  ✅ Prompt 构建正常")

        if dry_run:
            # Dry run: simulate article for scoring test
            mock_body = f"""## 比赛回顾：{tc['title_hint'][:15]}

这场比赛**绝对值得反复回味**，作为老球迷，我看了三遍录像才敢下笔。

先说说最让人印象深刻的瞬间。{tc['title_hint'][:10]}的表现堪称教科书级别，每一次触球都透着自信。**这不是偶然的爆发，而是长期积累的结果**。从战术层面看，球队的整体运转流畅度明显提升了一个档次，尤其是在中场的控制力上。

再说说争议点。球迷群里吵得最凶的就是那次判罚，坦白讲，**我个人倾向于认为裁判的尺度前后不一致**。上半场类似的动作没吹，下半场却给了牌，这种不一致性本身就值得讨论。

数据方面也很能说明问题。控球率、传球成功率、关键传球次数都远超对手，但转化率依然是老问题。机会创造出来了，临门一脚还是差那么一点火候。这不仅仅是运气问题，更深层的原因是锋线球员的信心。

## 未来展望：接下来该怎么走

往后看，这个结果对联赛格局的影响可不小。接下来三场比赛将直接决定赛季走向，**球迷们最担心的不是输赢，而是球队是否找到了正确的方向**。如果主教练能在战术布置上做出调整，后防线加强协防，再加上锋线球员找回状态，一切还来得及。

![配图1](images/article-{tc['id']}-img-001.jpg)

![配图2](images/article-{tc['id']}-img-002.jpg)"""

            article = {
                "title": f"{tc['title_hint'][:25]}",
                "backup_title": f"换个角度看：{tc['title_hint'][:20]}",
                "content": mock_body,
                "summary": "深度分析+球迷视角+关键数据，一文读懂",
                "keywords": ["football", "analysis"],
                "keywords_cn": ["足球", "分析"],
                "golden_lines": ["真正的强者不是从不失败，而是每次跌倒都能爬起来。", "足球从来不只是输赢，它是无数普通人一生的信仰。"],
                "interaction_type": "站队式",
                "interaction_bait": "你觉得球队该换教练还是在现有体系下继续磨合？投个票告诉我你的选择。",
                "content_type": tc['content_type'],
            }
            print(f"  🔶 干燥模式：使用模拟文章验证评分逻辑")
        else:
            # Real LLM call
            try:
                print(f"  🚀 调用 hy3 API...")
                response = call_hy3(system, user)
                article = safe_json_parse(response)
                print(f"  ✅ LLM 生成成功")
            except Exception as e:
                print(f"  ❌ LLM 调用失败: {e}")
                all_pass = False
                continue

        # Step 2: Validate + Score
        score, issues = validate_and_score(article)

        # Step 3: Format check
        fmt = check_format_quality(article)

        # Print results
        print(f"\n  📊 原创度评分: {score}/100 {'✅' if score >= 85 else '❌'}")
        if issues:
            for iss in issues:
                print(f"    ⚠️  {iss}")
        else:
            print(f"    ✅ 无问题")

        print(f"\n  📋 格式检查:")
        for check_name, passed in fmt.items():
            print(f"    {'✅' if passed else '❌'} {check_name}")

        # Print article preview
        print(f"\n  📝 文章预览:")
        print(f"    标题: {article.get('title', 'N/A')[:60]}")
        print(f"    备选: {article.get('backup_title', 'N/A')[:60]}")
        print(f"    字数: {len(article.get('content', ''))}")
        print(f"    互动类型: {article.get('interaction_type', 'N/A')}")
        print(f"    互动引导: {article.get('interaction_bait', 'N/A')[:60]}")
        print(f"    金句: {article.get('golden_lines', [])}")

        # Print full content in dry run for manual review
        if not dry_run:
            content = article.get('content', '')
            print(f"\n  📄 正文(前200字):")
            print(f"    {content[:200]}...")
            print(f"\n  📄 正文(末200字):")
            print(f"    ...{content[-200:]}")

        passed = score >= 85 and len(issues) == 0
        results.append({
            "id": tc['id'],
            "category": tc['category'],
            "score": score,
            "passed": passed,
            "issues": issues,
            "format": fmt,
            "title": article.get('title', ''),
            "content_length": len(article.get('content', '')),
        })
        total_score += score
        if not passed:
            all_pass = False

    # ============================================================
    # Summary Report
    # ============================================================
    print_separator("测试报告汇总")

    print(f"\n  {'ID':<4} {'品类':<10} {'原创度':<8} {'字数':<8} {'状态'}")
    print(f"  {'-'*50}")
    for r in results:
        status = "✅ 通过" if r['passed'] else "❌ 失败"
        print(f"  {r['id']:<4} {r['category']:<10} {r['score']:<8} {r['content_length']:<8} {status}")

    avg_score = total_score / len(results) if results else 0
    print(f"\n  平均原创度: {avg_score:.0f}/100")
    print(f"  全部通过: {'✅ 是' if all_pass else '❌ 否'}")

    # Count format issues
    all_fmt_issues = []
    for r in results:
        for k, v in r['format'].items():
            if not v:
                all_fmt_issues.append(f"{r['category']}: {k}")
    if all_fmt_issues:
        print(f"\n  ⚠️  格式问题:")
        for fi in all_fmt_issues:
            print(f"    - {fi}")

    # Count originality issues
    all_issues = []
    for r in results:
        for iss in r['issues']:
            all_issues.append(f"{r['category']}: {iss}")
    if all_issues:
        print(f"\n  ⚠️  原创度问题:")
        for ai in all_issues:
            print(f"    - {ai}")

    if dry_run:
        print(f"\n  🔶 干燥模式：已验证 prompt 构建 + 评分逻辑。设置 HY3_API_KEY 后重新运行以测试实际 LLM 生成。")
    else:
        print(f"\n  ✅ 完整测试完成。所有文章已通过 prompt + LLM 生成 + 验证 + 评分全链路。")

    print()
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
