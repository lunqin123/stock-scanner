#!/usr/bin/env python3
"""诊断单日回测流水线"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_engine import _fetch_limit_up_pool, _score_limit_up
from t1_real_backtest import _get_ohlcv_batch

D = '20260601'
print(f'=== 诊断信号日 {D} ===')

# Step 1: 信号池
pool = _fetch_limit_up_pool(D)
has = pool is not None and hasattr(pool, 'empty') and not pool.empty
print(f'1. pool: {type(pool).__name__}, has_data={has}, len={len(pool) if pool is not None else 0}')

# Step 2: 评分
if pool is not None and not pool.empty:
    scored = _score_limit_up(pool, D)
    has_s = scored is not None and hasattr(scored, 'empty') and not scored.empty
    print(f'2. scored: {type(scored).__name__}, has_data={has_s}, len={len(scored) if scored is not None else 0}')
    if has_s:
        print(f'   cols: {list(scored.columns)[:6]}')
        score_col = [c for c in scored.columns if '评分' in str(c) or 'score' in str(c).lower()]
        print(f'   score_cols: {score_col}')
else:
    print('2. SKIP - pool empty')

# Step 3: OHLCV (random stock)
print('3. testing OHLCV for 000001...')
try:
    ohlcv = _get_ohlcv_batch('000001', [D, '20260602', '20260603'])
    print(f'   got {len(ohlcv)} dates')
    for d, v in sorted(ohlcv.items()):
        print(f'   {d}: open={v.get("open")} close={v.get("close")}')
except Exception as e:
    print(f'   FAIL: {type(e).__name__}: {e}')
