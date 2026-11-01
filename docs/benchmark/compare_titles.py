# -*- coding: utf-8 -*-
"""把我们的标题 vs 头条头部账号标题做结构对比（全部用真实抓取/台账数据）。"""
import json, re, glob, statistics as st, collections
from pathlib import Path
ROOT = Path('/home/chenwu/WorkBuddy/2026-10-09-15-21-09/football-auto-publish')

# ---------- 1. 我们的标题 ----------
ours = []
for d in sorted(glob.glob(str(ROOT/'output/2026-10-*'))):
    mp = Path(d)/'metadata.json'
    if not mp.exists(): continue
    m = json.loads(mp.read_text())
    for a in m.get('articles') or []:
        t = (a.get('title') or '').strip()
        if t: ours.append(dict(title=t, ct=a.get('content_type'), date=m.get('date')))

# ---------- 2. 头部账号标题（从 playwright 抓到的账号页文本 + 搜索页解析） ----------
raw = json.loads(Path('raw/pw_accounts.txt.json').read_text())
CHROME = {'搜索','综合','视频','资讯','小视频','图片','音乐','用户','微头条','APP内打开'}
head = []
for acc, txt in raw.items():
    lines = [l.strip() for l in txt.split('\n') if l.strip()]
    # 账号页结构：标题行 -> 时间行 -> N赞 -> N人看过（或反过来）
    for i, l in enumerate(lines):
        if re.fullmatch(r'\d+人看过', l) or re.fullmatch(r'\d+万?人看过', l):
            # 往回找标题：跳过 N赞 / 时间
            j = i
            while j > 0:
                j -= 1
                p = lines[j]
                if re.fullmatch(r'\d+赞', p) or re.fullmatch(r'(\d+天前|\d+小时前|\d+分钟前|昨天.*|前天.*|\d+月\d+日)', p):
                    continue
                if p in CHROME or re.fullmatch(r'[\d.]+万?粉丝', p): break
                if len(p) >= 8:
                    v = int(re.sub(r'[^\d.]', '', l).replace('万','0000').split('.')[0]) if '万' not in l else int(float(re.sub(r'[^\d.]','',l))*10000)
                    head.append(dict(account=acc, title=p, views=v))
                break
    # 也抓「纯文字首行」形态（微头条）
print('我们的标题:', len(ours))
print('头部账号标题:', len(head), '（来自', len(set(h["account"] for h in head)), '个账号）')
Path('raw/head_titles.json').write_text(json.dumps(head, ensure_ascii=False, indent=1), encoding='utf-8')

def feats(t):
    han = len(re.findall(r'[\u4e00-\u9fa5]', t))
    return dict(
        len=han,
        has_quote=1 if ('：' in t or ':' in t) else 0,
        has_num=1 if re.search(r'\d', t) else 0,
        has_ques=1 if ('?' in t or '？' in t) else 0,
        has_excl=1 if ('!' in t or '！' in t) else 0,
        has_bang=1 if ('#' in t) else 0,
        emo=1 if re.search(r'(哭|崩|炸|怒|骂|尴尬|无奈|惨|爆冷|震惊|泪|酸了|破防|离谱|哑火|血洗|绝杀|屠杀|翻车|打脸|服了|不服|发威|打崩)', t) else 0,
        conflict=1 if re.search(r'(炮轰|回应|质疑|怒斥|反驳|开炮|喊话|讽刺|点名|互撕|举报|道歉|交涉|处罚)', t) else 0,
        person=1 if re.search(r'(凯恩|梅西|C罗|姆巴佩|亚马尔|哈兰德|萨拉赫|贝林厄姆|维尼修斯|孙颖莎)', t) else 0,
        colon_lead=1 if re.match(r'^[^：:]{2,10}[：:]', t) else 0,
    )

def report(name, items):
    F = [feats(x['title']) for x in items]
    print('\n=== %s (n=%d) ===' % (name, len(F)))
    print('标题汉字数 中位 %d / 均值 %.1f / 区间 %d-%d' % (
        st.median([f['len'] for f in F]), st.mean([f['len'] for f in F]),
        min(f['len'] for f in F), max(f['len'] for f in F)))
    for k, label in [('has_quote','含冒号引语'),('has_num','含数字'),('has_ques','疑问收尾'),
                     ('has_excl','感叹'),('emo','情绪词'),('conflict','冲突词'),
                     ('person','含顶流人名'),('colon_lead','冒号前置')]:
        print('  %-10s %.1f%%' % (label, 100*sum(f[k] for f in F)/len(F)))
    return F

report('我们（10月成稿）', ours)
report('头条头部账号', head)

# 分组：头部账号按粉丝规模
print('\n=== 头部账号逐号 ===')
g = collections.defaultdict(list)
for h in head: g[h['account']].append(h['views'])
for k, v in sorted(g.items(), key=lambda x: -st.mean(x[1])):
    print('%-10s n=%-2d 中位看过 %6d 均值 %8.0f 最高 %7d' % (k, len(v), st.median(v), st.mean(v), max(v)))

print('\n=== TOP 12 头部标题（按看过） ===')
for h in sorted(head, key=lambda x: -x['views'])[:12]:
    print('%8d  %-10s %s' % (h['views'], h['account'], h['title'][:52]))
