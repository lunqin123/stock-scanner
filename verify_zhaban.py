#!/usr/bin/env python3
import subprocess, json, time, os
TOKEN = "ya1CbjZnnLPlEHeb1XKK0g"
EP = "http://127.0.0.1:8080/api/scan/zhaban/cards?token=" + TOKEN

def curl():
    t1 = time.time()
    out = subprocess.check_output(["curl", "-s", EP]).decode()
    t2 = time.time()
    return (t2 - t1) * 1000, json.loads(out)

print("=== zhaban 首次 ===")
ms1, d1 = curl()
print(f"  耗时: {ms1:.0f}ms, ok={d1.get('ok')}, items={len(d1.get('items', []))}")
if d1.get("items"):
    it = d1["items"][0]
    print(f"  第1只: {it.get('name')}({it.get('code')}) score={it.get('score')} seal_fund={it.get('seal_fund')}")
    print(f"    signals={it.get('signals')}")
    print(f"    industry={it.get('industry')}, turnover={it.get('turnover')}")
    print(f"    advice={it.get('advice')}")
    print(f"    auction={it.get('auction_check')}")

print()
print("=== zhaban 二次 ===")
ms2, d2 = curl()
print(f"  耗时: {ms2:.0f}ms, items={len(d2.get('items', []))}")
if d2.get("items"):
    it2 = d2["items"][0]
    print(f"  第1只: {it2.get('name')}({it2.get('code')}) score={it2.get('score')}")
    # 验证重算
    same = d1["items"][0]["score"] == d2["items"][0]["score"]
    print(f"  二次重算 score 与首次一致: {same}")

print()
print("=== pkl ===")
os.system("ls -la /tmp/claude_stock_cache/daily_2026-06-05_zhaban_raw_*.pkl")

print()
print("=== server 错误 (最近 50 条日志) ===")
os.system("sudo journalctl -u stock-scanner --no-pager -n 50 2>&1 | grep -iE 'error|exception|traceback' | grep -vE 'LARK|larkcli|toolunavailable' | tail -3")
