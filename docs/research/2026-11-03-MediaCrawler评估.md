# MediaCrawler 评估：它是什么、能不能用、该用哪一部分

> 调研时间：2026-11-03
> 对象：`NanmiCoder/MediaCrawler`（GitHub main 分支，commit 截至 2026-10-27）
> 方法：下载全仓源码（18.6 MB / 356 文件 / 32,504 行 Python）逐层通读 + 与本项目现有采集链路逐模块对照
> 结论口径：只讨论「能不能落到 `football-auto-publish` 这条流水线上」，不讨论泛泛的爬虫技术

---

## 0. 结论先行

**一句话**：MediaCrawler 是一个工程质量很高的「7 平台内容 + 评论」浏览器自动化抓取框架，核心巧思是**不逆向算法、而是让平台自己的 JS 替它算签名**；但对我们这条流水线，**它不该进主流程，只该借两个工程范式**。

三条判断，按重要性排序：

1. **许可证把路堵死了**。仓库是 `NON-COMMERCIAL LEARNING LICENSE 1.1`（非商业学习许可），不是 MIT/Apache。头条号流量分成是商业行为，直接违约。项目自己的免责声明还写着「禁止用于商业用途」，这句话在诉讼里不是护身符，而是**「明知」的证据**。
2. **它能补的那一格，收益被现有架构封顶了**。它能提供的是「社媒讨论量」这一个信号（我们现在是 0 覆盖）。但 `ranker._SOCIAL_FAME_BANDS = ((200_000, 1),)` 只给 **+1 分**，满分 25 分。而且抖音互动量 → 头条图文 CTR 是跨平台口径，和我们自己踩过的「视频号播放量 vs 图文阅读量不能混算」是同一个坑。
3. **风险不对称**。抖音和今日头条同属字节，用带登录态的真实浏览器抓抖音，与发布头条号的环境存在设备关联可能。收益是「排序 +1 分」，风险是「发布账号」。

**该拿的两样东西（零许可风险，本周可做）**：

- **CDP 复用真实浏览器登录态**——直接改善我们 `collect_toutiao_exports.py` / `publisher.py` 每次都要靠 secret 注入 `toutiao_auth.json` 的脆弱性。
- **「被拦必须显式区分 403 / 429 / 挑战页 / 真空数据」**——直接修 `media_scraper.py` 里懂球帝走 EdgeOne 返回 HTTP 567 时 `return ""` 静默吞异常的老毛病。

---

## 1. 项目基本面

| 项 | 数值 | 说明 |
|---|---|---|
| Star / Fork | 69,939 / 13,275 | 自媒体爬虫类目第一梯队 |
| Open Issues | 220 | 平台改版带来的持续问题单 |
| 语言 / 规模 | Python 3.11+ / 32,504 行（356 文件） | `media_platform/` 2.3 MB 为主 |
| 创建时间 | 2023-06-09 | 已维护 3 年半 |
| 最近推送 | 2026-10-27 | 但见下条 |
| **最近代码提交** | **2026-09-19** | 之后 45 天零代码提交 |
| 许可证 | NON-COMMERCIAL LEARNING LICENSE 1.1 | **非开源许可证**（OSI 不认） |

### 1.1 活跃度必须拆开看

拉最近 40 次提交，结构非常清楚：

```
2026-09-19  fix(dy): 补上 ArgusSecurityPlugin 要求的 x-tt-argus 请求头
2026-09-18  fix(dy): 补上 detail 接口的 uifid/verifyFp 风控参数
2026-09-18  fix(xhs): 适配上游 EF* 分档，修复视频只下到封面
2026-09-18  fix(bili): 登录态判定改为校验 cookie 有效性
2026-09-17  feat(media): 重构媒体下载，支持五平台
--- 以上为最后一次代码提交 ---
2026-10-03 ~ 10-27  9 次提交，全部是 README / 赞助商 / Pro 版介绍
```

两个结论：

- **9 月那三天 8 个 fix 全是「补平台风控参数」**（`x-tt-argus`、`uifid/verifyFp`、`EF*` 分档、ArgusSecurityPlugin）。这说明这类工具的**真实维护成本是跟版**：平台改一次风控参数，工具就哑一次。
- **10 月之后代码冻结，只更新赞助商**。项目重心已转向 Pro 订阅（断点续爬 / 多账号 / 去 Playwright / Linux 支持）。开源版正在进入「够用但不跟版」的状态——**这对我们是坏消息**：我们需要的恰恰是「跟版」。

### 1.2 它明确没有的东西

