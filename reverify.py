#!/usr/bin/env python3
import subprocess, glob, time
# 清 pkl
files = glob.glob("/tmp/claude_stock_cache/daily_2026-06-05_zhaban_raw_*.pkl")
for f in files:
    subprocess.check_call(["rm", "-f", f])
    print(f"删 {f}")
# restart
subprocess.check_call(["sudo", "systemctl", "restart", "stock-scanner"])
time.sleep(2)
# 跑 verify
subprocess.check_call(["python3", "/tmp/verify_zhaban.py"])
