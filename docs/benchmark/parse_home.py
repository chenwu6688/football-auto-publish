# -*- coding: utf-8 -*-
import json, re, pathlib
d = json.loads(pathlib.Path('raw/pw_home.json').read_text())

RE_ROW = re.compile(r'^([\d.]+)(万)?阅读(?:([\d.]+)(万)?评论)?(.+)$')
def num(s, w): return int(float(s) * (10000 if w else 1))

rows = []
for acc, txt in d.items():
    if not txt or txt.startswith(('ERR','CLICK')):
        continue
    lines = [l.strip() for l in txt.split('\n') if l.strip()]
    for i, l in enumerate(lines):
        m = RE_ROW.match(l)
        if not m: continue
        views = num(m.group(1), m.group(2))
        cmts = num(m.group(3), m.group(4)) if m.group(3) else 0
        when = m.group(5)
        title = lines[i-1] if i > 0 else ''
        if len(title) < 6 or RE_ROW.match(title): continue
        rows.append(dict(account=acc, title=title, views=views, comments=cmts, when=when))

seen, ded = set(), []
for r in rows:
    k = (r['account'], r['title'][:26])
    if k in seen: continue
    seen.add(k); ded.append(r)

print('解析出', len(rows), '条，去重', len(ded))
pathlib.Path('raw/head_home_rows.json').write_text(json.dumps(ded, ensure_ascii=False, indent=1), encoding='utf-8')

import statistics as st
print()
print('%-10s %-4s %-8s %-8s %-8s %s' % ('账号','n','中位阅读','均值','最高','>=1000占比'))
for acc in sorted(set(r['account'] for r in ded)):
    v = [r['views'] for r in ded if r['account']==acc]
    print('%-10s %-4d %-8.0f %-8.0f %-8d %.0f%%' % (acc, len(v), st.median(v), st.mean(v), max(v),
          100*sum(1 for x in v if x>=1000)/len(v)))
