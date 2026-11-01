# -*- coding: utf-8 -*-
import json, re, statistics as st, collections
from pathlib import Path
rows = json.loads(Path('raw/head_home_rows.json').read_text())

TOP = r'(梅西|C罗|凯恩|姆巴佩|亚马尔|哈兰德|贝林厄姆|维尼修斯|内马尔|萨拉赫|齐达内|小罗|曼联|皇马|巴萨|拜仁|利物浦|阿森纳|曼城|切尔西)'
FEAT = {
    '顶流人名/豪门': lambda t: bool(re.search(TOP, t)),
    '冲突词': lambda t: bool(re.search(r'(炮轰|质疑|怒斥|争议|遭|喊话|反击|回击|道歉|处罚|举报|炮轰|开炮|不满|骂|打脸|拒绝|风波|危机|惩罚|严惩)', t)),
    '情绪词': lambda t: bool(re.search(r'(哭|崩|炸|怒|尴尬|无奈|惨|爆冷|震惊|破防|离谱|血洗|绝杀|屠|翻车|梦碎|失控|无敌|封神|闪耀|创历史|自毁前程|难求一胜|怪圈|泥潭)', t)),
    '具体数字': lambda t: bool(re.search(r'\d', t)),
    '冒号结构': lambda t: bool(re.search(r'[：:]', t)),
    '疑问句': lambda t: bool(re.search(r'[?？]', t)),
    '感叹句': lambda t: bool(re.search(r'[!！]', t)),
    '书面语倾向': lambda t: bool(re.search(r'(降服|闪耀|难求一胜|正名|揭幕|折桂|问鼎|荣膺|惜败|锁定|官宣|领衔|坐镇|出击)', t)),
}
def bucket(v):
    return 'A ≥1000' if v>=1000 else ('B 100-999' if v>=100 else ('C 10-99' if v>=10 else 'D <10'))

print('=== 全部样本按阅读分档 ===')
g = collections.Counter(bucket(r['views']) for r in rows)
for k in ['A ≥1000','B 100-999','C 10-99','D <10']:
    print('  %-10s %3d 条 (%.0f%%)' % (k, g[k], 100*g[k]/len(rows)))

print('\n=== 标题特征 × 阅读（全样本 n=%d）===' % len(rows))
print('%-14s %-22s %-22s %s' % ('特征','命中组中位','未命中组中位','倍数'))
for name, fn in FEAT.items():
    hit = [r['views'] for r in rows if fn(r['title'])]
    miss = [r['views'] for r in rows if not fn(r['title'])]
    if not hit or not miss: continue
    a, b = st.median(hit), st.median(miss)
    print('%-14s n=%-4d 中位 %-10.0f n=%-4d 中位 %-10.0f %.2fx' % (name, len(hit), a, len(miss), b, a/max(b,1)))

print('\n=== 控制账号：西甲二人转内部（n=%d，避免跨号偏差）===' % sum(1 for r in rows if r['account']=='西甲二人转'))
sub = [r for r in rows if r['account']=='西甲二人转']
for name, fn in FEAT.items():
    hit = [r['views'] for r in sub if fn(r['title'])]
    miss = [r['views'] for r in sub if not fn(r['title'])]
    if len(hit) < 5 or len(miss) < 5: continue
    a, b = st.median(hit), st.median(miss)
    print('  %-14s 命中 n=%-3d 中位 %-7.0f | 未命中 n=%-3d 中位 %-7.0f | %.2fx' % (name, len(hit), a, len(miss), b, a/max(b,1)))

print('\n=== 标题长度 × 阅读 ===')
for lo, hi in [(0,15),(15,20),(20,25),(25,30),(30,99)]:
    v = [r['views'] for r in rows if lo <= len(re.findall(r'[\u4e00-\u9fa5]', r['title'])) < hi]
    if v: print('  %2d-%2d 字: n=%-3d 中位 %-7.0f 均值 %.0f' % (lo, hi, len(v), st.median(v), st.mean(v)))

print('\n=== TOP 15 真实爆款 ===')
for r in sorted(rows, key=lambda x: -x['views'])[:15]:
    print('%7d  %-10s %s' % (r['views'], r['account'], r['title'][:56]))

print('\n=== BOTTOM 12 ===')
for r in sorted(rows, key=lambda x: x['views'])[:12]:
    print('%7d  %-10s %s' % (r['views'], r['account'], r['title'][:56]))
