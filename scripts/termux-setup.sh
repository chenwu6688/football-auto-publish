#!/data/data/com.termux/files/usr/bin/bash
# ==============================================
# 足球自媒体 - Termux 定时调度部署脚本
# 在 Android Termux 中执行此脚本即可完成全部配置
# ==============================================
set -e

echo "====================================="
echo "  足球自媒体 Termux 调度部署"
echo "====================================="
echo ""

# --- 1. 安装依赖 ---
echo "[1/4] 安装 curl 和 cron..."
pkg update -y
pkg install curl cronie termux-services -y

# --- 2. 创建触发脚本 ---
echo "[2/4] 创建触发器脚本..."
mkdir -p ~/.scripts

cat > ~/.scripts/trigger-batch.sh << 'SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
BATCH="$1"
TOKEN="PASTE_YOUR_GITHUB_TOKEN_HERE"
LOG=~/.scripts/trigger.log

RESP=$(curl -s -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/chenwu6688/football-auto-publish/actions/workflows/batch.yml/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":{\"batch\":\"$BATCH\"}}" \
  -w "\nHTTP:%{http_code}" 2>&1)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] $BATCH → $RESP" >> "$LOG"
echo "$RESP"
SCRIPT

chmod +x ~/.scripts/trigger-batch.sh

# --- 3. 设置 crontab ---
echo "[3/4] 配置定时任务..."
TMP_CRON=$(mktemp)
cat > "$TMP_CRON" << 'CRON'
# 足球自媒体 - 三个批次定时触发
# 晨读 08:00
0 8 * * * ~/.scripts/trigger-batch.sh morning
# 午间 12:00
0 12 * * * ~/.scripts/trigger-batch.sh noon
# 晚间 17:30
30 17 * * * ~/.scripts/trigger-batch.sh evening
CRON

crontab "$TMP_CRON"
rm "$TMP_CRON"

# --- 4. 启动 cron 服务 ---
echo "[4/4] 启动 cron 服务..."
sv-enable crond
sv up crond

echo ""
echo "====================================="
echo "  部署完成!"
echo "====================================="
echo ""
echo "验证:"
echo "  1. sv status crond    → 确认 cron 在运行"
echo "  2. crontab -l          → 查看定时任务"
echo "  3. ~/.scripts/trigger-batch.sh morning  → 手动测试"
echo "  4. cat ~/.scripts/trigger.log → 查看日志"
echo ""
echo "⚠️  重要：退出 Termux 后到系统设置："
echo "  设置 → 应用 → Termux → 电池 → 无限制"
echo "  通知栏保持 Termux 常驻"
