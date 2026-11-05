# 采集失败分类验收报告

- 生成时间：2026-11-05 10:44
- 验收结论：**通过 ✅**

## 非 200/404 必须抛出而非静默返空（硬项）：✅
- EdgeOne 挑战（567）→ 抛出 ScraperChallengeError ✅
- Cloudflare 挑战（521）→ 抛出 ScraperChallengeError ✅
- 403 封禁（403）→ 抛出 ScraperBlockedError ✅
- 5xx 服务异常（503）→ 抛出 Exception ✅

## 真空数据 / 被拦 / 挑战页三者可区分（硬项）：✅
- 404 → 返回空串 + kind=empty（页面真没了）✅
- 403 → kind=blocked（不与 empty 混记）✅
- 只有 empty 时不报警（真空数据不是故障）✅

## 200 挑战页壳子可识别且不误伤长文（硬项）：✅
- 薄壳 + EdgeOne 特征 → challenge ✅
- 长文含 captcha（80007 字符）不误判 ✅
- 200 挑战壳 → ScraperChallengeError ✅

## 负向验证：篡改台账必须报错（硬项）：✅
- 正样本通过校验 ✅
- kind 篡改 → 报错：第 0 条 kind 非法: 'totally-made-up' ✅
- status 缺失 → 报错 ✅

## 真实采集台账（告警项）（告警项）：❌
- 生成时间 2026-11-04 12:05｜记录 75 条
- 需人工介入：http → challenge
- 需人工介入：dongqiudi → challenge
