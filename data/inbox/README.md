# data/inbox —— 导出文件投递箱

把头条后台下载的文件**直接丢进这个目录**，`scripts/ingest_exports.py` 会自动识别并入库
（R11 建议7：绩效回填自动化）。处理完文件会被移到 `data/processed/<日期>/`。

支持的文件（`.xlsx / .csv / .tsv`）：

| 后台位置 | 用来做什么 | 去向 |
| --- | --- | --- |
| 数据 → 作品数据 → 导出 | 单篇阅读/点赞/点击率/完读率/涨粉（若导出含该列） | `output/performance_log.json` + 回写各日 `metadata.json` |
| 数据 → 粉丝分析 → 导出 | 日涨粉 / 粉丝总数（转粉通道，唯一来源） | `data/growth/fans_daily.json` |

## 用法

```bash
# 本机手动跑一次
python3 scripts/ingest_exports.py

# 定时任务（CI）每天自动跑，见 .github/workflows/perf-ingest.yml
```

## 纪律

- 识别不出类型 → **原样留在 inbox** 并报 `unknown`，不猜、不导入；
- 导入失败 → 留在 inbox 并记 `failed`，下次投递自动重试；
- 同一文件（按 sha256）重复投递 → 跳过（幂等），不会重复回填；
- 幂等台账：`data/ingest_state.json`（含最近 60 次投递历史）。
