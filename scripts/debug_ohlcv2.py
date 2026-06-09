#!/usr/bin/env python3
"""Deep debug _get_ohlcv_batch"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import akshare as ak
import pandas as pd
from cache import get as _cache_get, put as _cache_put

code = '000001'
dates = ['20260601', '20260602', '20260603']

# Step 1: check cache
result = {}
missing = []
for d in dates:
    key = f"t1_ohlcv_{code}_{d}"
    cached = _cache_get(key)
    print(f"  cache[{key}]: {type(cached).__name__} = {str(cached)[:50]}")
    if cached is not None:
        if cached != '__NONE__':
            result[d] = cached
    else:
        missing.append(d)

print(f"result so far: {len(result)} dates")
print(f"missing: {missing}")

if not missing:
    print("nothing missing, done")
    sys.exit(0)

# Step 2: Tencent source
start, end = min(missing), max(missing)
prefix = 'sh' if code.startswith('6') else 'sz'
fmt_s = f'{start[:4]}-{start[4:6]}-{start[6:8]}'
fmt_e = f'{end[:4]}-{end[4:6]}-{end[6:8]}'
print(f"Trying Tencent: {prefix}{code} {fmt_s}~{fmt_e}")

try:
    df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}', start_date=fmt_s, end_date=fmt_e)
    print(f"  type={type(df).__name__}")
    if df is not None and not df.empty:
        print(f"  rows={len(df)} cols={list(df.columns)}")
        df['日期'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
        print(f"  dates after transform: {df['日期'].tolist()}")

        for _, row in df.iterrows():
            d = str(row.get('日期', '')).replace('-', '')
            print(f"    row d={d} in missing={d in missing}")
            if d not in missing:
                continue
            o = {'open': float(row['open']), 'close': float(row['close']),
                 'high': float(row['high']), 'low': float(row['low']),
                 'volume': int(row['volume']), 'amount': float(row.get('amount', 0)),
                 'turnover': float(row.get('turnover', 0)),
                 'change_pct': float(row.get('change', row.get('涨跌幅', 0)))}
            _cache_put(f"t1_ohlcv_{code}_{d}", o)
            result[d] = o
            print(f"    -> added {d}: open={o['open']} close={o['close']}")
    else:
        print("  EMPTY")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print(f"\nFINAL result: {len(result)} dates")
for d, v in sorted(result.items()):
    print(f"  {d}: open={v['open']} close={v['close']}")
