#!/usr/bin/env python3
"""清 OHLCV 缓存并重试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cache import _cache_put, _cache_get
from t1_real_backtest import _get_ohlcv_batch

# 直接测腾讯源 (绕过东方财富的 Connection aborted)
import akshare as ak
import pandas as pd

print('=== 直接用腾讯源 ===')
code = '000001'
prefix = 'sz'  # 000001 is Shenzhen
df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}', start_date='2026-06-01', end_date='2026-06-03')
if df is not None and not df.empty:
    print(f'OK: {len(df)} rows')
    print(df[['date','open','close']].to_string())
else:
    print('EMPTY')

print()
print('=== 走 _get_ohlcv_batch (可能卡东方财富) ===')
result = _get_ohlcv_batch('000001', ['20260601','20260602','20260603'])
print(f'result: {len(result)} dates')
for d, v in sorted(result.items()):
    print(f'  {d}: open={v["open"]} close={v["close"]}')
