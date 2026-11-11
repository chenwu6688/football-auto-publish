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
