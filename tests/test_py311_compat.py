#!/usr/bin/env python3
"""Python 3.11 语法兼容回归（2026-11-04/05 连续两批「生成文章」1 秒崩事故）。

根因：``title_elements.check()`` 里写了 Python 3.12 (PEP 701) 才允许的
``f"...{'、'.join(\\n ...)}"``（f-string 表达式内跨行）。本机 3.12/3.13、单测
job 3.12 全都绿，但批次 job 是 3.11 → ``import title_elements`` 导入期
SyntaxError，进程还没进 main() 就死了：漏斗台账没落盘、没有 LLM 调用、
告警只有「失败步骤: 生成文章」。

三条守线：
  1. 重构后的 detail 字符串必须和事故前逐字一致（别把修复做成行为变更）；
  2. 本机若有 3.11，用**真解释器**编译全仓（ast.parse(feature_version=(3,11))
     抓不住 f-string 分词，别再用它当闸门）；
  3. 闸门必须真能报红：拿一段 PEP 701 语法喂给 3.11，必须编译失败。
     没有 3.11 时跳过（≠通过），CI 由 unit-tests.yml 的 py311-compat job 硬拦。
"""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import title_elements as te  # noqa: E402

# 和事故现场同构：单行 f-string 的表达式里换行（3.12 合法 / 3.11 SyntaxError）
BAD_PEP701 = '''\
def f(items):
    return [f"命中 {'、'.join(
        [x for x in items])}"]
'''

COMPILE_ALL = r'''
from pathlib import Path
bad = []
for p in sorted(Path('.').rglob('*.py')):
    s = str(p)
    if any(x in s for x in ('.workbuddy', 'node_modules', 'cienv', 'ciut')):
        continue
    try:
        compile(p.read_text(encoding='utf-8'), s, 'exec')
    except SyntaxError as e:
        bad.append(f'{p}:{e.lineno}: {e.msg}')
print('\n'.join(bad))
raise SystemExit(1 if bad else 0)
'''


def _find_py311():
    """找本机可用的 3.11 解释器；找不到返回 None（测试跳过，不算通过）。"""
    for name in ("python3.11", "python3.11.exe"):
        p = shutil.which(name)
        if p:
            return p
    uv = shutil.which("uv")
    if uv:
        r = subprocess.run([uv, "python", "find", "3.11"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    return None


class TestPy311Compat(unittest.TestCase):

    def test_detail_unchanged_after_refactor(self):
        r = te.check("1平2负只拿0.33分，中超这根独苗还能撑到2028吗？")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["detail"], ["三要素 3/3 达标（数字、对比、专名）"])

    def test_all_files_compile_under_311(self):
        py311 = _find_py311()
        if not py311:
            self.skipTest("本机无 Python 3.11（未判定≠通过）；CI py311-compat job 硬拦")
        r = subprocess.run([py311, "-c", COMPILE_ALL], cwd=str(ROOT),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_gate_really_rejects_pep701(self):
        """负向自检：3.11 必须对 PEP 701 跨行 f-string 报错，否则闸门是摆设。"""
        py311 = _find_py311()
        if not py311:
            self.skipTest("本机无 Python 3.11（未判定≠通过）")
        r = subprocess.run(
            [py311, "-c", "import sys; compile(sys.stdin.read(), 'bad.py', 'exec')"],
            input=BAD_PEP701, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "3.11 竟然接受了 PEP 701 语法？")
        self.assertIn("SyntaxError", r.stderr)


if __name__ == "__main__":
    unittest.main()
