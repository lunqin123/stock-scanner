"""
权重优化脚本：基于因子 IC 分析，为各 tab 计算最优权重。

方式：
1. 对每个 tab 跑大样本回测（全部股票），提取因子分列和收益率
2. 计算每因子与收益的 IC（Spearman 秩相关）
3. 按 IC 大小比例分配权重：正 IC 高权，负 IC 低权
4. 保存最优权重到对应的 weights JSON 文件
"""
import sys, os, json, math

# 统一项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from backtest_engine import run_tab_backtest
from weight_manager import _save_tab_weights


# 各 tab 的因子列名映射（trade record 中的列名 → 因子名）
TAB_FACTOR_COLS = {
    'zhaban': ['zb_seal', 'zb_money', 'zb_feature', 'zb_turnover', 'zb_sector'],
    'dtqiaoban': ['dt_deal', 'dt_seal', 'dt_cont', 'dt_turnover', 'dt_time'],
    'reversal': ['rev_turnover', 'rev_consecutive', 'rev_pullback', 'rev_sector'],
    'trend': ['trend_chg', 'trend_turnover', 'trend_amount', 'trend_vr', 'trend_nh', 'trend_ma'],
}

# 各 tab 默认权重
TAB_DEFAULTS = {
    'zhaban': {'seal': 20, 'money': 20, 'feature': 15, 'turnover': 10, 'sector': 12},
    'dtqiaoban': {'deal': 25, 'seal': 25, 'cont': 25, 'turnover': 15, 'time': 10},
    'reversal': {'turnover': 25, 'consecutive': 30, 'pullback': 25, 'sector': 15, 'retention': 5},
    'trend': {},  # trend 权重不在此优化（用 adjust_trend_weights_from_backtest）
}

# factor_col → weight_key 映射
FACTOR_TO_WEIGHT_KEY = {
    'zhaban': {'zb_seal': 'seal', 'zb_money': 'money', 'zb_feature': 'feature',
               'zb_turnover': 'turnover', 'zb_sector': 'sector'},
    'dtqiaoban': {'dt_deal': 'deal', 'dt_seal': 'seal', 'dt_cont': 'cont',
                  'dt_turnover': 'turnover', 'dt_time': 'time'},
    'reversal': {'rev_turnover': 'turnover', 'rev_consecutive': 'consecutive',
                 'rev_pullback': 'pullback', 'rev_sector': 'sector'},
}

# 限制因子权重在 [MIN, MAX] 倍默认值之间
MIN_SCALE = 0.3
MAX_SCALE = 2.5


def compute_ic(values, returns):
    """计算因子值与收益的 Spearman 秩相关（IC）"""
    if len(values) < 5 or len(set(values)) <= 1:
        return 0.0
    import pandas as pd
    s = pd.Series(values).rank()
    r = pd.Series(returns).rank()
    corr = s.corr(r)
    return corr if not math.isnan(corr) else 0.0


def optimize_tab_weights(tab):
    """对单个 tab 进行权重优化"""
    factor_cols = TAB_FACTOR_COLS.get(tab)
    defaults = TAB_DEFAULTS.get(tab)
    if not factor_cols or not defaults:
        print(f"  [优化] {tab}: 无因子列映射，跳过")
        return None

    # 1. 跑全量回测获取所有交易的因子分
    print(f"\n{'='*60}")
    print(f"  优化 tab={tab}")
    print(f"{'='*60}")

    result = run_tab_backtest(tab, max_days=30, top_n=999, min_score=0,
                              capital=30000, use_cache=False)
    trades = result.get('comparison', {}).get('open_buy', {}).get('trades', [])
    if not trades:
        print(f"  [优化] {tab}: 无交易数据，跳过")
        return None

    print(f"  [{tab}] 总交易数: {len(trades)}")

    # 2. 计算每因子的 IC
    ics = {}
    for fcol in factor_cols:
        vals = []
        rets = []
        for t in trades:
            v = t.get(fcol)
            r = t.get('net_ret_pct')
            if v is not None and r is not None and not (isinstance(v, str)):
                try:
                    vals.append(float(v))
                    rets.append(float(r))
                except (ValueError, TypeError):
                    continue
        if len(vals) < 5:
            print(f"    {fcol}: 数据不足 ({len(vals)}笔)")
            ics[fcol] = 0.0
        else:
            ic = compute_ic(vals, rets)
            ics[fcol] = ic
            print(f"    {fcol}: IC={ic:+.4f} ({len(vals)}笔)")

    # 3. 按 IC 生成最优权重
    factor_to_key = FACTOR_TO_WEIGHT_KEY.get(tab, {})
    optimal_w = {}
    total_ic = sum(abs(ics.get(f, 0)) for f in factor_cols)

    if total_ic == 0:
        print(f"  [优化] {tab}: 所有因子 IC 为零，保留默认权重")
        return dict(defaults)

    # 计算目标权重：正 IC 因子按比例加权，负 IC 因子降权
    for fcol in factor_cols:
        key = factor_to_key.get(fcol, fcol)
        default = defaults.get(key, 10)
        ic = ics.get(fcol, 0)

        if ic > 0.02:
            # 正 IC：提高权重，IC 越大权重越高
            scale = 1.0 + ic * 10  # IC=0.1 → 2.0x, IC=0.05 → 1.5x
        elif ic < -0.02:
            # 负 IC：降低权重，但不能低于 MIN_SCALE
            scale = max(MIN_SCALE, 1.0 + ic * 10)
        else:
            # IC 接近零：保持默认
            scale = 1.0

        scale = max(MIN_SCALE, min(MAX_SCALE, scale))
        optimal_w[key] = round(default * scale, 1)

    # 4. 确保所有权重合理，归一化保持总权重和不变
    total_default = float(sum(defaults.values()))
    total_opt = float(sum(optimal_w.get(k, defaults[k]) for k in defaults))
    if total_opt > 0 and total_default > 0:
        scale_factor = total_default / total_opt
        optimal_w = {k: round(v * scale_factor, 1) for k, v in optimal_w.items()}

    for key in defaults:
        if key in optimal_w:
            optimal_w[key] = max(round(defaults[key] * MIN_SCALE, 1),
                                 min(round(defaults[key] * MAX_SCALE, 1),
                                     optimal_w[key]))
        else:
            optimal_w[key] = defaults[key]

    # 只保留 defaults 中存在的 key
    optimal_w = {k: float(v) for k, v in optimal_w.items() if k in defaults}

    print(f"\n  [{tab}] 优化前权重: {defaults}")
    print(f"  [{tab}] 优化后权重: {optimal_w}")

    return optimal_w


