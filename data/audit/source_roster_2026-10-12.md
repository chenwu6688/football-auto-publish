# 数据源与信源清单验收报告（计划 13.2 / 13.4）

- 生成时间：2026-10-12 13:54
- 验收结论：**通过 ✅**

## 四源分工齐备（13.2）（硬项）：✅
- ✅ football-data.org
- ✅ API-Football
- ✅ 聚合数据 juhe.cn
- ✅ 人工赛历

## 原始信源清单齐备（13.4）（硬项）：✅
- 就位 11/11：罗马诺、天空体育、德国天空体育、迪马济奥、米兰体育报、全市场、阿斯报、马卡报、图片报、队报、俱乐部官方

## 信源只作信号（13.4 纪律）（硬项）：✅
- source_watch 仅驱动 L2 热度信号，抽取器/生成器无引用 ✅

## 不接入商业数据商（13.2）（告警）：✅
- 商业数据商（纳米/火星/创冰/同道伟业）零引用 ✅

## 无授权媒体接口边界（13.2）（告警）：✅
- 计划 13.2：懂球帝/直播吧/虎扑/雷速 接口无授权，只能用其名提取实体中文名、
- 可作新闻流素材，但**正文不得进生成器**（由 11.3 物理隔离与相似度闸门兜底）。
- 当前命中文件：{'懂球帝': ['content_reference.py', 'content_reference.yaml', 'data_collector.py', 'health_check_scraper.py', 'image_service.py', 'media_scraper.py', 'orchestrator.py', 'utils.py'], '直播吧': ['content_reference.py', 'content_reference.yaml', 'data_collector.py', 'health_check_scraper.py', 'media_scraper.py', 'orchestrator.py'], '虎扑': ['content_reference.yaml']}
- （本项为边界提示，不作硬断言）
