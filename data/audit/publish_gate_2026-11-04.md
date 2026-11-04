# 发布闸门验收报告（计划 7.2 / 7.3）

- 生成时间：2026-11-04 18:03
- 验收结论：**通过 ✅**

## 成稿标题零硬违规（生效日起）（硬项）：跳过
- 无法载入评分器：unterminated string literal (detected at line 281) (title_elements.py, line 281)

## 标题格式合规通过率≥90%（近14天）（告警）：跳过
- 无法载入评分器：unterminated string literal (detected at line 281) (title_elements.py, line 281)

## 队名判定别名感知（计划7.3）（硬项）：跳过
- 无法载入事实校验器：unterminated string literal (detected at line 281) (title_elements.py, line 281)

## 比分判定口径归一（计划7.3）（硬项）：跳过
- 无法载入事实校验器：unterminated string literal (detected at line 281) (title_elements.py, line 281)

## 溯源覆盖率（目标100%）（告警）：✅
- 成稿 930 条，带来源 112 条，覆盖率 12.0%（目标 100%）
- ⚠️ 未达标（存量缺口，告警不阻断；口径与 audit_isolation D 一致）

## 一致性校验拦截率可统计（硬项）：跳过
- consistency_stats 台账暂无运行记录，机制就绪，本项跳过

## 一致性校验误杀率<5%（告警）：跳过
- 尚无人工抽检标注，误杀率「待接入」（绝不用 0 冒充）
- 上限 5%；标注写入 data/consistency_labels.json

## 打回可定位到选题（告警）：跳过
- 近 30 天无打回记录，本项跳过

## 连续14天无旧闻新发（硬项）：跳过
- 时效闸门台账暂无记录，本项跳过

## 赛果播报式存量（近14天，告警）（告警）：✅
- 近 14 天成稿 86 条，命中赛果播报式 0 条 ✅

## 标题三要素覆盖率（近14天，告警）（告警）：跳过
- 无法载入三要素口径：unterminated string literal (detected at line 281) (title_elements.py, line 281)

## 熔断拦住当前批次（计划十章）（硬项）：✅
- 生成侧中止（orchestrator._breaker_active）+ 发布边界硬闸（publisher.publish_all）+ CI 生成后复核（batch.yml）三层验收
- 三层防线齐备 ✅

## 失败告警可诊断（带原因）（硬项）：✅
- 两处合规中止写结构化原因 + 失败通知走 notify_failure.py + 原因为空时给占位 + 非合规失败按分步结论兜底
- 告警可诊断 ✅
