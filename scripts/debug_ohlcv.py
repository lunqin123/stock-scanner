#!/usr/bin/env python3
"""Debug OHLCV pipeline"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import akshare as ak
import pandas as pd

code = '000001'
start = '20260601'
end = '20260603'
prefix = 'sh' if code.startswith('6') else 'sz'
fmt_s = f'{start[:4]}-{start[4:6]}-{start[6:8]}'
fmt_e = f'{end[:4]}-{end[4:6]}-{end[6:8]}'
print(f'calling: ak.stock_zh_a_hist_tx(symbol={prefix}{code}, start={fmt_s}, end={fmt_e})')

df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}', start_date=fmt_s, end_date=fmt_e)
print(f'type={type(df).__name__}')
if df is not None and hasattr(df, 'empty'):
    print(f'empty={df.empty}')
if df is not None and not df.empty:
    print(f'columns: {list(df.columns)}')
    print(df.head())
    df['日期'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
    print(f'dates: {df["日期"].tolist()}')
else:
    print('EMPTY OR NONE')
