# 外部项目评估：NanmiCoder/NewsCrawler 是否适用于本项目

评估日期：2026-11-04
评估对象：https://github.com/NanmiCoder/NewsCrawler （638 star / 116 fork / GPL-3.0）
评估方法：读目录树 + 核心实现（base/fetchers/models/detector/extractor/toutiao adapter）+ 用它自己的 URL 规则跑我们的真实采集链接 + 对照我方采集台账

---

## 一、结论

**不能作为依赖引入，但有两个技术点值得拿、一个场景可以自己实现。**

| 判定 | 内容 |
|---|---|
| ❌ 不引入 | ① **定位错位**：它是「给一条 URL，提取一篇正文」，我们主链路是「列表发现 → 选题 → 生成 → 发布」；② **足球领域实测零覆盖**；③ **GPL-3.0**，我们仓库 public 且无 LICENSE，代码进来即构成分发 |
| ⚠️ 不直接用 | 它自带的头条 adapter 里**硬编码了一个完整的头条登录 Cookie**（明文提交在 public 仓库）。不卫生、且大概率已失效，用它等于借别人的账号身份 |
| ✅ 值得拿 | **`curl_cffi` 的 `impersonate`（TLS 指纹伪装）**——我们全仓 grep 无一处，而今天采集台账里 **challenge 占 19/75（25%）** |
| ✅ 值得拿 | **有序 `contents` 模型**（text/image/video 按原文顺序），比我们「正则抠 img 塞一个 list」更保真 |
| 🔧 可自己做 | 抓竞品/对标头条文章正文（30 行 xpath 的事，不值得为它背 GPL） |

---

## 二、它是什么（实测）

| 项 | 实测值 |
|---|---|
| 规模 | 195 个文件、76 次提交、2024-11 建仓、最后 push 2026-08-08 |
| 语言 / 许可 | Python / **GPL-3.0**（有 LICENSE 文件） |
| 平台覆盖 | 12 个：微信、头条、网易、搜狐、腾讯、BBC News、CNN、Twitter/X、Naver、Detik、Quora、Lenny's Newsletter |
| 核心抽象 | `BaseNewsCrawler(new_url: str)` → 抽象方法只有 `parse_content(html)` 与 `get_article_id()`；`ExtractorService.extract_news(url, platform, cookie)` 是唯一服务入口 |
| **是否有「列表/频道发现」** | **没有。** 输入永远是一条文章 URL，输出永远是一篇 `NewsItem` |
| 测试 | **零**。README 写 `uv run pytest`，但仓库内除 `news_crawler/quora/test/test.py`（手工脚本）外**没有任何测试文件** |
| 网络层 | `requests` 与 `curl_cffi` 双策略；`tenacity` 重试 3 次、固定间隔 1s；**无随机抖动、无代理、无频率控制** |

它的定位写得很清楚（SKILL.md）：`description: 新闻站点内容提取`，输出 JSON/Markdown 给 Agent 用。
**它解决的是「把一条网页变成结构化正文」，不是「今天有什么足球新闻」。**

---

## 三、最有说服力的实测：我们一个源都不命中

把它的 `PLATFORM_PATTERNS`（detector.py 原样）跑我们真实的采集链接：

| 场景 | URL | 命中 |
|---|---|---|
| 直播吧首页 | `https://www.zhibo8.cc/` | **None** |
| 直播吧战报 | `https://news.zhibo8.com/zuqiu/2026-11-03/match123456.htm` | **None** |
| 懂球帝首页 | `https://www.dongqiudi.com/` | **None** |
| 懂球帝文章 | `https://www.dongqiudi.com/article/123456.html` | **None** |
| BBC **Sport** | `https://www.bbc.com/sport/football/articles/c123...` | **None**（规则只认 `bbc.com/news/articles/`） |
| Sky Sports | `https://www.skysports.com/football/news/11095/13123456` | **None** |
| ESPN | `https://www.espn.com/soccer/report/_/gameId/704321` | **None** |
| transfermarkt | `https://www.transfermarkt.com/...` | **None** |
| 对标头条文章 | `https://www.toutiao.com/article/7629689292301926952/` | toutiao ✓ |
| 头条移动端分享 | `https://m.toutiao.com/article/7629...` | **None**（`re.match` 前缀匹配，不认 m. 子域） |
| 微信文章 | `https://mp.weixin.qq.com/s/AbCd...` | wechat ✓ |

**11 个样本里，属于足球的任何一条都是 None。** 唯一命中的两类（头条、微信）恰好是「我们发出去的地方」而不是「我们采集的地方」。

---

## 四、许可证约束（决定性）

它用 **GPL-3.0**。我们仓库 **public + 无 LICENSE** ——这正是既定的那条纪律：把 GPL 代码 commit 进来即构成分发，整仓都得跟着改 GPL。

所以边界很明确：

- ❌ 不复制它的 `.py` 进本仓库（哪怕只有一个 adapter）
- ❌ 不把 `.claude/skills/news-extractor` 整目录 vendor 进来
- ✅ 可以**装 `curl_cffi`**（MIT，与它的 GPL 无关）——用第三方依赖不等于分发它的代码
- ✅ 可以**学结构**（事件顺序、重试语义、adapter 划分），思想不受版权保护
- ⚠️ 若真要用它跑一次提取，只能用「本地命令行调用、不进仓库、不随产物分发」的方式，且不能改造成我们流水线的一部分

