#!/usr/bin/env python3
"""快速回测试跑 - 诊断版"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_engine import run_tab_backtest, _trading_dates_in_range, _is_trading_day

# 先看 get 到哪些交易日
dates = _trading_dates_in_range('20260601', '20260609', max_count=7)
print(f'trade_dates ({len(dates)}): {dates}')

result = run_tab_backtest("limit-up", start_date="20260601", end_date="20260609", max_days=7, use_cache=False)
s = result.get("summary", {})
s30 = result.get("summary_30d", {})
tc = s.get("trade_count", 0)
print(f'涨停回测: {tc}笔 胜率{s.get("win_rate",0)}%')

trades = result.get("trades", [])
skipped = result.get("skipped", [])
print(f'trades={len(trades)} skipped={len(skipped)}')

if trades:
    for t in trades[:6]:
        print(f'  {t["signal_date"]}->{t["buy_date"]}->{t["sell_date"]} {t["name"]}({t["code"]}) {t["net_ret_pct"]:+.2f}%')
if skipped:
    print('--- skipped ---')
    for sk in skipped:
        print(f'  {sk["signal"]}: {sk["reason"]}')
