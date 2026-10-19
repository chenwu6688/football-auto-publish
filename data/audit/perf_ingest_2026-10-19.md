# 绩效回填自动化验收报告（建议7）

- 生成时间：2026-10-19 18:05
- 验收结论：**通过 ✅**

## 投递箱类型识别（perf / growth / unknown）（硬项）：✅
- 作品数据导出 → perf ✅；粉丝分析导出 → growth ✅；无关文件 → unknown ✅

## 幂等 + 归档（duplicate 跳过 / 文件移出 inbox）（硬项）：✅
- 首次：ok（2 行，差分 1）✅
- 重复投递：duplicate（sha256 幂等）✅
- 归档：data/processed/<日期>/ ✅

## 失败/未知不吞（留在 inbox 等人工确认）（硬项）：✅
- unknown → 留在 inbox ✅；failed → 留在 inbox 等重试 ✅

## 复用入口（import_performance.ingest_toutiao）（硬项）：✅
- 投递箱与 CLI 共用同一导入入口 ✅

## 滞后监控（滞后天数 / 待接入 / 覆盖率）（硬项）：✅
- 滞后天数 / 无数据 None / 覆盖率口径 均正确 ✅

## CI 接线（定时自动入库 + 滞后监控 + 回写）（硬项）：✅
- perf-ingest.yml：cron 定时 + 消费投递箱 + 滞后监控 + 回写提交 ✅

## 一键周更入口（weekly_review.py）（硬项）：✅
- 收文件 → 入库 → 滞后 → 看板 → 调参建议 → 汇总报告 六步接线齐 ✅
- 调参只算不写（不动 config/ratio_config.yaml）✅
- dry-run 零副作用（不写台账/转粉/报告）✅

## 导出自动回收（collector）（硬项）：✅
- toutiao-collector.yml：每天 14:05 CST cron + 登录态注入 + 回写投递箱 ✅
- workflow 参数契约（run 命令 --flag 均已被 argparse 注册）✅
- 降级告警为真实 Call（登录过期/导出失败/缺登录态三条路径）✅
- 落箱同名不覆盖（sha 判重 / 更新另存 _2）✅
- 无登录态不启动浏览器 ✅

## 月度复盘（monthly_review）（硬项）：✅
- monthly-review.yml：每月 1 号 15:00 CST cron + 回写提交 ✅
- 月度统计按自然月边界（collect since/until）✅
- 调参只算不写（AST 禁 apply_suggestion/save_config）✅
- dry-run 零副作用（不写看板/报告）✅

## 当前滞后实况（仅提示）（告警项）：✅
- 绩效最新 2026-10-18（滞后 1 天）；转粉最新 2026-10-17（滞后 2 天）
- 近 14 天回填覆盖率 55.7%（68/122）