---

## 五、值得拿的两个技术点

### 5.1 `curl_cffi` 的 TLS 指纹伪装（有硬数据支撑）

它把抓取做成策略模式：`RequestsFetcher` / `CurlCffiFetcher`，后者支持 `impersonate="chrome124"` 之类，
用 curl 的 TLS 指纹发起请求。**我们全仓 grep `curl_cffi|impersonate|ja3|tls` → 零命中**，只有裸 `requests`。

今天的采集台账（`data/audit/scraper_fetch_latest.json`，12:05）：

```
总 75 次请求：ok 56 / challenge 19 / blocked 0 / network 0 / http_error 0 / parse 0
  http 通道     ok 39 / challenge 17
  dongqiudi     ok 17 / challenge 2
```

**25% 的请求打在「200 挑战壳」上**，其中 http 通道 17 次。这类拦截一部分正是靠 TLS/JA3 指纹识别
（`requests` 的 TLS 指纹是 Python 的，一眼假），换成浏览器指纹有机会直接过。

**但这个假设必须先实测再采信**：同一个 URL（`https://www.zhibo8.cc/`）既出现 ok 也出现 challenge，
说明更像**频率风控**而不是纯指纹识别——如果是频率风控，`curl_cffi` 一样过不去。
所以正确做法是 **A/B 各打 50 次同 URL 对比 challenge 率**，而不是直接换掉 `_http_get`。

### 5.2 有序 `contents` 模型

它的 `NewsItem` 是 pydantic 模型：

```python
contents: List[ContentItem]  # type ∈ {text, image, video}，按原文顺序
# texts / images / videos 由 model_post_init 从 contents 派生
```

我们现在的 `source_images` 只回答「有哪些图」，**不回答「图在哪两段之间」**。这有个具体后果：
上一轮评估的那个发布 skill 强调「配图要放在小标题上方」——**没有顺序信息，这条判据根本无法校验**。

不需要它的代码（50 行的模型而已），但 `contents` 保序、`texts/images/videos` 派生这两个设计可以直接照做。

---

## 六、不能用的地方（逐条）

| 它有什么 | 为什么不能用 |
|---|---|
| 头条 adapter 的解析 | `parse_html_to_news_content` 用 `//article/*` 取正文、`img/@src` 取图，**不处理懒加载 `data-src`**；且该类里 `FIXED_COOKIE` 硬编码了作者本人的完整头条登录态（含 `tt_webid`/`sso_uid` 等），明文提交在 public 仓库。不卫生、会借别人身份、2024 年的 Cookie 到 2026 年基本已废 |
| BBC / CNN adapter | 只认 `bbc.com/news` 与 `edition.cnn.com`，与足球无关 |
| Twitter/X adapter | 技术上可双模式（公开推文免认证 / Cookie 认证），但它是**单条推文提取**，不是按账号/关键词拉时间线——对「转会信号源」用途不够 |
| 网络层 | 固定 1s 间隔、3 次重试、无抖动无代理。我们的 `scraper_diagnostics` 已经按成因分了 7 类（ok/empty/blocked/challenge/network/http_error/parse），比它细；退回去是降级 |
| 测试 | 零测试。638 star 不代表可依赖——这类项目正是「star 数最会骗人」的例子（我们自己的筛选维度里热度只占 20 分） |
| 视频素材模块（`video_crawler/` 里 pexel/pixabay/mixkit） | 是「素材下载」不是新闻；我们图源刚接入 Pexels，视频素材若要用应另评，不与该仓库耦合 |

---

## 七、建议

1. **P0（1 小时，有数据支撑）**：给 `_http_get` 加一条 `curl_cffi` 通道做 A/B，同 URL 各 50 次对比 challenge 率。
   - 若 challenge 率显著下降 → 接成 fallback（`requests` → `curl_cffi` → 最小头通道），并进 `scraper_diagnostics` 的归因
   - 若无改善 → 结论是「频率风控」，转去做退避与错峰，不要在这条路上继续投
   - **注意**：`curl_cffi` 是 MIT 依赖，装它不涉及该仓库的 GPL
2. **P1（半天）**：给 `source_images` 补顺序——不用它的代码，按 `contents` 保序思路自己实现，
   这样「配图位置在小标题上方」才有校验对象（也是上一轮那个发布 skill 唯一能落地的结构判据）。
3. **P2（可选）**：竞品/对标头条文章正文提取，自己写 xpath（我们已有 `benchmark_peers` 抓列表，缺正文）。
   **不要**引它的 adapter（GPL + 硬编码 Cookie + 脆弱 xpath）。
4. **不做**：不把它当采集主干、不 vendor、不接 MCP/FastAPI/WebUI（那套是给别的使用场景准备的）。

---

## 八、未验证项

- `curl_cffi` 能否真降 challenge 率：**未验证**，必须 A/B 才知道（同 URL 结果不一致指向频率风控，这是主要风险）。
- 它的 12 个 adapter 当前是否还能跑：**未验证**（本机不通境外站；且硬编码 Cookie 已过期，头条 adapter 大概率直接失败）。
- 视频素材模块是否对我们的 video_pipeline 有价值：**未验证**，需要单独评估，不在本轮范围。
