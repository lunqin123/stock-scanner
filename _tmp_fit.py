"""在服务器上跑: 用历史交易数据拟合最优评分逻辑
只使用信号日可知的特征 (不用gap, 因为gap是买入日才知道的)"""
import sys, os, pickle, json, math, statistics
sys.path.insert(0, '/home/ubuntu/stock-scanner')
os.chdir('/home/ubuntu/stock-scanner')

import pandas as pd
import numpy as np
from backtest_engine import run_tab_backtest, _detect_available_days

# 1. 收集所有tab的全量交易 (top_n=1, ms=0, sn=2, 拿到所有特征)
all_trades = []
for tab in ['limit-up', 'trend', 'reversal', 'zhaban', 'dtqiaoban']:
    d = _detect_available_days(tab)
    print(f"{tab}: {d} days available", flush=True)
    for sn in [2, 3]:
        r = run_tab_backtest(tab=tab, max_days=d, top_n=1, min_score=0, sell_n=sn, use_cache=False)
        trades = r.get('trades', [])
        for t in trades:
            t['tab'] = tab
            t['sell_n'] = sn
            all_trades.append(t)
    s = r.get('summary', {})
    print(f"  -> {len(trades)} trades, wr={s.get('win_rate',0)}%, pnl={s.get('total_pnl',0)}", flush=True)

print(f"\nTotal trades: {len(all_trades)}", flush=True)
if not all_trades:
    print("NO TRADES"); sys.exit()

# 2. 分析信号日可知特征与收益的关系
rets = [t['net_ret_pct'] for t in all_trades]

def pearson(x, y):
    n = len(x)
    if n < 3: return 0
    mx, my = sum(x)/n, sum(y)/n
    sx = sum((xi-mx)**2 for xi in x)
    sy = sum((yi-my)**2 for yi in y)
    if sx == 0 or sy == 0: return 0
    sxy = sum((x[i]-mx)*(y[i]-my) for i in range(n))
    return sxy / math.sqrt(sx * sy)

# 信号日可知的特征
sig_features = ['score', 'signal_close', 'buy_turnover', 'gap_open_pct', 'rank']
print("\n=== Signal-time feature IC ===", flush=True)
for feat in sig_features:
    vals = [float(t.get(feat, 0) or 0) for t in all_trades]
    ic = pearson(vals, rets)
    win_vals = [v for v, r in zip(vals, rets) if r > 0]
    loss_vals = [v for v, r in zip(vals, rets) if r <= 0]
    print(f"  {feat:20s}: IC={ic:+.3f}  win_avg={statistics.mean(win_vals) if win_vals else 0:.2f}  loss_avg={statistics.mean(loss_vals) if loss_vals else 0:.2f}", flush=True)

# 3. 逐特征分桶看胜率和盈亏
def bucket_analysis(name, trades, key, buckets):
    print(f"\n=== {name} buckets ===", flush=True)
    results = []
    for lo, hi in buckets:
        bt = [t for t in trades if lo <= float(t.get(key, 0) or 0) < hi]
        if not bt: continue
        tr = [t['net_ret_pct'] for t in bt]
        wins = [r for r in tr if r > 0]
        wr = len(wins)/len(tr)*100
        pnl = sum(t['pnl'] for t in bt)
        avg = statistics.mean(tr)
        print(f"  [{lo},{hi}): n={len(bt):3d} wr={wr:5.1f}% avg={avg:+.2f}% pnl={pnl:+8.0f}", flush=True)
        results.append({'lo': lo, 'hi': hi, 'n': len(bt), 'wr': wr, 'pnl': pnl, 'avg': avg})
    return results

bucket_analysis("Score", all_trades, 'score', [(0,30),(30,40),(40,50),(50,60),(60,70),(70,80),(80,100)])
bucket_analysis("Price", all_trades, 'signal_close', [(0,5),(5,10),(10,15),(15,20),(20,30),(30,50),(50,999)])
bucket_analysis("Gap", all_trades, 'gap_open_pct', [(-99,-3),(-3,0),(0,1),(1,3),(3,5),(5,99)])
bucket_analysis("Turnover", all_trades, 'buy_turnover', [(0,1),(1,3),(3,5),(5,8),(8,15),(15,999)])

