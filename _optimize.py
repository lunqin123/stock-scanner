#!/usr/bin/env python3
"""全参数网格优化 + 智能权重优化 一站式脚本
运行: python _optimize.py (在服务器上)
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── 配置 ───
TABS = ['limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal']
DAYS = 30
TOP_N = 3
CAPITAL = 30000

results = {}

# ════════════════════════════════════
# 阶段1: 最优交易策略搜索 (buy_time + sell_n + min_score)
# ════════════════════════════════════

print("=" * 70)
print("阶段1: 最优交易策略参数搜索")
print("=" * 70)

from backtest_engine import run_tab_backtest

for tab in TABS:
    print(f"\n{'─'*60}")
    print(f"  Tab: {tab}")
    print(f"{'─'*60}")

    best_ev = -999
    best_params = {}
    tab_results = []

    # 买入时间: open / close (取决于 tab 特性)
    buy_times = ['open']
    if tab == 'limit-up':
        buy_times = ['close', 'open']  # 涨停两个都试

    for bt in buy_times:
        for sn in [1, 2, 3, 5]:
            for ms in [0, 30, 50, 60, 70]:
                try:
                    r = run_tab_backtest(
                        tab=tab, max_days=DAYS, top_n=TOP_N,
                        capital=CAPITAL, use_cache=False,
                        buy_time=bt, sell_n=sn, min_score=ms,
                    )
                    s = r.get('summary', {})
                    ev = s.get('ev', 0) or 0
                    wr = s.get('win_rate', 0) or 0
                    pnl = s.get('total_pnl', 0) or 0
                    trades = s.get('trade_count', 0) or 0

                    entry = {
                        'buy_time': bt, 'sell_n': sn, 'min_score': ms,
                        'ev': round(ev, 2), 'win_rate': round(wr, 1),
                        'total_pnl': round(pnl, 0), 'trades': trades,
                    }
                    tab_results.append(entry)

                    if trades >= 3 and ev > best_ev:
                        best_ev = ev
                        best_params = entry

                    print(f"  {bt:5s} sn={sn} ms={ms:2d} → "
                          f"EV={ev:+.2f}% WR={wr:.1f}% "
                          f"盈亏={pnl:+,.0f} {trades}笔")
                except Exception as e:
                    pass

    results[tab] = {
        'best': best_params,
        'all': tab_results,
    }
    print(f"\n  ★ 最优: {best_params}")

# ════════════════════════════════════
# 阶段2: 使用最优参数运行智能优化
# ════════════════════════════════════

print("\n" + "=" * 70)
print("阶段2: 智能权重优化 (使用最优策略参数)")
print("=" * 70)

for tab in TABS:
    best = results[tab]['best']
    if not best or best.get('trades', 0) < 3:
        print(f"\n  {tab}: 跳过(无足够交易)")
        continue

    print(f"\n{'─'*60}")
    print(f"  {tab}: 最优策略 {best}")
    print(f"{'─'*60}")

    # 使用最优参数跑回测获取因子 IC
    bt = run_tab_backtest(
        tab=tab, max_days=DAYS * 2, top_n=TOP_N,
        capital=CAPITAL, use_cache=False,
        buy_time=best['buy_time'],
        sell_n=best['sell_n'],
        min_score=best['min_score'],
    )
    trades = bt.get('trades', [])
    s = bt.get('summary', {})
    print(f"  当前回测: EV={s.get('ev',0):+.2f}% WR={s.get('win_rate',0):.1f}% "
          f"{s.get('trade_count',0)}笔")

    # 计算 IC
    from backtest_engine import _compute_factor_ics
    ics = _compute_factor_ics(trades, tab=tab)
    if ics:
        print(f"  因子IC: {' | '.join(f'{k}={v:+.4f}' for k,v in sorted(ics.items(), key=lambda x:-abs(x[1]))[:6])}")

    # ═══ 手动智能调权（IC 驱动 + 市场逻辑）═══
    from app import _load_tab_weights_smart, _save_tab_weights_smart, _get_market_regime_adjustment, _optimize_weights_icir

    current_w = _load_tab_weights_smart(tab)
    print(f"  当前权重: {current_w}")

    market_adj = _get_market_regime_adjustment()
    print(f"  市场情绪: {market_adj.get('_sentiment',{}).get('level','N/A')} "
          f"| 市场状态: {market_adj.get('_regime','N/A')}")

    new_w, msg = _optimize_weights_icir(tab, current_w, trades, ics, market_adj)

    if new_w != current_w:
        _save_tab_weights_smart(tab, new_w)
        print(f"  新权重: {new_w}")
        print(f"  调权详情: {msg[:300]}")

        # 验证新权重
        bt2 = run_tab_backtest(
            tab=tab, max_days=DAYS * 2, top_n=TOP_N,
            capital=CAPITAL, use_cache=False,
            buy_time=best['buy_time'],
            sell_n=best['sell_n'],
            min_score=best['min_score'],
        )
        s2 = bt2.get('summary', {})
        old_ev = s.get('ev', 0) or 0
        new_ev = s2.get('ev', 0) or 0
        print(f"  验证: EV {old_ev:+.2f}% → {new_ev:+.2f}% "
              f"({'改善! ✓' if new_ev > old_ev else '未改善'})")

        # 如果未改善，恢复旧权重
        if new_ev <= old_ev:
            _save_tab_weights_smart(tab, current_w)
            print(f"  → 恢复旧权重")
    else:
        print(f"  权重无需调整: {msg}")

# ════════════════════════════════════
# 总结
# ════════════════════════════════════

print("\n" + "=" * 70)
print("优化总结")
print("=" * 70)

for tab in TABS:
    best = results[tab].get('best', {})
    print(f"\n  {tab}:")
    print(f"    最优参数: buy_time={best.get('buy_time','?')} "
          f"sell_n={best.get('sell_n','?')} min_score={best.get('min_score','?')}")
    print(f"    EV={best.get('ev',0):+.2f}% WR={best.get('win_rate',0):.1f}% "
          f"盈亏={best.get('total_pnl',0):+,.0f} {best.get('trades',0)}笔")

    # 展示最终权重
    w = _load_tab_weights_smart(tab)
    print(f"    最终权重: {dict(sorted(w.items()))}")

print(f"\n{'='*70}")
print("优化完成! 记得 `rm _optimize.py` 清理临时文件")
print(f"{'='*70}")
