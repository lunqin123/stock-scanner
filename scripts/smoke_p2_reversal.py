"""P2.1 冒烟测试: _score_reversal"""
import sys
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
from scanner import _score_reversal
from backtest_engine import _fetch_reversal_pool

# 历史回测模式 (today_str=None)
pool = _fetch_reversal_pool('20260605')
if pool is None or pool.empty:
    print('  信号池空,跳过')
else:
    print(f'  回调池: {len(pool)} 只')
    res = _score_reversal(pool, today_str=None)
    print(f'  评分后: {len(res)} 只')
    if '反转评分' in res.columns:
        top3 = res.nlargest(3, '反转评分')
        for _, r in top3.iterrows():
            print(f'    {r["代码"]} {r["名称"]}: 反转{r["反转评分"]:.0f}分 | 今{r["今日涨幅"]:+.1f}%')

# 实盘模式 (today_str=今天)
print()
print('--- 实盘模式 ---')
pool2 = _fetch_reversal_pool('20260605')
res2 = _score_reversal(pool2, today_str='20260605')
print(f'  评分后: {len(res2)} 只')
if '反转评分' in res2.columns:
    top3 = res2.nlargest(3, '反转评分')
    for _, r in top3.iterrows():
        print(f'    {r["代码"]} {r["名称"]}: 反转{r["反转评分"]:.0f}分 | 今{r["今日涨幅"]:+.1f}%')