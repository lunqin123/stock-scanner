"""P2.1 后端到端冒烟: 4 个 tab 各跑15天(在 akshare 30天限制内)"""
import sys
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
from backtest_engine import run_tab_backtest, ALL_TABS, TAB_NAMES_CN, _PENDING_TABS

for tab in [t for t in ALL_TABS if t not in _PENDING_TABS]:
    print(f'\n=== {tab} ({TAB_NAMES_CN[tab]}) 15天 ===')
    res = run_tab_backtest(tab=tab, max_days=15, top_n=3, capital=30000, use_cache=False)
    err = res.get('error')
    if err and not res.get('trades'):
        print(f'  错误: {err}')
        print(f'  跳过信号日: {len(res.get("skipped", []))} 个')
        for sk in res.get('skipped', [])[:3]:
            print(f'    {sk}')
        continue
    s = res['summary']
    print(f'  笔数: {s.get("trade_count", 0)} | 胜率: {s.get("win_rate", 0)}% | 累计: {s.get("cumulative_ret", 0):+.2f}%')
    print(f'  跳过信号日: {len(res.get("skipped", []))} 个')
    cmp = res.get('comparison', {})
    print(f'  一字板跳过: {cmp.get("unbuyable_count", 0)} 笔')
    # TOP3 明细
    for t in res.get('trades', [])[:3]:
        print(f'    {t["signal_date"]} {t["code"]} {t["name"]} score={t["score"]} ret={t["net_ret_pct"]:+.2f}%')