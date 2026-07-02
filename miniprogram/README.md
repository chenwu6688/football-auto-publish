# 球评人老六 — 微信小程序

> 基于 GitHub CDN 的零成本小程序。每天自动同步今日文章。

---

## 你需要做的（一次性，约 1 小时）

### 1. 确认小程序已注册

你已经注册好了，AppID: `wx8ae0659f2c822e4b`

如未注册：微信公众平台 → 注册 → 小程序 → 个人主体（身份证即可）

### 2. 添加 CDN 域名到白名单

小程序后台 → 开发 → 开发设置 → **request 合法域名**

添加：
```
https://cdn.jsdelivr.net
```

### 3. 下载微信开发者工具

https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html

安装后导入本目录（`miniprogram/`），确认能正常预览。

### 4. 上传 + 提交审核

开发者工具 → 工具栏"上传" → 填写版本号 → 上传成功
登录小程序后台 → 版本管理 → 提交审核 → 等待 1-7 天

---

## 我能做的（全部代码已完成）

| 模块 | 状态 |
|------|------|
| `scripts/export_static_data.py` | ✅ 管线导出脚本 |
| `miniprogram/project.config.json` | ✅ 项目配置（已填入你的 AppID） |
| `miniprogram/app.js` | ✅ 更新检查 |
| `miniprogram/app.json` | ✅ 页面路由 + 窗口配置 |
| `miniprogram/app.wxss` | ✅ 全局样式 |
| `miniprogram/utils/api.js` | ✅ CDN 请求封装 |
| `miniprogram/pages/index/` | ✅ 首页 Feed（批次 Tab + 文章卡片列表） |
| `miniprogram/pages/article/` | ✅ 文章详情页（rich-text 渲染 + 金句展示 + 分享） |

---

## 数据流说明

```
管线完成后
     │
     ├── 自动运行 export_static_data.py
     │    ├── 读取 output/{date}/*.md
     │    ├── 转 HTML + 替换图片为 CDN URL
     │    └── 写入 static_data/today.json
     │
     ├── git add + commit + push
     │    ├── output/{date}/  (文章 + 图片)
     │    └── static_data/   (小程序数据)
     │
     └── jsDelivr CDN（~2-5 分钟生效）
          └── 小程序读取 today.json → 展示
```

---

## 管线改造说明

`scripts/export_static_data.py` 已被添加到 GitHub Actions 工作流中：

- 每个批次（morning/noon/evening）完成后自动运行
- 幂等设计：多次运行不会重复/覆盖
- 如果某批次还没生成，只导出已有内容
- 小程序会显示已有批次，未生成的批次不显示

---

## 状态说明

| 小程序显示 | 含义 |
|-----------|------|
| 今日内容正在路上 | 早上第一批还没生成（通常 8-10 点出现） |
| 全部 | 所有已生成的文章 |
| 晨读/午间/晚间 | 按批次筛选 |
| 加载失败 | 网络问题，下拉刷新重试 |

---

## 自定义修改

如果你想修改：
- **小程序标题/颜色** → 改 `app.json` 的 `navigationBarTitleText` 和 `navigationBarBackgroundColor`
- **CDN 地址** → 改 `utils/api.js` 和 `app.js` 的 `CDN_BASE`
- **文章样式** → 改 `pages/article/article.wxss` 的 `rich-text` 样式
- **赞赏方式** → 改 `pages/article/article.js` 的 `showReward` 函数，换成你的赞赏码图片
