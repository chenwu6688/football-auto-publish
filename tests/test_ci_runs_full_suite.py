#!/usr/bin/env python3
"""守「CI 真的跑全量单测」这件事本身（2026-11-11）。

背景：`.github/workflows/unit-tests.yml` 曾用
`--ignore=tests/test_video_pipeline.py` 把一整个测试文件排除在外，理由是
「缺 edge_tts/ffmpeg 的重依赖、且 video_pipeline 不在发布主链路」。

代价是：那个文件里的 5 个用例整整一个版本没有执行过，而它们当时**已经坏了**
（夹具硬编码 libopenh264，在没编该编码器的 ffmpeg 上 rc=8）。全量跑起来
才 5 秒就暴露。

教训不是「那个 ignore 写错了」，而是**排除项本身就是不可观测的破口**：
一条 --ignore 加进去，红灯永远不会出现，直到有人手动去跑。所以这里用测试
把「全量」钉住：新增排除项会让本文件红，而不是让覆盖率悄悄缩水。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "unit-tests.yml"
REQUIREMENTS = ROOT / "requirements.txt"


def test_unit_tests_workflow_does_not_ignore_any_test_file():
    """单测 job 不许用 --ignore 排除测试文件。

    只扫**真实命令行**：YAML 注释里写到 ``--ignore=`` 是在记录「为什么取消它」，
    不是排除项。把注释当证据会让守卫自己红（第一版就这么错的）。
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    offenders = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):     # 注释不是配置
            continue
        if "--ignore=" in stripped and "tests/" in stripped:
            offenders.append(stripped)
    assert offenders == [], (
        "unit-tests.yml 排除了测试文件：\n  " + "\n  ".join(offenders) +
        "\n排除项会让被排除的用例永远不执行 —— 坏了也没人知道。"
        "若确实需要重依赖，请把依赖补进 requirements.txt / 安装步骤，"
        "而不是把用例藏起来。")


def test_pytest_job_runs_whole_tests_dir():
    """单测 job 的 pytest 命令必须作用在整个 tests/ 目录上。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pytest tests/" in text, "unit-tests.yml 里找不到 `pytest tests/` 全量调用"


def test_video_pipeline_dependency_is_declared():
    """``edge-tts`` 必须在 requirements.txt 里 —— 否则 video 用例收集期就失败。"""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    names = [ln.split(">")[0].split("=")[0].strip().lower()
             for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert "edge-tts" in names, (
        "requirements.txt 缺 edge-tts：tests/test_video_pipeline.py 导入 "
        "video_pipeline.tts 时会 ModuleNotFoundError，pytest 收集期 exit 2，"
        "整个 job 一个用例都跑不了。")


def test_every_test_file_actually_contains_tests():
    """`tests/test_*.py` 里必须至少有一个 `def test_*`。

    2026-11-11 发现 `tests/test_all_categories.py` 其实是个要 API Key 的手工演示
    脚本：pytest 从它里面收集到 **0 个用例**，但它一直计入「测试文件数」，让人以为
    四品类生成有自动化覆盖。**空壳测试文件比没有测试更坏**——它给的是虚假的安全感。

    这条判据用 AST 扫真实函数定义（注释/字符串里的 `def test_` 不算）。
    需要手工才能跑的东西放 `scripts/`，不要放 `tests/`。
    """
    import ast
    offenders = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        n = sum(1 for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test"))
        if n == 0:
            offenders.append(f.name)
    assert offenders == [], (
        f"这些文件在 tests/ 下但收集不到任何用例：{offenders}。"
        f"手工脚本请移到 scripts/，否则它只会制造「有覆盖」的错觉。")
