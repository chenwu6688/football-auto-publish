# -*- coding: utf-8 -*-
"""用我们自己的后台真实阅读量，验证同一组标题特征是否成立。"""
import json, re, statistics as st, glob
from pathlib import Path
ROOT = Path('/home/chenwu/WorkBuddy/2026-10-09-15-21-09/football-auto-publish')
P = json.loads((ROOT/'output/performance_log.json').read_text())['articles']
meta = {}
for d in sorted(glob.glob(str(ROOT/'output/2026-10-*'))):
    m = json.loads((Path(d)/'metadata.json').read_text())
    for a in m.get('articles') or []:
        meta[(m['date'], a.get('slug'))] = a
rows = []
for k, v in P.items():
    d = v.get('date','')
    if not d.startswith('2026-10'): continue
    a = meta.get((d, v.get('slug')), {})
    rows.append(dict(title=v.get('title') or a.get('title') or '', reads=v.get('reads') or 0,
                     ct=a.get('content_type'), date=d))
print('有绩效的 10 月成稿:', len(rows))
def f_num(t): return bool(re.search(r'\d', t))
def f_excl(t): return bool(re.search(r'[!！]', t))
def f_ques(t): return bool(re.search(r'[?？]', t))
def f_emotion(t): return bool(re.search(r'(大败|崩盘|反水|绝杀|逆转|丢球|拉胯|爆冷|崩|怒|惨|耻辱|羞愧|捂脸|王者|史诗|彻底|惊人|疯狂|复活)', t))
def f_book(t): return bool(re.search(r'(降服|闪耀|难求一胜|正名|荣膺|惜败|锁定|领衔|坐镇|出击|多点开花|挺进|力克)', t))
print()
print('=== 我们号（n=%d）=== ' % len(rows))
print('%-28s %-5s %-9s %s' % ('特征','n','中位阅读','最高'))
tests = [
 ('含数字', f_num), ('含感叹', f_excl), ('疑问收尾', f_ques),
 ('情绪词', f_emotion), ('书面语倾向', f_book),
 ('数字+感叹', lambda t: f_num(t) and f_excl(t)),
 ('数字+情绪', lambda t: f_num(t) and f_emotion(t)),
 ('无数字无感叹无情绪', lambda t: not f_num(t) and not f_excl(t) and not f_emotion(t)),
]
for name, fn in tests:
    hit = [r['reads'] for r in rows if fn(r['title'])]
    if not hit: continue
    print('%-28s %-5d %-9.0f %d' % (name, len(hit), st.median(hit), max(hit)))
print()
print('=== 我们的 TOP8 / BOTTOM8 ===')
for r in sorted(rows, key=lambda x:-x['reads'])[:8]: print('  %4d %-8s %s' % (r['reads'], r['ct'] or '-', r['title'][:46]))
print('  ---')
for r in sorted(rows, key=lambda x:x['reads'])[:8]: print('  %4d %-8s %s' % (r['reads'], r['ct'] or '-', r['title'][:46]))