- **没有头条号**。7 个平台是 小红书 / 抖音 / 快手 / B站 / 微博 / 贴吧 / 知乎。**我们要发布的那个平台，它一个字都读不到**。
- **没有断点续爬**（Pro 才有）。默认 `CRAWLER_MAX_NOTES_COUNT=15` 一跑到底，中断即从头。
- **没有粉丝画像 / 私域数据**。只能拿到公开页面能看到的聚合互动量。

---

## 2. 核心功能

### 2.1 能力矩阵（全部 7 平台对齐）

| 平台 | 代号 | 关键词搜索 | 指定 ID | 二级评论 | 创作者主页 | 登录态缓存 | 代理池 | 媒体下载 |
|---|---|---|---|---|---|---|---|---|
| 小红书 | `xhs` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 抖音 | `dy` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 快手 | `ks` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| B站 | `bili` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 微博 | `wb` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 贴吧 | `tieba` | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✗ |
| 知乎 | `zhihu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✗ |

### 2.2 三种抓取模式（`CRAWLER_TYPE`）

| 模式 | 行为 | 对我们的潜在用途 |
|---|---|---|
| `search` | 按 `KEYWORDS` 逗号分词循环搜索，逐页翻 | **唯一对信号层有意义**的模式 |
| `detail` | 按给定 ID / URL 列表精确抓 | 需先知道抓谁，价值低 |
| `creator` | 抓指定创作者主页全部作品 + 评论 | 对标账号监控（我们已有 `benchmark_peers.py`） |

### 2.3 周边能力

- **评论**：`ENABLE_GET_COMMENTS=True` 默认开，一级评论 `CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES=10`；二级默认关。
- **词云**：jieba 分词 + wordcloud，支持自定义词表（`CUSTOM_WORDS`，如把「零几」识别为整体）和停用词文件。
- **代理池**：快代理 / 豌豆HTTP / 静态代理三选一，带 IP 校验与失效刷新（`ProxyRefreshMixin`）。
- **存储**：`csv / json / jsonl / sqlite / mysql / postgres / mongodb / excel` 八种，db 模式带去重。
- **三个交互面**：CLI（typer）、FastAPI（`/api/crawler/start|stop|status|logs` + WebSocket 日志流）、React WebUI。

---

## 3. 技术架构与实现原理

### 3.1 分层

```
入口层     main.py ／ cmd_arg(argparse→typer) ／ config/*.py ／ api(FastAPI) ／ webui(React)
            │  CrawlerFactory.create_crawler(platform) → 7 个实现类
核心爬虫层  AbstractCrawler 子类：XiaoHongShuCrawler / DouYinCrawler / ...
            │  职责：登录、翻页、并发编排、评论调度、媒体下载
API 客户端层 AbstractApiClient 子类：XiaoHongShuClient / DouYinClient / ...
            │  职责：签名、请求、重试(tenacity)、异常分类、代理刷新
存储层      StoreFactory → csv / json / jsonl / sqlite / mysql / mongodb / excel
            │  AbstractStore：store_content / store_comment / store_creator
基础设施层  proxy(IP池) ／ cache(本地+Redis) ／ media_downloader(ffmpeg) ／ tools(stealth, slider, execjs)
```

三件套抽象（`AbstractCrawler` / `AbstractApiClient` / `AbstractStore`）是它真正的可复用资产：**加一个平台 = 加一个目录实现这四个文件**（`client/core/login/field/help/media`）。这个结构本身值得抄。

### 3.2 核心原理：签名不是算出来的，是「借」来的

这是整个项目最关键的技术点，也是理解其风险的钥匙。

平台接口都要求签名参数（小红书的 `x-s`/`x-t`/`x-s-common`、抖音的 `a_bogus`、快手的 `__ks_realm` 加密）。常规做法是逆向算法或补环境；**它选的是第三条路：让平台自己的代码在自己面前运行，然后把结果抄下来**。

| 平台 | 实现 | 机制 |
|---|---|---|
| 抖音 | `execjs.compile("libs/douyin.js")` → `douyin_sign_obj.call("sign_datail" / "sign_reply", params, ua)` | 把平台混淆过的签名 JS 抽出来，用 Node 本地执行。**不需要浏览器** |
| 抖音（旧方案，已废弃） | `page.evaluate("() => window.bdms.init._v[2].p[42].apply(null,[0,1,8,params,post_data,ua])")` | 直接调页面里挂载的函数，代码注释写着 `playwright version is deprecated` |
| 小红书 | `media_platform/xhs/playwright_sign.py` → `xhshow` 库算 `x-s/x-t/x-s-common/x-b3-traceid` | 早期走 `window._webmsxyw` JS 表达式，2026-07 起换成纯算法库（MIT），注释说明「依赖 xhshow>=0.2.0，原生修复了 GET 请求 a3_hash 计算的 bug」 |
| 快手 | 注入 hook 到页面，等 `window.__ks_realm` 就绪后 `call('$encode', [...])` | 平台自己把加密对象挂在 window 上，直接借用 |
| 知乎 | `libs/zhihu.js` + execjs | 同抖音 |

**这就是「无需 JS 逆向」这句话的真实含义**：不是破解了算法，而是**绕过了「需要理解算法」这件事**。技术上很优雅，法律上很敏感——它绕过的正是平台设置的「技术保护措施」。这一点在第 5 章展开。

### 3.3 登录态：三种方式 + CDP 复用

- `qrcode`（扫码，默认）/ `phone`（手机号）/ `cookie`（直接贴）
- 登录态落 `browser_data/`（`USER_DATA_DIR = "%s_user_data_dir"`），下次免登录
- **`ENABLE_CDP_MODE = True` + `CDP_CONNECT_EXISTING = True`（新版默认）**：连用户**已经打开的 Chrome**（`chrome://inspect/#remote-debugging`，需 Chrome ≥ 144），直接复用它的 Cookie、扩展、浏览历史

官方文档把 CDP 模式写成「反检测最佳方案」，常见问题里也直说：「小红书扫码成功后一直验证滑块，**强烈建议使用 CDP 模式连接自己的真实浏览器**，不要用无痕或标准 Playwright」。

**翻译一下**：它承认默认自动化环境会被识别，解决方案是**把你本人的浏览器租给它用**。这意味着风控命中的是**你的真实账号**，不是某个匿名身份。

### 3.4 反检测细节（可作为工程参考）

`media_platform/tieba/core.py` 里那段注入脚本是最完整的样本：覆写 `navigator.webdriver`、补 `window.navigator.chrome`、包装 `navigator.permissions.query`、删除 `cdc_adoQpoasnfa76pfcZLmcfl_*` 系列（ChromeDriver 残留变量），外加 `libs/stealth.min.js`（180 KB）。

这段代码的实用价值其实在**反面**：它精确列出了「自动化浏览器会被怎么识别」。我们自己在 CI 里跑 `publisher.py` / `collect_toutiao_exports.py`，同样面对这个问题。

### 3.5 并发与限速：默认就是慢速低频

```python
MAX_CONCURRENCY_NUM = 1          # 并发 1
CRAWLER_MAX_SLEEP_SEC = 2        # 每页/每条后 sleep 2s
CRAWLER_MAX_NOTES_COUNT = 15     # 单次最多 15 条
```

`semaphore = asyncio.Semaphore(MAX_CONCURRENCY_NUM)` + 每页 `await asyncio.sleep(...)`。默认配置下它对平台的负载接近人工浏览——**但这是自保（少被封），不是合规**。合规取决于「有没有获得授权」，不取决于「抓得慢不慢」。

---

## 4. 支持的数据源与抓取方式

### 4.1 能稳定拿到的字段

| 平台 | 核心入口 | 能拿到的 | 拿不到的 |
|---|---|---|---|
| 抖音 | 搜索 / `aweme_detail` | 作品 ID、标题、话题、发布时间、`statistics`（点赞/评论/分享/收藏/播放）、作者基础信息 | 真实完播率、粉丝画像、推荐来源 |
| 小红书 | 搜索 / note detail / `window.__INITIAL_STATE__` | note_id、标题、正文、`liked_count` 等互动数、图片/视频 URL | 收藏-点赞转化、私域数据 |
| 微博 | 搜索 / detail | 正文、转发/评论/点赞数、发布时间 | 阅读量（非博主不可见） |
| 知乎 | 搜索 / 回答 / 文章 | 标题、正文、`voteup_count`、`comment_count` | 收藏数、盐选数据 |
| B站 | 搜索 / 视频详情 | 播放/弹幕/评论/点赞/投币/收藏 | 完播率、推荐流量 |
| 快手 | 搜索 / REST 签名 | 视频、互动数 | — |
| 贴吧 | 搜索 / 帖子 | 帖子、回复 | — |

**只有「页面上肉眼可见的聚合互动量」是稳的**。任何需要登录后台才能看到的指标（完播、涨粉来源、推荐占比）它一概没有——这和我们从 `mp.toutiao.com` 导出作品数据是完全不同性质的通道。

### 4.2 抓取形态

`search` 模式的实际行为（以小红书为例，`media_platform/xhs/core.py`）：

```
for keyword in KEYWORDS.split(","):
    page = 1
    while (page - start_page + 1) * 20 <= CRAWLER_MAX_NOTES_COUNT:
        notes = get_note_by_keyword(keyword, search_id, page, sort)
        if not notes or not notes["has_more"]: break
        # 并发抓详情（信号量=1）
        # 逐条 download_media（默认关）
        # batch_get_note_comments
        await asyncio.sleep(CRAWLER_MAX_SLEEP_SEC)   # 每页 2s
```

注意 `has_more` 判断和 `except DataFetchError: break`——**被风控时会静默结束当前关键词，日志里只留一行 "Get note detail error"**。这正是我们 `media_scraper.py` 踩过的坑（懂球帝 567 挑战页 → `return ""`），说明这个错误在不同项目里会以同一种方式复现。

---

## 5. 使用限制与合规风险

### 5.1 许可证：这是硬约束，不是提醒

`LICENSE` 原文（中英双语）：

> 1. 版权所有者特此免费授予……**在非商业学习目的下**使用、复制、修改、合并本软件
> 2. 本软件**仅限用于学习和研究目的**，不得用于大规模爬虫或对平台造成运营干扰的行为
> 3. **未经版权所有者书面同意，不得将本软件用于任何商业用途**

我们是什么性质？头条号流量分成 + 可能的商单。**是商业用途**。

有人会说「我只是内部工具，不算商业」。风险在于判定标准不是「有没有直接卖软件」，而是「是否用于经营性活动」。用爬来的数据支撑一个变现账号的选题，链条是完整的。

另外注意 README 顶部那句免责声明：

> 大家请以学习为目的使用本仓库⚠️⚠️⚠️⚠️，爬虫违法违规的案件……本仓库的所有内容仅供学习和参考之用，**禁止用于商业用途**。

**这类声明不能免除使用者的责任，只能加重。** 它证明作者已明确禁止，使用者仍然用了 → 在侵权/不正当竞争纠纷中直接构成「明知故犯」。这是评估这类工具时最容易忽略的一点。

### 5.2 技术限制

| 限制 | 具体表现 | 对我们的影响 |
|---|---|---|
| 必须有登录态 | 扫码/手机号/Cookie，Cookie 会过期 | 无法无人值守，与「全 CI」流水线正交 |
| 需要真实浏览器 | CDP 模式要已打开的 Chrome ≥ 144；标准模式要 `playwright install` | GitHub Actions 里要装 Chromium + 处理滑块，成功率不可控 |
| 需要 Node | 抖音/知乎签名走 `execjs`，缺 Node 报 `SyntaxError: 缺少 ';'` | 多一层运行时依赖 |
| 无断点续爬 | 中断即从头（Pro 才有） | 71 个实体跑一半被封 = 全白跑 |
| 单账号 | 开源版无多账号轮换 | 封一个就断供 |
| 跟版成本 | 9 月三天 8 个 fix 全在补风控参数 | 平台一改就哑，且 10 月起已不跟 |
| 无头条号 | 7 平台不含头条 | 读不到自己的数据 |

### 5.3 法律风险（按层次）

| 层次 | 依据 | 我们的暴露面 |
|---|---|---|
| **合同/许可** | 《著作权法》第十七条（计算机软件）＋ 本仓库许可 | 未经书面同意商用 → 违约/侵权 |
| **平台规则** | 小红书/抖音/微博《用户协议》均禁止「使用任何自动化程序、软件、引擎、网络爬虫」；robots.txt 基本全站 Disallow | 账号封禁、设备封禁、IP 封禁 |
| **不正当竞争** | 《反不正当竞争法》第十二条（互联网专条：妨碍、破坏其他经营者合法提供的网络产品正常运行） | 若被平台主张「影响正常运行」，举证责任在我方 |
| **个人信息** | 《个人信息保护法》：评论区的昵称、头像、用户 ID、主页链接都是个人信息 | 抓评论 = 处理他人个人信息，需「告知-同意」或法定事由，我们没有 |
| **数据安全** | 《网络安全法》第二十七条：不得从事非法侵入他人网络、干扰正常功能、**窃取网络数据** | 绕过签名调私有 API，容易被认定为「未经授权访问」 |
| **刑事** | 《刑法》第二百八十五条第二款（非法获取计算机信息系统数据罪）；已有「爬虫入刑」判例 | 低频个人使用实际风险低，但**绕过技术措施**这一情节是加重项，不是减轻项 |

**必须点明的一个区分**：同样是取数，读公开 RSS / 公开榜单接口（我们现在的微博热搜、百度联想），和**伪造签名调用平台私有 API**，法律定性完全不同。前者接近「访问公开内容」，后者接近「规避技术保护措施」。MediaCrawler 的核心技术恰好是后者——它的工程优雅之处，正是它的法律危险之处。

### 5.4 一个只有我们这个项目才有的风险：字节系关联

抖音与今日头条同属字节跳动，账号体系互通（可用抖音号登录头条），发布走 `mp.toutiao.com`。

如果按它的推荐方式「CDP 连接你自己的真实 Chrome」，那么：
- 同一个浏览器实例、同一套扩展与浏览历史
- 同一台设备指纹（Canvas / WebGL / 字体列表 / 时区）
- 同一个出口 IP

而 `publisher.py` 与 `collect_toutiao_exports.py` 也在这台机器上跑。**风控层面的设备关联是可能的**。

这笔账很清楚：**收益 = 排序 +1 分；风险 = 发布账号。** 不对称到不该做。

---

## 6. 融入本项目的可行性评估

### 6.1 我们现在的采集面

| 模块 | 数据源 | 通道 | 性质 |
|---|---|---|---|
| `media_scraper.py` | 直播吧、懂球帝 | 纯 HTTP + BeautifulSoup | 公开页面 |
| `source_watch.py` | `config/source_watchlist.yaml` | 本地清单 → 匹配标题 | 不联网 |
| `signal_sources.py` | 微博热搜、百度热搜、百度联想 | 公开接口（带 Referer） | 公开接口 |
| `redfox_client.py` | 抖音作品、公众号文章 | **付费 API**（¥0.02/次起） | 采买 |
| `scripts/collect_toutiao_exports.py` | 自有后台作品数据/粉丝分析 | Playwright + 登录态 | **自有数据** |
| `scripts/benchmark_peers.py` | 对标账号主页 | Playwright（无则退 2） | 公开页面 |

MediaCrawler 能插的位置只有一个：**`signal_sources` 的「社媒讨论量」**，以及理论上新增的「评论洞察」。

### 6.2 信号层体检：那一格现在是空的

实测 `data/signals/external_signals.json`（2026-11-02 生成，71 个实体）：

| 信号 | 有值实体数 | 分布 | 判定 |
|---|---|---|---|
| 同题报道条数 | 0 | — | **全缺** |
| 来源媒体权重 | 0 | — | 由 `_SOURCE_WEIGHT` 兜底 |
| **社媒讨论量** | **0** | — | **全缺**（只有热搜命中才产生，足球实体几乎不上热搜） |
| 搜索指数 | 71 | **69 个 = 100**，1 个 80，1 个 70 | **饱和，无区分度** |

`ranker.py` 的注释自己也写了：「同源的『搜索指数』实测 97% 恒为 100（百度联想饱和），无区分度，故不参与加分」。

RedFox 那边倒是有真数据（`redfox_signals.json`，2026-10-13）：

```
足球 832,119 ／ 姆巴佩 519,066 ／ 梅西 203,753 ／ 内马尔 117,376 ／ C罗 20,697
```

但只有 12 个关键词、跑了一次（`data/redfox_budget.json` 只有 `{"2026-10-13": 12}` 这一条），之后没再刷新。

**所以「社媒讨论量缺口」是真实存在的**——这是 MediaCrawler 唯一有说服力的落点。

### 6.3 但收益被现有架构封顶了

`ranker.py`：

```python
# L2 社媒信号 → 维度加分单档阈值（依据 2026-10-13 实测：社媒讨论量 2.07万~115.5万）
_SOCIAL_FAME_BANDS = ((200_000, 1),)     # 社媒讨论量 → 人物知名度 +1
```

- 只有 **一档、只加 1 分**，作用于「人物知名度」这一维
- L3 量表满分 **25 分**（五维各 1-5）
- 注释解释了为什么只给 +1：基线已由 `awareness` 分层（A5/B3/C1），再加会把低知名度主体抬到 B 级球星同级

即使把这 71 个实体的社媒讨论量全部补齐，**理论上限就是每篇 +1 分**。而补它需要：一个带登录态的浏览器、一台不跑头条的机器、每天一次人工确认、以及一个非商业许可证的违约风险。

### 6.4 更根本的问题：我们其实不知道 L3 现在怎么打分

查 `data/rank_audit/`，**只有一份 2026-10-10 的校准，且 10 条样本全部 `来源=rule`**（规则降级，不是模型 L3）：

```
冲突度=1.1；人物知名度=1.0；时效性=2.4；话题延展性=3.0；粉丝画像匹配度=3.0
```

规则模式下「冲突度 / 人物知名度」恒为 1，总分差异全部来自时效性（1/3/5）。

这意味着两件事：

1. **在规则降级模式下，社媒讨论量确实是唯一未启用的区分维度**——这是我前面说它「有说服力」的依据。
2. **但正常 L3 模型模式的分数从来没留证**（rank_audit 只有 rule 样本）。也就是说：在不知道模型模式排序质量的前提下讨论「补信号能提升多少」，等于在黑箱上打补丁。

这正是我们自己记过的那条教训——**「有结论没留证据的环节都是黑箱」**。所以顺序应该是：先让 L3 分数落 audit → 看信号缺失对排序的实际影响 → 再决定要不要补、补哪个。

### 6.5 决策矩阵

| # | 接入点 | 收益 | 成本 | 风险 | 结论 |
|---|---|---|---|---|---|
| A | 社媒讨论量信号源（替代/补充 RedFox） | **低**（+1 分封顶，跨平台口径） | 中高（登录态+跟版） | **高**（许可违约+设备关联+账号封禁） | ❌ 不做 |
| B | 评论洞察（读者语言/误解/争议点） | 中（可反哺标题锚点与读者画像） | 中 | **中高**（个人信息，评论含 ID/昵称） | ⏸ 暂缓，先解决许可 |
| C | 选题发现（哪个球员今天在抖音火了） | 中（补「同题报道」缺失） | 中 | 中 | 🟡 只做人工情报，不做自动化 |
| D | **工程范式：CDP 登录态 + 错误分类** | **中高** | **低** | **零** | ✅ **推荐，本周做** |

### 6.6 为什么 D 才是真收益

**D-1：CDP 复用真实浏览器登录态**

我们现在的做法（`batch.yml`）：把 `toutiao_auth.json` 从 secret 写进工作区，Playwright 加载 storage_state 去 `mp.toutiao.com`。

痛点很清楚：**登录态是快照，会失效**。失效那天的表现是「导出页只剩空壳」——我们为此写过 `_wait_export_ready`(20s 轮询) + 单页重载重试 + `requestfailed` 进证据，那是**在给一个必然失效的快照打补丁**。

MediaCrawler 的 CDP 模式给了另一条路：连一个**长期在线的真实浏览器**，登录态由用户自己维护（扫码一次，长期有效），程序只是「借用」。对我们本地跑的场景（发布、导出、对标），这能把「登录态过期」从故障降级为日常。

注意：**不是照搬代码，是照搬这个拓扑**。用 `playwright.connect_over_socket` / `connect_over_cdp` 连本地 Chrome 只需几十行。

**D-2：被拦要显式区分**

`media_scraper.py` 现在的懂球帝分支：遇到 EdgeOne 的 HTTP 567 挑战页，不在 403/429/404 分支里 → `return ""`。表现为「备源 0 篇」，看不出原因。

MediaCrawler 这边是 `except DataFetchError: break` + 一行日志——**同一个病**。但它至少把异常分了类（`DataFetchError` / `IPBlockError` / `NoteNotFoundError` / `PlatformAccessError`）。

正确做法（两家都该做）：**抓取返回值必须能回答「真空数据 / 被风控 / 网络故障 / 解析失败」这四问**，落台账，而不是统一返回空。

---

## 7. 具体接入思路（如果你决定做 A/B，这是唯一安全形态）

前提：**必须先拿到作者书面同意**，或改用平台开放平台。没有这一步，下面全是空谈。

### 7.1 运行隔离（不进 CI，不进主仓依赖）

```
external/mediacrawler/          # git submodule 或 .gitignore 的独立 clone
  ├── .venv/                    # 独立 venv，不污染系统 python
  └── （上游原样，不改造）

scripts/collect_social_signals.py   # 我们的适配层，唯一入口
```

- **不 import 它的任何模块**，只用 `subprocess` 调 `uv run main.py --platform dy ...`
- **不写进 `requirements.txt`**（会污染 CI 的 `plan-invariants-verify`）
- 上游更新不影响我们；我们只依赖它的 CLI 与 jsonl 输出契约

### 7.2 数据契约：只落聚合值

`data/signals/social_signals.json`：

```json
{
  "generated_at": "2026-11-03T07:10:00+08:00",
  "collector": {"name": "mediacrawler", "version": "main@2026-10-27", "platform": "dy"},
  "coverage": {"requested": 71, "hit": 58, "skipped_by_breaker": 0},
  "signals": {
    "姆巴佩": {
      "社媒讨论量": 519066,
      "作品数": 22,
      "采集时间": "2026-11-03T07:02:11+08:00",
      "口径": "dy_search_top20_digg_sum"
    }
  }
}
```

**硬性不落盘**：正文、作者昵称、用户 ID、头像、评论原文、任何媒体文件。

只要聚合数（点赞和），它就从「内容抓取」退化为「热度统计」，风险等级完全不同——而且这也正好符合我们已有的纪律：**「原始信源只作信号，绝不进正文」**。`source_watch.py` 的 docstring 已经写死这条，接入层必须遵守。

### 7.3 频控与熔断

| 项 | 值 | 理由 |
|---|---|---|
| 单实体查询 | 1 次/天 | 71 个实体 × 1 页 ≈ 与人工浏览同量级 |
| 单日上限 | ≤ 80 次真实请求 | 硬闸，超了就用昨日缓存 |
| 单页条数 | 20 | 上游默认 15，够取互动量中位数 |
| 连续风控 | 3 次验证码/空返 → 停 24h 并告警 | 不硬撑，硬撑必封 |
| 出口 | 独立网络（不与头条发布同 IP 同设备） | 见 5.4 |

### 7.4 接 ranker 的正确姿势

**不要动 `_SOCIAL_FAME_BANDS`**。它是 10-13 用真数据标定的，只给 +1 是有理由的（避免把 C 级主体抬到 B 级）。

接进来只是**把覆盖率从 0/71 提到 ~58/71**，让那 +1 分有机会真正生效。并且在 `l2_enrich` 已有的 `_signal_missing` 机制下，缺失依然如实标记——**绝不用 0 冒充**。

同时必须加一条口径隔离注释：**抖音互动量 ≠ 头条图文阅读量，不进 CTR 归因**。这是我们 `benchmark_peers` 已经付过学费的地方（视频号 `N播放` 与图文 `N阅读` 必须分开算）。

### 7.5 验收脚本（`scripts/verify_social_signals.py`）

四条硬项：

1. **覆盖率**：`hit / requested ≥ 0.6`，且 `requested` 必须等于 `entity_map` 的活跃实体数（防止悄悄只查 12 个关键词就算过关——RedFox 现在就是这个状态）
2. **无污染**：产物里不得出现长度 > 40 的字符串字段（正文泄露检测），不得出现 `user_id` / `nickname` / `avatar` 键名
3. **口径声明**：每条记录必须带 `口径` 与 `采集时间`，缺任一判 FAIL
4. **负向验证**：篡改 1 个实体把互动量改成 0 或负数，校验必须报错

接入 `plan-invariants-verify.yml`，不软化。

### 7.6 接入顺序（不要跳）

```
M0  补 L3 分留证：rank_audit 扩到模型分数（现在只有 rule 样本）
    ↓ 看「社媒讨论量缺失」到底影响了多少排序，有数据再决策
M1  拿许可（书面同意 / 或改用平台开放平台）
    ↓
M2  collect_social_signals.py + 契约 + 频控 + 熔断 + verify
    ↓
M3  连续跑 14 天，看覆盖率稳定性与封控频率
    ↓
M4  才轮到讨论要不要调 _SOCIAL_FAME_BANDS
```

**M0 是免费的，而且不管最后做不做都该做。**

---

## 8. 收益量化

不编数字，给可测口径与区间：

| 指标 | 现状 | 补齐后（预估） | 依据 |
|---|---|---|---|
| 社媒讨论量覆盖率 | 0 / 71 | ~58 / 71（≈82%） | 假设 80% 实体在抖音有可检索作品 |
| 排序影响 | 0 | **每篇最多 +1 分 / 满分 25** | `_SOCIAL_FAME_BANDS` 单档 |
| 内容质量影响 | — | **0** | 纪律：信号不进正文 |
| CTR 影响 | — | **不可归因** | 跨平台口径，且当前主瓶颈是封面（无封面 CTR 0.52% vs 有封面 2.07%） |
| 新增运维成本 | — | 每天 1 次人工确认 + 跟版维护 | 无断点续爬、单账号、9 月 8 次风控 fix |
| 新增风险 | — | 许可违约 / 账号封禁 / 设备关联 | 见第 5 章 |

**结论：投入产出比不成立。** 封面问题带来的 CTR 差是 4 倍，而这条链路的上限是「排序 +1 分」。在封面还没压到 5% 以下之前，把工程时间投到社媒抓取上是错配。

---

## 9. 风险清单

| # | 风险 | 可能性 | 影响 | 缓解 |
|---|---|---|---|---|
| 1 | 许可违约（商业使用非商业许可） | **高**（默认就是） | 中（民事赔偿/下架） | 拿书面同意，或不用 |
| 2 | 发布账号被平台关联风控 | 中 | **高**（账号是唯一资产） | 独立设备/网络；最好别在同机跑 |
| 3 | 抓取账号被封 | **高**（官方 FAQ 自己说「多半是触发风控」） | 中 | 低频+熔断；但封了就得换号 |
| 4 | 平台改版导致失效 | **高**（9 月三天 8 个 fix） | 中 | 契约化输出 + 覆盖率告警；但上游已停止跟版 |
| 5 | 个人信息（评论含 ID/昵称） | 中（若做 B） | 中高 | 只落聚合值，不落评论原文 |
| 6 | 幻觉式「有信号」 | 中 | 中 | 沿用 `_signal_missing`，绝不 0 冒充 |
| 7 | 口径混算污染 CTR 归因 | 中 | 中 | 明确注释：抖音互动量不进 CTR 归因 |
| 8 | 把工程时间投错地方 | **高** | 中 | 先做 M0（免费）+ 先修封面 |

---

## 10. 最终建议

**立刻做（本周，零风险）**

1. `media_scraper.py` 的懂球帝分支：567 挑战页不再 `return ""`，改为显式分类落台账（`blocked_by_challenge`）。
2. 评估把 `publisher.py` / `collect_toutiao_exports.py` 从「secret 注入快照」迁到「CDP 连本地常驻 Chrome」（仅本地运行场景）。

**等 11-16 复盘后再定（有条件）**

3. 若封面压到 ≤5% 后 CTR 仍不动 → 说明瓶颈不在封面也不在标题，那时的候选是「选题本身不对」。**只有到这一步，社媒信号才值得花钱花风险去买**。
4. 在此之前补 M0：让 L3 模型分数落 `rank_audit`，别在黑箱上做决策。

**不做**

5. 不把 MediaCrawler 接进 CI，不写进 `requirements.txt`。
6. 不抓正文、不抓评论原文、不下载媒体。这条红线一旦破，我们自己的 `compliance_guard` / `differentiation` / 反洗稿三道闸门就全部失去意义——**用洗稿工具生产「原创」内容，是这个项目最不该出现的事故**。

---

## 附录：证据与复现

> **落地情况（2026-11-03 当天）**：本文「立刻做」的两件事已经做了——
> D-2 采集失败分类（`scraper_diagnostics.py` + `media_scraper` 改造 + `verify_scraper_diagnostics.py`）
> 与 M0 L3 打分留证（`ranker` 稳定主键 + `l3_scores.jsonl` + `verify_rank_audit_persistence.py`）。
> 单测 1325 → 1357，两个验收脚本已进 `plan-invariants-verify.yml` 硬门禁。
> **未做**且刻意不做的：CDP 登录态迁移（需实机验证，先留着）、社媒信号接入、任何内容抓取。
>
> M0 落地时顺带定位到两个具体 bug（详见第 7.6 节顺序里的 M0）：
> ① L4 缓存键是 `topic-{i}` 批内序号 → 同一天第二批的 `topic-0` 会复用第一批的分数，
> 且「命中缓存不花 token」所以日志上看不出来；② 缓存按日重置 → L3 分数每天被覆盖丢弃，
> 周审计永远读不到模型样本。这两条合起来解释了「L3 从未留证」的错觉。

```bash
# 仓库元数据
curl -s https://api.github.com/repos/NanmiCoder/MediaCrawler

# 代码规模 / 结构
git clone --depth 1 https://github.com/NanmiCoder/MediaCrawler
find . -name "*.py" | xargs wc -l | tail -1        # 32504

# 关键实现
config/base_config.py                              # 默认配置（CDP/限速/存储）
media_platform/xhs/playwright_sign.py              # xhshow 纯算法签名
media_platform/douyin/help.py                      # execjs 签名 + 已废弃的 playwright 方案
media_platform/tieba/core.py                       # 反检测注入脚本（完整样本）
LICENSE                                            # NON-COMMERCIAL LEARNING LICENSE 1.1
docs/常见问题.md                                    # 官方自己承认的风控问题

# 我们的现状
data/signals/external_signals.json                 # 71 实体：社媒讨论量 0、搜索指数 69/71 饱和
data/signals/redfox_signals.json                   # 12 关键词，2026-10-13 后未刷新
data/redfox_budget.json                            # {"2026-10-13": 12}
ranker.py:_SOCIAL_FAME_BANDS                       # ((200_000, 1),) 单档 +1
data/rank_audit/audit_2026-10-10.md                # 10 条样本全部 来源=rule
```
