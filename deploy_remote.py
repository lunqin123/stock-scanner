#!/usr/bin/env python3
import subprocess
# 部署新 app.py
subprocess.check_call(["cp", "/tmp/app.py", "/home/ubuntu/stock-scanner/app.py"])
# 清掉今天 zhaban 旧 pkl (drift 前是 dtgc 数据)
subprocess.check_call(["rm", "-f"] + __import__("glob").glob("/tmp/claude_stock_cache/daily_2026-06-05_zhaban_raw_*.pkl"))
# 重启
subprocess.check_call(["sudo", "systemctl", "restart", "stock-scanner"])
import time; time.sleep(2)
print("DEPLOY_OK")
