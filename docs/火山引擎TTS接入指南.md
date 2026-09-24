# 火山引擎 TTS 接入指南（真·抖音音色）

> 目标：把双人对话的 edge-tts 音色，换成**字节系原生的抖音/豆包同款音色**。
> 代码已就绪（单人 + 双人链路都通），你只需拿 key 填 4 个字段。
> 全程不写代码，纯控制台复制粘贴。

---

## 一、控制台操作（约 5 分钟）

### 1. 开通服务
1. 登录 [火山引擎控制台](https://console.volcengine.com/)（抖音/头条账号可直接扫）。
2. 左侧搜索 **「语音技术」→「语音合成（大模型）」**，或直接进
   [语音技术应用列表](https://console.volcengine.com/speech/app)。
3. 点 **「创建应用」**，随便起个名（如 `football-tts`），勾选 **语音合成** 服务。

> ⚠️ 语音合成有几个不同的产品线：
> - **语音合成（大模型）** ← 本项目的 `voice_type` 形如 `zh_male_xxx_bigtts` 走这里
> - **语音合成（小模型）** ← 老的 `BV700_streaming` 系列走这里
> 两条线 **cluster 不同**，填错会报 `Fail to feed text, reason Init Engine Instance failed`。

### 2. 拿 3 个鉴权字段
在应用详情页，复制这 3 个值：

| 字段 | 控制台位置 | 写入配置 |
|---|---|---|
| **AppID** | 应用列表「App ID」列 | `volcano.app_id` |
| **Access Token** | 应用详情「Access Token」 | `volcano.token` |
| **Cluster** | 按产品线选（见下表） | `volcano.cluster` |

| 产品线 | cluster 值 |
|---|---|
| 语音合成（大模型，`*_bigtts`） | `volcano_tts` |
| 语音合成（小模型，`BV*_streaming`） | `volcano_tts` |
| 声音复刻 2.0 | `volcano_icl` |

> 99% 的场景填 `volcano_tts` 就对。若报 cluster 错误，去
> [控制台 FAQ-Q1](https://docs.volcengine.com/docs/6561/196768) 对一下。

### 3. 下单音色（关键，别漏）
大模型音色 **除了 `BV001_streaming` / `BV002_streaming` 两个免费音色外，
其余都要在控制台「音色管理」里下单（付 0 元即开通）**。
没下单会报：`access denied ... 需要在控制台购买该音色才能调用`。

操作：**语音合成 → 音色管理 → 找到目标音色 → 点「添加/下单」→ 支付 0 元**。

---

## 二、推荐音色（挑流量向的）

以下是官方音色表里带 **「抖音同款 / 豆包同款 / 剪映同款」** 标签、适合体育吐槽的：

### A 角色（主播 · 男声）
| 音色名 | voice_type | 特点 |
|---|---|---|
| **京腔侃爷** | `zh_male_jingqiangkanye_moon_bigtts` | 京味儿侃大山，最像"老六" |
| **浩宇小哥** | `zh_male_haoyuxiaoge_moon_bigtts` | 年轻爽利，语速感强 |
| **解说小明** | `zh_male_jieshuoxiaoming_uranus_bigtts` | 专业解说腔，适合讲数据 |
| **阳光青年** | `zh_male_yangguangqingnian_uranus_bigtts` | 明快热血 |
| **反卷青年** | `zh_male_fanjuanqingnian_uranus_bigtts` | 吐槽感强，有梗 |
| **擎苍 2.0** | `zh_male_qingcang_uranus_bigtts` | 抖音同款，低沉磁性 |

### B 角色（搭档 · 女声）
| 音色名 | voice_type | 特点 |
|---|---|---|
| **爽快思思** | `zh_female_shuangkuaisisi_moon_bigtts` | 爽快抢话，配 A 有火药味 |
| **直率英子 2.0** | `zh_female_zhishuaiyingzi_uranus_bigtts` | 抖音同款，直率不装 |
| **开朗姐姐** | `zh_female_kailangjiejie_uranus_bigtts` | 开朗接梗 |
| **林潇 2.0** | `zh_female_linxiao_uranus_bigtts` | 抖音同款，情绪饱满 |

> 完整音色表见 [官方音色列表](https://www.volcengine.com/docs/6561/1257544)。
> 带 `_moon_bigtts` 的是较新的情感音色，带 `_uranus_bigtts` 的是 2.0 指令遵循音色。
> **试听**：控制台音色管理页每个音色都有「试听」按钮，先听再定。

---

## 三、填配置（`video_pipeline/video_config.yaml`）

```yaml
voice:
  provider: "volcano"          # 单人模式也走火山

volcano:
  enabled: true
  app_id: "你的AppID"
  token: "你的AccessToken"
  cluster: "volcano_tts"
  speaker: "zh_male_jingqiangkanye_moon_bigtts"     # 单人模式 / A 角色
  speaker_b: "zh_female_shuangkuaisisi_moon_bigtts"  # B 角色（双人对话用）

textmotion:
  dialogue:
    enabled: true
    engine: "volcano"          # 双人对话走火山（不填则跟随 voice.provider）
    volcano_voices:
      A: "zh_male_jingqiangkanye_moon_bigtts"
      B: "zh_female_shuangkuaisisi_moon_bigtts"
    rate_map:                  # 火山会自动换算成 speed_ratio
      A: "+22%"
      B: "+20%"
    gap_ms: 40
```

填完直接跑管线即可，**不用改代码**。

---

## 四、验证清单

跑之前，可以先单独验一条：

```bash
cd /workspace/football-auto-publish
python3 - <<'PY'
import sys; sys.path.insert(0, '.')
from pathlib import Path
from video_pipeline import tts
ap, sp, used = tts.synthesize_volcano(
    "皇马三比一逆转巴萨，姆巴佩又炸了！",
    app_id="你的AppID", token="你的Token", cluster="volcano_tts",
    speaker="zh_male_jingqiangkanye_moon_bigtts",
    audio_path=Path("/tmp/volcano_test.wav"), srt_path=Path("/tmp/volcano_test.srt"),
    rate="+20%")
print("产出:", ap, "音色:", used)
print(Path(sp).read_text(encoding="utf-8")[:200])
PY
```

- ✅ 听到 `.wav` 有声音 + `.srt` 有时间轴 → 通了
- ❌ 报错对照下表

### 常见错误速查

| 报错 message | 原因 | 解决 |
|---|---|---|
| `requested grant not found` | AppID/Token 填错 | 重新复制，注意别带空格 |
| `Fail to feed text, reason Init Engine Instance failed` | cluster 或 voice_type 错 | cluster 填 `volcano_tts`；确认音色 ID 拼写 |
| `access denied` | 音色没下单 | 控制台音色管理里 0 元下单 |
| `quota exceeded for types: xxx_lifetime` | 试用额度用完 | 控制台开通正式版 |
| `illegal input text!` | 文本只有标点/空了 | 检查口播稿 |
| `文本长度超限`（code 3010） | 单次 > 1024 字节 | 拆句（双人对话每 turn 天然较短，一般不会） |

---

## 五、计费与限额（心里有数）

- **定价**：约 **1.3 元 / 千字**（大模型音色），新用户有免费试用额度。
- **单次上限**：1024 字节（UTF-8，约 340 个汉字）；双人对话按 turn 拆，每 turn 都很短，安全。
- **HTTP 超时**：服务端 60s，本客户端默认 60s 超时。

> 按一条 60 秒短视频、约 400 字算，成本 ≈ **0.5 元/条**。量大可谈折扣。

---

## 六、实现说明（给将来维护的自己）

代码位置：`video_pipeline/tts.py`

- `synthesize_volcano(...)`：单人合成
  - 组装 `{app, user, audio, request}` 请求体，`Authorization: Bearer;{token}`（**分号**，不是空格）
  - 返回 `data` 是 base64 mp3 → 用 ffmpeg 转 **24k 单声道 WAV**（与 edge 分支口径一致）
  - 解析 `addition.frontend` 里的**词级时间戳** → 写 `.words.json` + SRT
  - 若音色不返回时间戳 → 按字符均分兜底，保证字幕不为空
- `synthesize_dialogue_volcano(...)`：双人对话，每 turn 一个 `voice_type`
  - 逐 turn 合成 → **filter_complex concat 重编码拼接**（规避 mp3-as-wav 时长错乱）
  - 时间轴整体偏移合并 → `.dialogue_segments.json`（带 `speaker`，供双色渲染）
- `pipeline.py`：`dialogue.engine=volcano` 时自动走火山双人分支；失败回退 edge。

**测试**：`tests/test_video_pipeline.py` 有 8 个火山用例（mock HTTP，不消耗额度），
覆盖请求体/鉴权头/输出格式/兜底/错误码/双人拼接/管线选路。跑：

```bash
PYTHONPATH=/workspace/football-auto-publish python3 -m pytest tests/test_video_pipeline.py -q -k volcano
```
