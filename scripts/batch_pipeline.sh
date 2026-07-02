#!/bin/bash
# 足球自媒体 - 批次发布脚本 (morning/noon/evening)
# 由 systemd timer 在 08:00 / 12:00 / 17:30 触发（均为北京时间）
# 如需配置 systemd timer，确保服务器时区为 Asia/Shanghai 或换算对应的 UTC 时间
#
# Usage: bash batch_pipeline.sh --batch=noon

set -euo pipefail

PROJECT_DIR="/home/chenwu/projects/football-auto-publish"
VENV_PYTHON="/home/chenwu/projects/wusongshuruf/venv/bin/python3"
LOG_DIR="$PROJECT_DIR/output/logs"
OUTPUT_DIR="/home/chenwu/每日自媒体文案"

# Parse --batch=xxx
BATCH="auto"
for arg in "$@"; do
    if [[ "$arg" == --batch=* ]]; then
        BATCH="${arg#--batch=}"
    fi
done

LOCK_FILE="/tmp/football_${BATCH}.lock"
ORCHESTRATOR_TIMEOUT=600
PUBLISHER_TIMEOUT=600

# WxPusher
WXPUSHER_APPTOKEN="AT_bDxaogJWYj2tDHbqpwx6SOnPFDlViC9f"
WXPUSHER_UID="UID_HeN23B80PBHA9E2Yb71QLHrNERM1"

mkdir -p "$LOG_DIR"

send_wxpush() {
    local title="$1"
    local content="$2"
    if [ -n "$WXPUSHER_APPTOKEN" ] && [ -n "$WXPUSHER_UID" ]; then
        curl -s -X POST "https://wxpusher.zjiecode.com/api/send/message" \
            -H "Content-Type: application/json" \
            -d "{\"appToken\":\"$WXPUSHER_APPTOKEN\",\"content\":\"$title\n\n$content\",\"contentType\":1,\"uids\":[\"$WXPUSHER_UID\"]}" \
            -m 10 > /dev/null 2>&1 || true
    fi
}

TODAY=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/orchestrator_${TODAY}.log"

# --- 0. 防并发锁 ---
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[$(date '+%H:%M:%S')] 批次 $BATCH 上一个任务仍在运行 (PID=$LOCK_PID)，退出。" | tee -a "$LOG_FILE"
        exit 0
    fi
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT

exec > >(tee -a "$LOG_FILE") 2>&1

echo ""
echo "=========================================="
echo "  足球自媒体批次发布 — $BATCH"
echo "  日期: $TODAY"
echo "  开始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  PID: $$"
echo "=========================================="

# --- 1. 检查 venv python ---
if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ venv python 不存在: $VENV_PYTHON"
    send_wxpush "足球自媒体 ❌" "${TODAY} ${BATCH} 批次失败：venv python 未找到。"
    exit 1
fi

# --- 2. 生成文章 ---
echo ""
echo "[1/2] 生成文章 ($BATCH 批次)... (最多等待 ${ORCHESTRATOR_TIMEOUT}s)"
cd "$PROJECT_DIR"

export OUTPUT_DIR="$OUTPUT_DIR"
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-sk-eb0eb4ef7f0841228fa540dfb6a7bcb1}"
export DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-sk-ca3e44197141434cb24f286402c3ecdf}"
export UNSPLASH_ACCESS_KEY="${UNSPLASH_ACCESS_KEY:-hiiaKbLS64bsBA-pWAJBnF_g1rG0ZAcw68clp70jkxU}"
export FOOTBALL_DATA_KEY="${FOOTBALL_DATA_KEY:-3106b3825289475ba851bff42ebb452d}"
export WXPUSHER_APPTOKEN="${WXPUSHER_APPTOKEN:-AT_bDxaogJWYj2tDHbqpwx6SOnPFDlViC9f}"
export WXPUSHER_UID="${WXPUSHER_UID:-UID_HeN23B80PBHA9E2Yb71QLHrNERM1}"
export TOUTIAO_AUTH_FILE="/home/chenwu/projects/wusongshuruf/config/toutiao_auth.json"

if timeout $ORCHESTRATOR_TIMEOUT "$VENV_PYTHON" orchestrator.py "$TODAY" --batch="$BATCH"; then
    echo "✅ 文章生成完成"
else
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 124 ]; then
        echo "❌ 文章生成超时 (${ORCHESTRATOR_TIMEOUT}s)"
        send_wxpush "足球自媒体 ⚠️" "${TODAY} ${BATCH} 批次生成超时！"
        exit 1
    fi
    echo "⚠️  文章生成退出码: $EXIT_CODE，继续发布"
fi

# --- 3. 发布到头条号 ---
echo ""
echo "[2/2] 发布到头条号... (最多等待 ${PUBLISHER_TIMEOUT}s)"

if timeout $PUBLISHER_TIMEOUT "$VENV_PYTHON" publisher.py "$TODAY" --headless --batch="$BATCH"; then
    PUB_RESULT=0
    echo "✅ 发布完成"
else
    PUB_RESULT=$?
    if [ $PUB_RESULT -eq 124 ]; then
        echo "❌ 发布超时 (${PUBLISHER_TIMEOUT}s)"
    else
        echo "⚠️  发布退出码: $PUB_RESULT"
    fi
fi

# --- 4. 通知 ---
END_TIME=$(date '+%H:%M:%S')
if [ $PUB_RESULT -eq 0 ]; then
    send_wxpush "足球自媒体 ✅" "${TODAY} ${BATCH} 批次发布完毕\n完成时间: $END_TIME"
    echo ""
    echo "=========================================="
    echo "  ✅ 全部完成 ($END_TIME)"
    echo "  日志: $LOG_FILE"
    echo "=========================================="
else
    send_wxpush "足球自媒体 ⚠️" "${TODAY} ${BATCH} 批次发布异常 (code=$PUB_RESULT)\n请查看日志: $LOG_FILE"
    echo ""
    echo "=========================================="
    echo "  ⚠️  完成但有错误 ($END_TIME)"
    echo "  日志: $LOG_FILE"
    echo "=========================================="
fi
