# -*- coding: utf-8 -*-
import json, re, statistics as st, collections
from pathlib import Path
rows = json.loads(Path('raw/head_home_rows.json').read_text())

def f_num(t): return bool(re.search(r'\d', t))
def f_excl(t): return bool(re.search(r'[!！]', t))
def f_emotion(t): return bool(re.search(r'(大败|崩盘|反水|绝杀|逆转|丢球|拉胯|反水|爆冷|崩|怒|惨|耻辱|羞愧|捂脸|王者|史诗|彻底|惊人|疯狂|复活)', t))
def f_book(t): return bool(re.search(r'(降服|闪耀|难求一胜|正名|荣膺|惜败|锁定|领衔|坐镇|出击|多点开花|挺进|力克)', t))

print('=== 组合交叉（全样本 n=%d）===' % len(rows))
print('%-30s %-5s %-9s %s' % ('组合','n','中位阅读','过千率'))
combos = [
    ('数字+感叹', lambda t: f_num(t) and f_excl(t)),
    ('数字+情绪', lambda t: f_num(t) and f_emotion(t)),
    ('数字+感叹+情绪', lambda t: f_num(t) and f_excl(t) and f_emotion(t)),
    ('感叹但无数字', lambda t: f_excl(t) and not f_num(t)),
    ('数字但无感叹无情绪', lambda t: f_num(t) and not f_excl(t) and not f_emotion(t)),
    ('书面语（无论有无数字）', lambda t: f_book(t)),
    ('三者都没有', lambda t: not f_num(t) and not f_excl(t) and not f_emotion(t)),
]
for name, fn in combos:
    hit = [r['views'] for r in rows if fn(r['title'])]
    if not hit: continue
    print('%-30s %-5d %-9.0f %.0f%%' % (name, len(hit), st.median(hit), 100*sum(1 for x in hit if x>=1000)/len(hit)))

print('\n=== 单事件饱和覆盖：金球奖（2026-10-26/27）===')
for acc in sorted(set(r['account'] for r in rows)):
    sub = [r for r in rows if r['account']==acc and re.search(r'(金球|凯恩|亚马尔|贝纳蒂亚)', r['title'])]
    if len(sub) >= 2:
        print('  %-10s %d 条  阅读: %s' % (acc, len(sub), sorted([x['views'] for x in sub], reverse=True)))
        for r in sub: print('      %6d  %s' % (r['views'], r['title'][:52]))

print('\n=== 单日产出量 vs 中位阅读 ===')
byday = collections.defaultdict(list)
for r in rows:
    byday[(r['account'], r['when'][:6] if '前' in r['when'] else r['when'][:5])].append(r['views'])
for acc in sorted(set(r['account'] for r in rows)):
    sub = [r for r in rows if r['account']==acc]
    print('  %-10s 总 %d 条，中位 %d' % (acc, len(sub), st.median([x['views'] for x in sub])))
