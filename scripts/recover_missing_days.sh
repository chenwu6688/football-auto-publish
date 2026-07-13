#!/bin/bash
# 补发缺失日期推文 — 通过 GitHub Actions workflow_dispatch 触发
#
# 用法:
#   ./scripts/recover_missing_days.sh                  # 补发 07/10-07/12 全部缺失批次
#   ./scripts/recover_missing_days.sh 2026-07-10        # 补发指定日期的全部批次
#   ./scripts/recover_missing_days.sh 2026-07-10 morning # 补发指定日期的指定批次
#
# 注意：补发的内容是"当时"的比赛数据（直播吧/懂球帝抓取对应日期的存档），
# 而非实时快讯。适合作为内容补充，但过期比赛的热度会低于实时推送。
# 建议优先恢复 07/10-07/12 的午间和晚间批次（内容时效性相对较好）。

set -euo pipefail

REPO="chenwu6688/football-auto-publish"
WORKFLOW="batch.yml"

# 需要补发的日期列表（缺失的3天 + 7/09 incomplete）
MISSING_DATES=("2026-07-10" "2026-07-11" "2026-07-12")

# 如果用户指定了日期
SINGLE_DATE="${1:-}"
SINGLE_BATCH="${2:-}"

trigger_batch() {
    local date="$1"
    local batch="$2"
    echo "🚀 触发: $date $batch"
    gh workflow run "$WORKFLOW" \
        --repo "$REPO" \
        --ref main \
        -f batch="$batch" \
        -f date="$date"
    echo "   触发成功 (workflow_dispatch)"
}

echo "======================================"
echo "  足球自媒体 — 缺失内容补发脚本"
echo "  日期范围: 2026-07-10 ~ 2026-07-12"
echo "======================================"
echo ""

# 检查 gh CLI 是否登录
if ! gh auth status 2>/dev/null; then
    echo "❌ 请先登录 GitHub CLI: gh auth login"
    exit 1
fi

if [ -n "$SINGLE_DATE" ]; then
    if [ -n "$SINGLE_BATCH" ]; then
        trigger_batch "$SINGLE_DATE" "$SINGLE_BATCH"
    else
        for batch in morning noon evening; do
            trigger_batch "$SINGLE_DATE" "$batch"
            sleep 3  # 避免 API 限流
        done
    fi
else
    for date in "${MISSING_DATES[@]}"; do
        echo ""
        echo "--- $date ---"
        for batch in morning noon evening; do
            trigger_batch "$date" "$batch"
            sleep 3
        done
    done
fi

echo ""
echo "======================================"
echo "  ✅ 所有补发任务已触发"
echo "  查看进度: https://github.com/$REPO/actions"
echo ""
echo "  ⚠️  重要提示:"
echo "  1. 补发的是过期的比赛数据，推荐跳过晨读（时效差）"
echo "  2. 午间和晚间可做内容填充"
echo "  3. 建议先跑一个批次验证能正常生成再批量跑"
echo "======================================"