# 4. 按 tab x sn 分析
print("\n=== Tab x sell_n ===", flush=True)
for tab in ['limit-up', 'trend', 'reversal', 'zhaban', 'dtqiaoban']:
    for sn in [2, 3]:
        bt = [t for t in all_trades if t['tab'] == tab and t['sell_n'] == sn]
        if not bt: continue
        tr = [t['net_ret_pct'] for t in bt]
        wins = [r for r in tr if r > 0]
        wr = len(wins)/len(tr)*100
        pnl = sum(t['pnl'] for t in bt)
        print(f"  {tab:12s} sn={sn}: n={len(bt):3d} wr={wr:5.1f}% pnl={pnl:+8.0f}", flush=True)

# 5. 组合规则搜索 (只用信号日可知的特征: score, price, tab, sn)
print("\n=== Best combo (score + price + tab + sn) ===", flush=True)
best_rules = []
for s_lo, s_hi in [(0,40),(40,50),(50,60),(60,100),(0,100)]:
    for p_lo, p_hi in [(0,10),(10,20),(20,999),(0,999)]:
        for tab_f in ['limit-up', 'zhaban', 'dtqiaoban', 'all']:
            for sn_f in [2, 3]:
                bt = [t for t in all_trades
                      if s_lo <= t.get('score', 0) < s_hi
                      and p_lo <= t.get('signal_close', 0) < p_hi
                      and (tab_f == 'all' or t['tab'] == tab_f)
                      and t['sell_n'] == sn_f]
                if len(bt) < 5: continue
                tr = [t['net_ret_pct'] for t in bt]
                wins = [r for r in tr if r > 0]
                wr = len(wins)/len(tr)*100
                pnl = sum(t['pnl'] for t in bt)
                avg = statistics.mean(tr)
                if pnl > 0:
                    best_rules.append({'n': len(bt), 'wr': wr, 'pnl': pnl, 'avg': avg,
                                       'score': f'[{s_lo},{s_hi})', 'price': f'[{p_lo},{p_hi})',
                                       'tab': tab_f, 'sn': sn_f})

best_rules.sort(key=lambda x: x['pnl'], reverse=True)
for r in best_rules[:20]:
    print(f"  n={r['n']:3d} wr={r['wr']:5.1f}% avg={r['avg']:+.2f}% pnl={r['pnl']:+8.0f} | score={r['score']} price={r['price']} tab={r['tab']} sn={r['sn']}", flush=True)

# 6. 交叉验证: 按时间分两半, 看规则稳定性
print("\n=== Time-split validation ===", flush=True)
dates = sorted(set(t['signal_date'] for t in all_trades))
mid = dates[len(dates)//2]
first_half = [t for t in all_trades if t['signal_date'] < mid]
second_half = [t for t in all_trades if t['signal_date'] >= mid]
print(f"  First half: {len(first_half)} trades (before {mid})", flush=True)
print(f"  Second half: {len(second_half)} trades (from {mid})", flush=True)

for tab_f in ['limit-up', 'zhaban', 'dtqiaoban']:
    for sn_f in [2, 3]:
        h1 = [t for t in first_half if t['tab'] == tab_f and t['sell_n'] == sn_f]
        h2 = [t for t in second_half if t['tab'] == tab_f and t['sell_n'] == sn_f]
        if len(h1) < 3 or len(h2) < 3: continue
        p1 = sum(t['pnl'] for t in h1)
        p2 = sum(t['pnl'] for t in h2)
        w1 = sum(1 for t in h1 if t['net_ret_pct'] > 0) / len(h1) * 100
        w2 = sum(1 for t in h2 if t['net_ret_pct'] > 0) / len(h2) * 100
        stable = "STABLE" if (p1 > 0 and p2 > 0) else "UNSTABLE"
        print(f"  {tab_f:12s} sn={sn_f}: H1(n={len(h1)},wr={w1:.0f}%,pnl={p1:+.0f}) H2(n={len(h2)},wr={w2:.0f}%,pnl={p2:+.0f}) [{stable}]", flush=True)

print("\nDONE", flush=True)
