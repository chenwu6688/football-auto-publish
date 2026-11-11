# 发布终态三态契约验收（2026-11-05 新增）

- 生成时间：2026-11-11 11:09:40
- 验收结论：**通过 ✅**
- 补的缺口：没人检查「没发出去时，系统有没有说清为什么」——0 篇 + exit 1 + 批次标成已完成，三件事同时成立，任何单一日志都看不出全貌。

## 退出码四态互异（含负向自检）：✅
- ✅ 四态互异：{'ok': 0, 'fail': 1, 'nothing_skipped': 2, 'nothing_empty': 3}
- ✅ 负向自检：把 EXIT_NOTHING_SKIPPED 设成 1 后本判据会报红

## load_articles 不得 sys.exit（改用 raise）：✅
- ✅ 无 sys.exit；没稿可发走 NoPublishableBatch（带 code + 台账）

## batch.yml 处理 rc=2/3：✅
- ✅ 按退出码分流
- ✅ 处理 rc=2（标完成 + warning）
- ✅ 处理 rc=3（exit 1 → 不标批次完成）
- ✅ rc=3 分支引用了 0 篇原因归类（publish_zero_reason）
- ✅ 回写批次状态绑定 publish.outcome

## 重拟链不得裸调评分器：✅
- ✅ 4 个入口均走 _safe_title_score / _safe_elements_ok

## 预测稿口径唯一（4 处）：✅
- ✅ 4 处口径一致（按依据分级，不按体裁）

## 批次已完成只由发布成功写：✅
- ✅ 生成侧只写 batches_generated，不写 completed
- ✅ 发布成功步骤调用 mark_batch_completed.py（completed 唯一写入点）
