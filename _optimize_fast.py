#!/usr/bin/env python3
"""快速智能权重优化 — 直接调用智能优化API，每个tab迭代3轮"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

TABS = ['limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal']
results = {}

# 直接调用app中的优化函数
from app import _load_tab_weights_smart, _save_tab_weights_smart, _get_market_regime_adjustment, _optimize_weights_icir
from backtest_engine import run_tab_backtest, _compute_factor_ics

print("=" * 70)
print("快速智能权重优化 (IC驱动 + 市场逻辑 + 多轮迭代)")
print("=" * 70)

for tab in TABS:
    print(f"\n{'='*60}")
    print(f"  Tab: {tab}")
    print(f"{'='*60}")

    # 阶段1: 快速策略参数扫描 (buy_time + sell_n)
    print("  [阶段1] 策略参数扫描...")
    best_ev = -999
    best_params = {}
    buy_times = ['close', 'open'] if tab == 'limit-up' else ['open']

    for bt in buy_times:
        for sn in [1, 2, 3]:
            try:
                r = run_tab_backtest(tab=tab, max_days=30, top_n=3,
                                     capital=30000, use_cache=False,
                                     buy_time=bt, sell_n=sn, min_score=50)
                s = r.get('summary', {})
                ev = s.get('ev', 0) or 0
                wr = s.get('win_rate', 0) or 0
                trades = s.get('trade_count', 0) or 0
                print(f"    {bt:5s} sn={sn} → EV={ev:+.2f}% WR={wr:.1f}% {trades}笔")
                if trades >= 3 and ev > best_ev:
                    best_ev = ev
                    best_params = {'buy_time': bt, 'sell_n': sn, 'min_score': 50}
            except Exception:
                pass

    if not best_params:
        print("  ⚠️ 无有效交易，跳过")
        continue

    print(f"  ★ 最优策略: {best_params}")

    # 阶段2: 迭代调权 (最多3轮)
    print("  [阶段2] 智能权重优化(ICIR+市场逻辑)...")
    current_w = _load_tab_weights_smart(tab)
    market_adj = _get_market_regime_adjustment()
    print(f"    市场: {market_adj.get('_sentiment',{}).get('level','N/A')} "
          f"| 状态: {market_adj.get('_regime','N/A')}")

    best_w = dict(current_w)
    best_bt_ev = best_ev

    for rd in range(3):
        bt = run_tab_backtest(tab=tab, max_days=30, top_n=3,
                              capital=30000, use_cache=False,
                              buy_time=best_params['buy_time'],
                              sell_n=best_params['sell_n'],
                              min_score=best_params['min_score'])
        trades = bt.get('trades', [])
        s = bt.get('summary', {})
        cur_ev = s.get('ev', 0) or 0

        ics = _compute_factor_ics(trades, tab=tab)
        ice_str = ' | '.join(f'{k}={v:+.3f}' for k,v in sorted(ics.items(), key=lambda x:-abs(x[1]))[:5]) if ics else '无IC'
        print(f"    第{rd+1}轮: EV={cur_ev:+.2f}% IC: {ice_str}")

        if rd < 2:  # 前两轮调权
            new_w, msg = _optimize_weights_icir(tab, current_w, trades, ics, market_adj)
            if new_w != current_w:
                _save_tab_weights_smart(tab, new_w)
                current_w = new_w
            else:
                print(f"    权重收敛，停止迭代")
                break
        else:  # 最后一轮验证
            if cur_ev > best_bt_ev:
                best_w = dict(current_w)
                best_bt_ev = cur_ev
                print(f"    ✅ 迭代改善: EV {best_ev:+.2f}% → {cur_ev:+.2f}%")
            else:
                # 恢复最优权重
                _save_tab_weights_smart(tab, best_w)
                current_w = best_w
                print(f"    ⚠️ 未改善，恢复上一轮权重")

    results[tab] = {
        'strategy': best_params,
        'final_weights': dict(current_w),
        'final_ev': round(best_bt_ev, 2),
    }

# 总结
print(f"\n{'='*70}")
print("优化总结")
print(f"{'='*70}")
for tab in TABS:
    r = results.get(tab, {})
    strat = r.get('strategy', {})
    print(f"\n  {tab}:")
    print(f"    策略: buy_time={strat.get('buy_time','?')} sell_n={strat.get('sell_n','?')} ms=50")
    print(f"    EV={r.get('final_ev',0):+.2f}%")
    ws = r.get('final_weights', {})
    print(f"    权重: {dict(sorted(ws.items()))}")

# 输出前端可读配置
print(f"\n{'='*70}")
print("前端 tab 默认策略配置 (更新到 app.js)")
print(f"{'='*70}")
for tab in TABS:
    r = results.get(tab, {})
    strat = r.get('strategy', {})
    print(f"'{tab}': {{'buy_time': '{strat.get('buy_time','open')}', 'sell_n': {strat.get('sell_n', 3)}, 'min_score': {strat.get('min_score', 50)}}},")

# 清理
if os.path.exists('_optimize_results.json'):
    with open('_optimize_results.json', 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
