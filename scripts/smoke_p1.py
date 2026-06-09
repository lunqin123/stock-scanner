"""P1.1 冒烟测试: limit-up / zhaban / dtqiaoban 各跑5天"""
import sys
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
from backtest_engine import run_tab_backtest, TAB_LIMIT_UP, TAB_ZHABAN, TAB_DTQIAOBAN

for tab in [TAB_LIMIT_UP, TAB_ZHABAN, TAB_DTQIAOBAN]:
    print(f'\n=== {tab} (5天) ===')
    res = run_tab_backtest(tab=tab, max_days=5, top_n=3, capital=30000, use_cache=False)
    err = res.get('error')
    if err and not res.get('trades'):
        print(f'  错误: {err}')
        continue
    s = res['summary']
    print(f'  笔数: {s.get("trade_count", 0)} | 胜率: {s.get("win_rate", 0)}% | 累计: {s.get("cumulative_ret", 0):+.2f}%')
    print(f'  跳过信号日: {len(res.get("skipped", []))} 个')
    cmp = res.get('comparison', {})
    print(f'  一字板跳过: {cmp.get("unbuyable_count", 0)} 笔')
    # 显示前 2 笔明细
    for t in res.get('trades', [])[:2]:
        print(f'    {t["signal_date"]} {t["code"]} {t["name"]} score={t["score"]} ret={t["net_ret_pct"]:+.2f}% buyable={t["buyable"]}')