#!/bin/bash
cp /tmp/app.py /home/ubuntu/stock-scanner/app.py
# 清掉今天所有 daily 缓存
rm -f /tmp/claude_stock_cache/daily_*.json /tmp/claude_stock_cache/daily_*.pkl 2>&1
sudo systemctl restart stock-scanner
sleep 2

for name_ep in "limit-up:/api/scan/limit-up/cards" "sector:/api/scan/sector/cards" "zhaban:/api/scan/zhaban/cards"; do
    name="${name_ep%:*}"
    ep="${name_ep#*:}"
    echo
    echo "=== $name 首次 ==="
    T1=$(date +%s%N)
    curl -s "http://127.0.0.1:8080${ep}?principal=20000&token=ya1CbjZnnLPlEHeb1XKK0g" -o /tmp/r1.json
    T2=$(date +%s%N)
    SIZE=$(stat -c%s /tmp/r1.json 2>/dev/null || stat -f%z /tmp/r1.json 2>/dev/null)
    OK=$(python3 -c "import json; d=json.load(open('/tmp/r1.json')); print('ok' if d.get('ok') else d.get('error','no-ok'))" 2>&1 | head -c 200)
    echo "  耗时: $(( (T2-T1)/1000000 ))ms, 大小: ${SIZE}B, 状态: $OK"

    echo "=== $name 二次 ==="
    T1=$(date +%s%N)
    curl -s "http://127.0.0.1:8080${ep}?principal=20000&token=ya1CbjZnnLPlEHeb1XKK0g" -o /tmp/r2.json
    T2=$(date +%s%N)
    OK=$(python3 -c "import json; d=json.load(open('/tmp/r2.json')); print('ok' if d.get('ok') else d.get('error','no-ok'))" 2>&1 | head -c 200)
    echo "  耗时: $(( (T2-T1)/1000000 ))ms, 状态: $OK"

    if [ "$name" == "sector" ]; then
        S1=$(python3 -c "import json; d=json.load(open('/tmp/r1.json')); print(len(d.get('items',[])))" 2>&1)
        S2=$(python3 -c "import json; d=json.load(open('/tmp/r2.json')); print(len(d.get('items',[])))" 2>&1)
        echo "  items 数: 首次=$S1 二次=$S2"
    fi
done

echo
echo "=== pkl 文件 ==="
ls -la /tmp/claude_stock_cache/daily_*_raw_*.pkl 2>&1
echo
echo "=== server 错误日志 ==="
sudo journalctl -u stock-scanner --no-pager -n 100 2>&1 | grep -iE 'error|exception|traceback' | tail -5
