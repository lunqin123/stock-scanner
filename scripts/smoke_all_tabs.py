#!/usr/bin/env python3
"""6 tab 全量冒烟测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_engine import run_tab_backtest

tabs = ['limit-up', 'zhaban', 'dtqiaoban', 'trend', 'reversal', 'sector']
errors = []

for tab in tabs:
    try:
        r = run_tab_backtest(tab, start_date='20260601', end_date='20260609', max_days=7, use_cache=False)
        s = r.get('summary', {})
        trades = len(r.get('trades', []))
        err = r.get('error', '')
        status = f'ERR={err}' if err else 'OK'
        print(f'{tab}: {trades}笔 胜率{s.get("win_rate",0)}% EV{s.get("ev",0):+.2f}% {status}')
        if err:
            errors.append(f'{tab}: {err}')
    except Exception as e:
        print(f'{tab}: CRASH {type(e).__name__}: {e}')
        errors.append(f'{tab}: {type(e).__name__}: {e}')

print()
if errors:
    print(f'❌ {len(errors)} 个错误:')
    for e in errors:
        print(f'  {e}')
else:
    print('✅ 全部 6 个 tab 通过')
