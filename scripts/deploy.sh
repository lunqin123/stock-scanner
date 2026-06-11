#!/usr/bin/env bash
# 部署脚本: scp 推送 + 远端 restart + 健康检查
# 用法: bash scripts/deploy.sh
# 前置: 1. ssh config 配好 ubuntu@134.175.231.8  2. 本地有 qqChatBot.pem

set -e

REMOTE_HOST="ubuntu@134.175.231.8"
REMOTE_DIR="/home/ubuntu/stock-scanner"
PEM_FILE="qqChatBot.pem"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 推送的核心文件 (增量更新, 不传 .git / data / archive.db 等)
FILES=(
    "app.py"
    "scanner.py"
    "weight_manager.py"
    "backtest_engine.py"
    "cache.py"
    "config.py"
    "data_manager.py"
    "indicators.py"
    "community.py"
    "ak_utils.py"
    "plans/plan_a.py"
    "plans/plan_b.py"
    "plans/datasource.py"
    "static/app.js"
    "static/cards.js"
    "static/dashboard.js"
    "static/style.css"
    "templates/index.html"
)

echo "═══ 1. SCP 推送 ${#FILES[@]} 个文件 → ${REMOTE_HOST} ═══"
for f in "${FILES[@]}"; do
    if [ -f "${LOCAL_DIR}/${f}" ]; then
        scp -i "${PEM_FILE}" -q "${LOCAL_DIR}/${f}" "${REMOTE_HOST}:${REMOTE_DIR}/${f}" && echo "  ✓ ${f}"
    else
        echo "  ✗ ${f} (本地不存在, 跳过)"
    fi
done

echo ""
echo "═══ 2. 远端 systemctl restart ═══"
ssh -i "${PEM_FILE}" "${REMOTE_HOST}" "sudo systemctl restart stock-scanner"

echo ""
echo "═══ 3. 等待服务就绪 (5s) ═══"
sleep 5

echo ""
echo "═══ 4. 健康检查 ═══"
HEALTH=$(ssh -i "${PEM_FILE}" "${REMOTE_HOST}" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/bt/trend/full?days=30" 2>/dev/null || echo "000")
if [ "$HEALTH" = "200" ]; then
    echo "  ✓ /api/bt/trend/full 返回 200"
else
    echo "  ✗ /api/bt/trend/full 返回 ${HEALTH}"
    echo "  → 查看日志: ssh ${REMOTE_HOST} 'sudo journalctl -u stock-scanner -n 30'"
    exit 1
fi

echo ""
echo "═══ 部署完成 ═══"
echo "  浏览器: 强制刷新 (Ctrl+Shift+R) 加载新前端"
