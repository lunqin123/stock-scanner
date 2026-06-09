"""Debug: 单日 zhaban 评分"""
import sys
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
import traceback
from backtest_engine import _fetch_zhaban_pool, _score_zhaban

pool = _fetch_zhaban_pool('20260520')
print(f'信号池: {type(pool).__name__} | empty: {pool.empty if pool is not None else None}')
if pool is not None and not pool.empty:
    print(f'  columns ({len(pool.columns)}):', list(pool.columns[:5]), '...')
    try:
        scored = _score_zhaban(pool, '20260520')
        print(f'评分后: {type(scored).__name__} | empty: {scored.empty if scored is not None else None}')
    except Exception as e:
        traceback.print_exc()