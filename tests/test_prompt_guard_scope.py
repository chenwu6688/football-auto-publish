"""提示词四方针（H 项）的扫描面必须覆盖「示例即模板」的那两类文件。

2026-11-11 晚间事故：11-06 立的 H 项只扫 prompts/*.txt + constants.py +
orchestrator.py，于是漏了两处真正在教模型写违规话术的地方：

  · ``config/brand_manual.yaml`` 的 hook_strategy —— 两条账号沉淀示例 +
    一条数字表态示例，被 ``_render_brand_manual`` 原样渲染进改写 prompt；
  · ``scripts/refill_reservoir.py`` 的 ``_TMPL`` —— 强制要求写一句账号引导
    并给了原句示例，蓄水池是 30% 稿源。

结果：模型的成稿 100% 带「诱导交互 / 违规涨粉」话术，出口合规按 TT-07/TT-08
各判 20 分整篇剔除，11-11 晚间 2 篇全丢、发布 0 篇、批次未完成、心跳告警。

本文件锁三件事：
  1. 扫描面包含这两类文件（否则下次又漏）；
  2. 当前真实配置是干净的（正向）；
  3. 把事故原句塞回去必须判红（负向，防「检查项退化成永远 ok」）。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "verify_rule_coverage", ROOT / "scripts" / "verify_rule_coverage.py")
vrc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vrc)


def test_scan_scope_includes_brand_manual_and_reservoir_prompt():
    units = vrc.prompt_texts()
    assert "config/brand_manual.yaml" in units, "品牌手册会被渲染进 prompt，必须在扫描面内"
    assert "scripts/refill_reservoir.py" in units, "蓄水池提示词是 30% 稿源的源头，必须在扫描面内"


def test_current_prompts_pass():
    r = vrc.check_prompt_alignment()
    assert r["ok"] is True, r.get("detail")


def test_negative_brand_manual_solicit_follow_is_caught():
    bad = {"config/brand_manual.yaml": [(1, "关注老六，这转会窗的瓜我替你盯着")]}
    r = vrc.check_prompt_alignment(bad)
    assert r["ok"] is False
    assert any("索要关注" in d for d in r["detail"])


def test_negative_brand_manual_digit_vote_is_caught():
    """「觉得A不该罚下的扣1」示例：教模型用数字表态换互动，也是 20 分档。"""
    bad = {"config/brand_manual.yaml": [(1, "评论区站队，觉得A不该罚下的扣1，觉得没问题的扣2")]}
    r = vrc.check_prompt_alignment(bad)
    assert r["ok"] is False


def test_yaml_scan_ignores_comments():
    """只扫渲染进 prompt 的值：注释里的说明不算证据（与 AST 那条一致）。

    否则这份文件里解释事故的注释会把检查自己刷红，逼着人删注释——
    下次事故就没人知道为什么不能这么写了。
    """
    import yaml
    data = yaml.safe_load(
        (ROOT / "config" / "brand_manual.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # 注释不在解析结果里，_yaml_values 自然扫不到；这里只确认接口仍按值遍历
    vals = vrc._yaml_values(ROOT / "config" / "brand_manual.yaml")
    assert vals and all(isinstance(v, str) for _, v in vals)
    assert not any("comment" in v for _, v in vals)
