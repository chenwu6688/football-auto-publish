# -*- coding: utf-8 -*-
import json, re, statistics as st
from pathlib import Path
rows = json.loads(Path('raw/head_home_rows.json').read_text())

LOSER = r'(哭|黯然|无缘|失意|惜败|负|输|遭|崩|惨|耻辱|羞愧|捂脸|退场|离场|拉胯|反水|危机|泥潭|怪圈|出局|冷门|梦碎|罚下|红牌|伤病|缺席|落选|质疑|炮轰|争议|不满|斥|骂|打脸|翻车|瘫痪|瘫痪)'
WINNER = r'(夺魁|夺冠|登顶|加冕|荣膺|创历史|创纪录|首人|破纪录|终结|大胜|狂胜|横扫|逆转|绝杀|帽子戏法|封神|里程碑|双冠|晋级|挺进|豪取|领跑|登顶|名利双收|商业价值)'

def hit(t, p): return bool(re.search(p, t))
lo = [r for r in rows if hit(r['title'], LOSER) and not hit(r['title'], WINNER)]
wi = [r for r in rows if hit(r['title'], WINNER) and not hit(r['title'], LOSER)]
both = [r for r in rows if hit(r['title'], LOSER) and hit(r['title'], WINNER)]
neither = [r for r in rows if not hit(r['title'], LOSER) and not hit(r['title'], WINNER)]

print('=== 叙事框架 × 阅读（头部 181 条）===')
for name, grp in [('只写「输家/出事」', lo), ('只写「赢家/成就」', wi), ('两者都有（冲突）', both), ('都不涉及', neither)]:
    if not grp: continue
    v = [r['views'] for r in grp]
    print('%-22s n=%-4d 中位 %-7.0f 均值 %-8.0f 过千率 %.0f%%' % (
        name, len(v), st.median(v), st.mean(v), 100*sum(1 for x in v if x>=1000)/len(v)))

print('\n=== 同账号内对照（排除账号差异）===')
for acc in ['我爱英超','西甲二人转']:
    sub = [r for r in rows if r['account']==acc]
    a = [r['views'] for r in sub if hit(r['title'], LOSER) and not hit(r['title'], WINNER)]
    b = [r['views'] for r in sub if hit(r['title'], WINNER) and not hit(r['title'], LOSER)]
    if a and b:
        print('  %-8s 输家框架 n=%-3d 中位 %-6.0f | 赢家框架 n=%-3d 中位 %-6.0f | %.1fx' % (
            acc, len(a), st.median(a), len(b), st.median(b), st.median(a)/max(st.median(b),1)))

print('\n=== 「赢家框架」的高阅读例外（说明什么条件下赢家也能爆）===')
for r in sorted([x for x in wi], key=lambda x: -x['views'])[:6]:
    print('  %6d  %-10s %s' % (r['views'], r['account'], r['title'][:56]))
print('=== 「赢家框架」的哑弹 ===')
for r in sorted([x for x in wi], key=lambda x: x['views'])[:6]:
    print('  %6d  %-10s %s' % (r['views'], r['account'], r['title'][:56]))
