#!/usr/bin/env bash
# cron-job.org 配置工具 — 精确调度 GitHub Actions workflow_dispatch
#
# 用法:
#   bash scripts/setup-cronjob.sh test   # 测试 API 调用
#   bash scripts/setup-cronjob.sh show   # 显示 cron-job.org 配置

set -e

REPO="chenwu6688/football-auto-publish"
API="https://api.github.com/repos/${REPO}/actions/workflows/daily.yml/dispatches"
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
        echo "  cron-job.org 配置指南"
        echo "  ═══════════════════════════════════"
        echo ""
        echo "  ⚠️  重要：cron-job.org 时区必须设置为 UTC！"
        echo "       设置方法：cron-job.org 控制台 → 右上角齿轮 → Timezone → (UTC) UTC"
        echo "       如果时区设错（如 Asia/Shanghai），推送时间会提前/延后 8 小时！"
        echo ""
        echo "  1. 打开 https://console.cron-job.org 注册/登录"
        echo "  2. 创建 3 个 Cron Job，参数如下（所有时间基于 UTC，对应北京时间）:"
        echo ""
        echo "  ┌─ ① 晨读 08:00 CST (UTC 00:00) ────────────────────────────┐"
        echo "  │ Title:   足球晨读-晨读快讯+话题擂台                        │"
        echo "  │ URL:     ${API}  │"
        echo "  │ Method:  POST                                    │"
        echo "  │ Schedule: 7 0 * * *                              │"
        echo "  │ Header:  Authorization: Bearer ${TOKEN}"
        echo "  │ Header:  Accept: application/vnd.github.v3+json  │"
        echo "  │ Header:  Content-Type: application/json          │"
        echo "  │ Body:    {\"ref\":\"main\",\"inputs\":{\"batch\":\"morning\"}}│"
        echo "  └──────────────────────────────────────────────────┘"
        echo ""
        echo "  ┌─ ② 午间 12:00 CST (UTC 04:00) ───────────────────────────┐"
        echo "  │ Title:   足球午间-数据盘点+深水区                          │"
        echo "  │ URL:     ${API}  │"
        echo "  │ Method:  POST                                    │"
        echo "  │ Schedule: 7 4 * * *                              │"
        echo "  │ Header:  Authorization: Bearer ${TOKEN}"
        echo "  │ Header:  Accept: application/vnd.github.v3+json  │"
        echo "  │ Header:  Content-Type: application/json          │"
        echo "  │ Body:    {\"ref\":\"main\",\"inputs\":{\"batch\":\"noon\"}}   │"
        echo "  └──────────────────────────────────────────────────┘"
        echo ""
        echo "  ┌─ ③ 晚间 17:30 CST (UTC 09:30) ───────────────────────────┐"
        echo "  │ Title:   足球晚间-人物志+时光机                            │"
        echo "  │ URL:     ${API}  │"
        echo "  │ Method:  POST                                    │"
        echo "  │ Schedule: 37 9 * * *                             │"
        echo "  │ Header:  Authorization: Bearer ${TOKEN}"
        echo "  │ Header:  Accept: application/vnd.github.v3+json  │"
        echo "  │ Header:  Content-Type: application/json          │"
        echo "  │ Body:    {\"ref\":\"main\",\"inputs\":{\"batch\":\"evening\"}}│"
        echo "  └──────────────────────────────────────────────────┘"
        echo ""
        echo "  3. 保存后建议点「Run」测试一次"
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
