#!/usr/bin/env python3
"""Monkey-patch _get_ohlcv_batch with debug output"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import t1_real_backtest as t1
import akshare as ak
from cache import get as cg, put as cp

orig_fn = t1._get_ohlcv_batch

def debug_ohlcv(code, dates):
    result = {}
    missing = []
    for d in dates:
        key = f"t1_ohlcv_{code}_{d}"
        cached = cg(key)
        if cached is not None:
            if cached != '__NONE__':
                result[d] = cached
        else:
            missing.append(d)
    print(f"ohlcv[{code}]: cached={len(result)} missing={len(missing)}")
    if not missing:
        return result

    start, end = min(missing), max(missing)
    prefix = 'sh' if code.startswith('6') else 'sz'
    fmt_s = f'{start[:4]}-{start[4:6]}-{start[6:8]}'
    fmt_e = f'{end[:4]}-{end[4:6]}-{end[6:8]}'

    # Tencent
    print(f"  TX: {prefix}{code} {fmt_s}~{fmt_e}")
    df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}', start_date=fmt_s, end_date=fmt_e)
    if df is not None:
        print(f"  TX df: type={type(df).__name__} empty={df.empty if hasattr(df, 'empty') else '?'}")
    if df is not None and hasattr(df, 'empty') and not df.empty:
        print(f"  TX cols={list(df.columns)} rows={len(df)}")
        # Process like original
        import pandas as pd
        df['日期'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
        for _, row in df.iterrows():
            d = str(row.get('日期', '')).replace('-', '')
            if d not in missing:
                continue
            o = {'open': float(row['open']), 'close': float(row['close']),
                 'high': float(row['high']), 'low': float(row['low']),
                 'volume': int(row['volume']), 'amount': float(row.get('amount', 0)),
                 'turnover': float(row.get('turnover', 0)),
                 'change_pct': float(row.get('change', row.get('涨跌幅', 0)))}
            cp(f"t1_ohlcv_{code}_{d}", o)
            result[d] = o
        for d in missing:
            if d not in result:
                cp(f"t1_ohlcv_{code}_{d}", '__NONE__')
        print(f"  TX result: {len(result)} dates")
        return result
    print("  TX: empty, fallback to EM...")

    # 东方财富 fallback
    try:
        df2 = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start, end_date=end, adjust='')
        if df2 is not None:
            print(f"  EM df: type={type(df2).__name__} empty={df2.empty if hasattr(df2, 'empty') else '?'}")
    except Exception as e:
        print(f"  EM error: {type(e).__name__}")

    for d in missing:
        cp(f"t1_ohlcv_{code}_{d}", '__NONE__')
    return result

t1._get_ohlcv_batch = debug_ohlcv

# Now test
print("=== Testing ===")
r = t1._get_ohlcv_batch('000001', ['20260601','20260602','20260603'])
print(f"\nFINAL: {len(r)} dates")
for d, v in sorted(r.items()):
    print(f"  {d}: open={v['open']} close={v['close']}")
