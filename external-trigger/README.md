# external-trigger · Cloudflare Workers 外部定时触发

用 Cloudflare Cron Triggers 在准点调用 GitHub `workflow_dispatch`，
绕开 GitHub 免费调度器在 UTC 00:00 的拥堵，保证三班发布准点。

## 为什么需要它

GitHub Actions 免费调度器在 UTC 00:00（全球零点）严重拥堵，
映射到 UTC 00:xx 的**晨读批次会被延迟 1~2 小时甚至被整体跳过**
（实证：2026-08-27 晨读批次 4 个 cron 窗口全部未触发，靠手动 dispatch 补救）。

> 历史延迟对照（cron UTC → 实际触发，越接近 UTC 零点延迟越大）：
> - 晨读 `00:xx` → 实际延迟 0~2 小时（最严重）
> - 午间 `04:xx` → 实际延迟 ~40~67 分钟
> - 晚间 `09~10` → 实际延迟 ~30~40 分钟

`batch.yml` 已带**幂等保护**：同一批次多次触发只发布一次，
因此本触发器与原 GitHub `schedule` 可并存、互为兜底，**不会重复发文**。

## 文件

| 文件 | 作用 |
|---|---|
| `worker.js` | 调 GitHub dispatch API 的 Worker 逻辑（按 cron 映射批次） |
| `wrangler.toml` | Worker 配置 + 3 个 cron trigger（免费上限，刚好三班） |

## 部署步骤

1. 安装 wrangler：
   ```bash
   npm install -g wrangler
   ```
2. 登录 Cloudflare（浏览器授权你的 CF 账号）：
   ```bash
   wrangler login
   ```
3. 配置 GitHub Token（**Fine-grained PAT**，权限最小化）：
   - GitHub → Settings → Developer settings → Fine-grained PAT
   - 仅授权仓库 `chenwu6688/football-auto-publish`
   - Repository permissions → **Actions → Read and write**
   - 复制生成的 token
   ```bash
   wrangler secret put GH_TOKEN
   # 粘贴上面的 token；secret 存于 CF，不落地第三方
   ```
4. 部署：
   ```bash
   wrangler deploy
   ```
5. 验证：
   - Cloudflare 控制台 → Workers & Pages → `gh-batch-trigger` → **Triggers**
     应看到 3 个 cron（UTC `00:07` / `04:07` / `09:37`）
   - 下次触发后，GitHub Actions 会出现一条 `event=workflow_dispatch` 的 run
   - 想立即验证：部署后在 CF 控制台手动 **Run** 一次，或临时把某个 cron 改成近时间点

## 与原 GitHub schedule 的关系

- **默认（推荐）**：保留 `batch.yml` 的 `on.schedule` 作为兜底——
  万一 CF 抽风，GH 仍会跑（可能晚点），幂等保证不重复。
- **完全交外部**：删除 `batch.yml` 的 `on.schedule` 整个块，仅保留 `workflow_dispatch`。
- 两种情况下幂等保护都生效，不会重复发布。

## 时区换算表（Cloudflare 仅 UTC）

| 批次 | CST | UTC（wrangler crons） |
|---|---|---|
| 晨读 | 08:07 | `7 0 * * *` |
| 午间 | 12:07 | `7 4 * * *` |
| 晚间 | 17:37 | `37 9 * * *` |

## 安全须知

- **禁止**使用带全账号权限的经典 PAT；务必用 Fine-grained PAT 且只授权本仓库 Actions:write。
- `GH_TOKEN` 以 CF secret 形式存储，不写入任何文件、不暴露给 cron-job.org 等第三方。
- 本目录不含任何真实凭据，可安全提交到仓库。
