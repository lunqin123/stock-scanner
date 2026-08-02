"""回测指标/统计工具 (2026-08-01 自 backtest_engine.py 拆分)。

包含: 确定性成交模拟 / 聚合统计(复利累计+资金曲线回撤+EV 修复) / 因子 IC 分析。
"""
import numpy as np
import pandas as pd

def _deterministic_fill(code: str, date_str: str, prob: float) -> bool:
    """确定性成交模拟: 用 (code, date) 的稳定哈希替代 random, 保证回测可复现。

    prob=1.0 恒成交, prob=0.0 恒不成交, 中间值按哈希均匀分布近似概率。
    """
    if prob >= 1.0:
        return True
    if prob <= 0.0:
        return False
    import hashlib
    h = int(hashlib.md5(f'{code}|{date_str}'.encode('utf-8')).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0 < prob
def _aggregate(records, label='backtest'):
    """聚合统计 (正确性修复 2026-08-01):
    - ev: 无亏损样本时不再恒为 0 (ev = 胜率×平均盈利 + 败率×平均亏损)
    - cumulative_ret: 改为复利累计 (prod(1+r)-1), 原简单求和保留在 cumulative_ret_sum
    - max_dd: 基于复利资金曲线的最大回撤, 而非收益求和曲线
    """
    if not records:
        return None
    rets = [float(r['net_ret_pct']) for r in records]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    n = len(rets)
    win_n = len(wins)
    win_avg_v = float(np.mean(wins)) if wins else 0.0
    loss_avg_v = float(np.mean(losses)) if losses else 0.0
    win_rate = win_n / n
    # EV 恒为 胜率×平均盈利 + 败率×平均亏损 (全赢样本下 = 胜率×平均盈利)
    ev = win_rate * win_avg_v + (1.0 - win_rate) * loss_avg_v
    # 复利资金曲线
    equity = np.cumprod(1.0 + np.asarray(rets) / 100.0)
    compound_ret = (equity[-1] - 1.0) * 100.0
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min() * 100.0)
    sum_ret = float(np.sum(rets))
    return {
        'trade_count': n,
        'win_count': win_n, 'loss_count': n - win_n,
        'win_rate': round(win_rate * 100, 1),
        'avg_ret': round(float(np.mean(rets)), 2),
        'median_ret': round(float(np.median(rets)), 2),
        'win_avg': round(win_avg_v, 2),
        'loss_avg': round(loss_avg_v, 2),
        'total_pnl': round(sum(r['pnl'] for r in records), 0),
        'plr': round(abs(win_avg_v / loss_avg_v), 2) if loss_avg_v != 0 else 0,
        'max_dd': round(max_dd, 2),
        'best': round(max(rets), 2),
        'worst': round(min(rets), 2),
        'ev': round(ev, 2),
        'cumulative_ret': round(compound_ret, 2),
        'cumulative_ret_sum': round(sum_ret, 2),
    }
def _compute_factor_ics(records: list, tab: str = 'limit-up') -> dict:
    """从交易记录计算各因子 IC (因子分 × net_ret_pct 的 Pearson 相关系数)

    根据 tab 自动识别因子列前缀:
      limit-up → f_       (plan_a 9 因子)
      zhaban   → zb_      (封板/资金/特征/换手/板块)
      dtqiaoban→ dt_      (放量/封单/连跌/换手/时间)
      reversal → rev_     (换手/连板/回调/板块/留存)
      trend    → trend_   (涨幅/换手/成交额/量比/新高/均线)

    Args:
        records: records_open 列表, 每笔含对应前缀的因子列 + net_ret_pct
        tab: tab 名称

    Returns:
        {factor_name: ic_value}
    """
    if not records or len(records) < 3:
        return {}
    import pandas as pd
    df = pd.DataFrame(records)

    # tab → 因子列前缀映射
    _PREFIX_MAP = {
        'limit-up': 'f_',
        'zhaban': 'zb_',
        'dtqiaoban': 'dt_',
        'reversal': 'rev_',
        'trend': 'trend_',
        'sector': None,
    }
    prefix = _PREFIX_MAP.get(tab)
    if prefix is None:
        return {}

    factor_cols = sorted([c for c in df.columns if c.startswith(prefix)])
    if 'net_ret_pct' not in df.columns or not factor_cols:
        return {}
    rets = df['net_ret_pct'].astype(float)
    ics = {}
    for col in factor_cols:
        vals = df[col].astype(float)
        if vals.std() < 0.01:
            continue
        c = vals.corr(rets)
        if not pd.isna(c):
            display_name = col[len(prefix):] if col.startswith(prefix) else col
            ics[display_name] = round(float(c), 4)
    return ics
