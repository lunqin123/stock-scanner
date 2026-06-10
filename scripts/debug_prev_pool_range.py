"""Debug akshare prev_pool 数据范围"""
import sys
sys.path.insert(0, r"/home/ubuntu/stock-scanner")
import akshare as ak
for d in ['20260608', '20260530', '20260515', '20260501', '20260424', '20260415']:
    try:
        df = ak.stock_zt_pool_previous_em(date=d)
        print(f'{d}: rows={len(df) if df is not None else None}')
    except Exception as e:
        print(f'{d}: ERR {type(e).__name__}: {str(e)[:80]}')