#!/usr/bin/env bash
# cron-job.org 精确调度 → GitHub Actions batch.yml
# 主调度方案，精度 <1 分钟。GitHub Actions 原生 cron 为 30 分钟后备份。
#
# 用法:
#   bash scripts/setup-cronjob.sh test   # 测试 API 调用
#   bash scripts/setup-cronjob.sh show   # 显示 cron-job.org 配置

set -e

REPO="chenwu6688/football-auto-publish"
API="https://api.github.com/repos/${REPO}/actions/workflows/batch.yml/dispatches"
SCRIPT_DIR="$(cd "$(/usr/bin/dirname "$0")" && /usr/bin/pwd)"
GIT_DIR="${SCRIPT_DIR}/.."
TOKEN=""

# 从 git remote 提取 token
fetch_token() {
    if [ -n "${GITHUB_PAT:-}" ]; then
        TOKEN="$GITHUB_PAT"
        return 0
    fi
    local url
    url=$(/usr/bin/git -C "$GIT_DIR" remote get-url origin 2>/dev/null || echo "")
    if [ -n "$url" ]; then
        local tmp="${url#*oauth2:}"
        if [ "$tmp" != "$url" ]; then
            TOKEN="${tmp%%@*}"
            return 0
        fi
    fi
    return 1
}

trigger() {
    local batch="$1" desc="$2"
    /usr/bin/curl -s -o /dev/null -w "%{http_code}" \
        -X POST \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Accept: application/vnd.github.v3+json" \
        -H "Content-Type: application/json" \
        -d "{\"ref\":\"main\",\"inputs\":{\"batch\":\"${batch}\"}}" \
        "$API"
}

cmd="${1:-show}"
case "$cmd" in
    test)
        fetch_token || { echo "❌ 未找到 GitHub token。请设置: export GITHUB_PAT=ghp_xxx"; exit 1; }
        echo "→ 测试各批次触发..."
        for batch in morning noon evening; do
            code=$(trigger "$batch" "")
            if [ "$code" = "204" ]; then
                echo "  ✅ $batch — 触发成功 (204)"
            else
                echo "  ❌ $batch — HTTP $code"
            fi
        done
        echo ""
        echo "→ 查看运行状态: https://github.com/${REPO}/actions"
        ;;

    show)
        fetch_token || TOKEN="<你的GitHub PAT>"
        echo ""
        echo "  cron-job.org 配置指南（主调度方案）"
        echo "  ══════════════════════════════════════"
        echo ""
        echo "  ⚠️  重要：cron-job.org 时区必须设置为 UTC！"
        echo "       设置方法：cron-job.org 控制台 → 右上角齿轮 → Timezone → (UTC) UTC"
        echo "       如果时区设错（如 Asia/Shanghai），触发时间会偏移 8 小时！"
        echo ""
        echo "  ⚠️  GitHub Actions batch.yml 原生 cron 为 30 分钟后备份，"
        echo "      通过幂等保护自动跳过 cron-job.org 已完成的批次。"
        echo ""
        echo "  1. 打开 https://console.cron-job.org 注册/登录"
        echo "  2. 创建 3 个 Cron Job，参数如下（所有时间基于 UTC）:"
        echo ""
        echo "  ┌─ ① 晨读 08:00 CST — 预触发 UTC 23:00 (CST 07:00) ────────────────────────────┐"
echo "  │ 备注:  提前1h触发，GH runner 在 UTC 00:00 时段延迟严重"
        echo "  │ Title:   足球晨读-晨读快讯+话题擂台                        │"
        echo "  │ URL:     ${API}  │"
        echo "  │ Method:  POST                                    │"
        echo "  │ Schedule: 0 23 * * *                              │"
        echo "  │ Header:  Authorization: Bearer ${TOKEN}"
        echo "  │ Header:  Accept: application/vnd.github.v3+json  │"
        echo "  │ Header:  Content-Type: application/json          │"
        echo "  │ Body:    {\"ref\":\"main\",\"inputs\":{\"batch\":\"morning\"}}│"
        echo "  └──────────────────────────────────────────────────┘"
        echo ""
        echo "  ┌─ ② 午间 12:00 CST — 预触发 UTC 03:55 (CST 11:55) ───────────────────────────┐"
        echo "  │ Title:   足球午间-数据盘点+深水区                          │"
        echo "  │ URL:     ${API}  │"
        echo "  │ Method:  POST                                    │"
        echo "  │ Schedule: 55 3 * * *                              │"
        echo "  │ Header:  Authorization: Bearer ${TOKEN}"
        echo "  │ Header:  Accept: application/vnd.github.v3+json  │"
        echo "  │ Header:  Content-Type: application/json          │"
        echo "  │ Body:    {\"ref\":\"main\",\"inputs\":{\"batch\":\"noon\"}}   │"
        echo "  └──────────────────────────────────────────────────┘"
        echo ""
        echo "  ┌─ ③ 晚间 17:30 CST — 预触发 UTC 09:25 (CST 17:25) ───────────────────────────┐"
        echo "  │ Title:   足球晚间-人物志+时光机                            │"
        echo "  │ URL:     ${API}  │"
        echo "  │ Method:  POST                                    │"
        echo "  │ Schedule: 25 9 * * *                             │"
        echo "  │ Header:  Authorization: Bearer ${TOKEN}"
        echo "  │ Header:  Accept: application/vnd.github.v3+json  │"
        echo "  │ Header:  Content-Type: application/json          │"
        echo "  │ Body:    {\"ref\":\"main\",\"inputs\":{\"batch\":\"evening\"}}│"
        echo "  └──────────────────────────────────────────────────┘"
        echo ""
        echo "  3. 保存后建议点「Run」测试一次"
        echo "  4. 验证: 去 GitHub Actions 看 batch.yml 是否有新的 workflow_dispatch 运行"
        echo ""
        if [ "$TOKEN" = "<你的GitHub PAT>" ]; then
            echo "  ⚠️  未检测到 token。请:"
            echo "     1. 在 GitHub 创建 PAT: Settings → Developer settings"
            echo "        → Fine-grained tokens → Read content + Write actions"
            echo "     2. 执行: export GITHUB_PAT=ghp_xxx"
            echo "     3. 然后运行: bash scripts/setup-cronjob.sh test"
            echo ""
        fi
        ;;

    *)
        echo "用法: bash scripts/setup-cronjob.sh {test|show}"
        exit 1
        ;;
esac