def _to_python_float(val):
    """递归转换 numpy float 到 Python float"""
    if hasattr(val, 'item'):
        return val.item()
    return val


def verify_weights(tab, new_weights):
    """验证新权重 vs 默认权重的性能"""
    DEFAULTS = TAB_DEFAULTS.get(tab, {})
    factor_to_key = FACTOR_TO_WEIGHT_KEY.get(tab, {})

    # 先恢复默认权重运行一次
    if DEFAULTS:
        _save_tab_weights(tab, DEFAULTS)
    r1 = run_tab_backtest(tab, max_days=30, top_n=3, min_score=50, capital=30000, use_cache=False)
    s1 = r1.get('summary', {})

    # 再用新权重运行一次
    if new_weights:
        _save_tab_weights(tab, new_weights)
    r2 = run_tab_backtest(tab, max_days=30, top_n=3, min_score=50, capital=30000, use_cache=False)
    s2 = r2.get('summary', {})

    print(f"\n  [{tab}] 性能对比:")
    print(f"    默认: trades={s1.get('trade_count',0)} WR={s1.get('win_rate',0)}% PnL={s1.get('total_pnl',0)}")
    print(f"    优化: trades={s2.get('trade_count',0)} WR={s2.get('win_rate',0)}% PnL={s2.get('total_pnl',0)}")

    # 返回效果更好的权重
    if s2.get('total_pnl', -99999) > s1.get('total_pnl', -99999):
        return new_weights
    else:
        print(f"  [{tab}] 默认权重更好，保留默认")
        return DEFAULTS


    # 优化除 trend 和 limit-up 外的所有 tab
    for tab in ['zhaban', 'dtqiaoban', 'reversal']:
        try:
            opt_w = optimize_tab_weights(tab)
            if opt_w:
                # 确保 JSON 可序列化
                opt_w = {k: _to_python_float(v) for k, v in opt_w.items()}
                _save_tab_weights(tab, opt_w)
                print(f"  ✓ {tab} 权重已保存: {opt_w}")
        except Exception as e:
            import traceback
            print(f"  ✗ {tab} 优化失败: {e}")
            traceback.print_exc()

    # 对 limit-up (plan_a) 单独处理：只保留默认权重 + 禁用自动调权
    # 因为 limit-up 的 trade 不含因子分列，无法做因子级 IC 分析
    print(f"\n{'='*60}")
    print(f"  limit-up (plan_a): 保留默认权重 (权重已稳定)")
    print(f"  trend: 使用现有 trend_weights.json (由 adjust_trend 维护)")
    print(f"{'='*60}")

    # 清理回测结果 JSON 的损坏数据（太长了）
    try:
        bt_path = os.path.join(os.path.dirname(__file__), 'data', 'backtest_results.json')
        if os.path.exists(bt_path):
            # 截断到 10000 行
            with open(bt_path, 'r') as f:
                lines = f.readlines()
            if len(lines) > 10000:
                with open(bt_path, 'w') as f:
                    f.writelines(lines[:10000])
                    f.write('\n  {"_truncated": true}\n]')
                print(f"  ✓ backtest_results.json 从 {len(lines)} 行截断到 10000 行")
    except Exception as e:
        print(f"  截断 backtest_results.json 失败: {e}")
