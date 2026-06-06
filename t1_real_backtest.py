"""A 股 T+1 真实回测模块 - 独立可调用

策略: 信号日 (D 日涨停) → 买入日 (D+1 开盘竞价) → 卖出日 (D+2 开盘竞价)
评分: backtest_score_prev (回测专用, 简化版, 不依赖 plan_a 的 9 因子全套)

返回 dict:
{
  'summary': {胜率/平均/总盈亏/盈亏比/最大回撤/最佳/最差/EV/笔数},
  'trades': [明细 list],
  'generated_at': 时间戳
}
"""
import sys, time
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime, timedelta
from scanner import backtest_score_prev
from cache import _last_trading_date, _is_trading_day

CAPITAL_DEFAULT = 30000
COMMISSION = 0.00025 * 2  # 向后兼容 (实际从 config 导入)
SLIPPAGE = 0.001
TOP_N_DEFAULT = 3
MAX_WORKERS = 4

# 从 config 统一导入 (P1-1 重构)
from config import COMMISSION_ROUNDTRIP_PCT as _COMMISSION_PCT, SLIPPAGE_PCT as _SLIPPAGE_PCT


def _trading_dates_in_range(start_str, end_str, max_count=60):
    """返回 [start, end] 区间内所有交易日 (YYYYMMDD list, 倒序)"""
    dates = []
    cur = end_str
    while len(dates) < max_count:
        if _is_trading_day(cur):
            dates.append(cur)
        cur_dt = datetime.strptime(cur, '%Y%m%d') - timedelta(days=1)
        cur = cur_dt.strftime('%Y%m%d')
        if cur < start_str:
            break
    dates.reverse()
    return dates


def _next_trading_date(d_str, max_lookahead=10):
    """d 的下一个交易日"""
    cur = datetime.strptime(d_str, '%Y%m%d')
    for _ in range(max_lookahead):
        cur += timedelta(days=1)
        c = cur.strftime('%Y%m%d')
        if _is_trading_day(c):
            return c
    return None


def _get_open_price(code, date_str):
    """拉 d 日开盘价 - 失败返回 None"""
    prefix = 'sh' if code.startswith('6') else 'sz'
    start_fmt = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'
    for attempt in range(3):
        try:
            hist = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}',
                                          start_date=start_fmt, end_date=start_fmt)
            if hist is None or hist.empty:
                return None
            hist['日期'] = pd.to_datetime(hist['date']).dt.strftime('%Y%m%d')
            row = hist[hist['日期'] == date_str]
            if row.empty:
                return None
            return float(row.iloc[0]['open'])
        except Exception:
            time.sleep(2)
    return None


def run_t1_backtest(
    start_date: str = None,
    end_date: str = None,
    top_n: int = TOP_N_DEFAULT,
    capital: float = CAPITAL_DEFAULT,
    max_days: int = 30,
):
    """T+1 真实回测主入口

    start_date/end_date: 区间 (默认最近 30 天)
    返回: dict with summary, trades, generated_at
    """
    # 默认日期: 最近 30 个交易日
    if end_date is None:
        cur = datetime.now()
        end = cur.strftime('%Y%m%d')
        while not _is_trading_day(end):
            cur -= timedelta(days=1)
            end = cur.strftime('%Y%m%d')
    else:
        end = end_date

    if start_date is None:
        sd = datetime.strptime(end, '%Y%m%d') - timedelta(days=max_days * 2)
        start = sd.strftime('%Y%m%d')
    else:
        start = start_date

    trade_dates = _trading_dates_in_range(start, end, max_count=max_days)
    if not trade_dates:
        return {'summary': {}, 'trades': [], 'generated_at': datetime.now().isoformat(),
                'error': '区间内无交易日'}

    records = []
    skipped = []
    for d_signal in trade_dates:
        d_buy = _next_trading_date(d_signal)
        if d_buy is None or d_buy > trade_dates[-1]:
            skipped.append({'signal': d_signal, 'reason': '买入日超出区间'})
            continue
        d_sell = _next_trading_date(d_buy)
        if d_sell is None or d_sell > trade_dates[-1]:
            skipped.append({'signal': d_signal, 'reason': '卖出日超出区间'})
            continue

        try:
            prev = ak.stock_zt_pool_previous_em(date=d_signal)
            if prev is None or prev.empty:
                skipped.append({'signal': d_signal, 'reason': 'prev池空'})
                continue
            try:
                today_df = ak.stock_zt_pool_em(date=d_signal)
            except Exception:
                today_df = None
            df_res, summary = backtest_score_prev(prev, today_df=today_df, date_str=d_signal)
            if df_res is None or df_res.empty:
                skipped.append({'signal': d_signal, 'reason': '评分后空'})
                continue
            score_col = None
            for cand in ['回测评分', '综合分', '总分', '评分']:
                if cand in df_res.columns:
                    score_col = cand
                    break
            if score_col is None:
                skipped.append({'signal': d_signal, 'reason': '找不到评分列'})
                continue
            top = df_res.sort_values(score_col, ascending=False).head(top_n)
            for rank, (_, row) in enumerate(top.iterrows(), 1):
                code = str(row.iloc[1]).strip().zfill(6)
                name = str(row.iloc[2])
                sc = float(row.get(score_col, 0))
                buy_price = _get_open_price(code, d_buy)
                if buy_price is None:
                    continue
                sell_price = _get_open_price(code, d_sell)
                if sell_price is None:
                    continue
                raw_ret = (sell_price / buy_price - 1) * 100
                net_ret = raw_ret - COMMISSION * 100 - SLIPPAGE * 100
                records.append({
                    'signal_date': d_signal,
                    'buy_date': d_buy,
                    'sell_date': d_sell,
                    'rank': rank,
                    'code': code,
                    'name': name,
                    'score': round(sc, 1),
                    'buy_price': round(buy_price, 2),
                    'sell_price': round(sell_price, 2),
                    'raw_ret_pct': round(raw_ret, 2),
                    'net_ret_pct': round(net_ret, 2),
                    'pnl': round(capital * net_ret / 100, 0),
                })
        except Exception as e:
            skipped.append({'signal': d_signal, 'reason': f'错误: {str(e)[:50]}'})
        time.sleep(0.5)

    # 聚合
    if not records:
        return {
            'summary': {'trade_count': 0, 'win_rate': 0, 'avg_ret': 0,
                        'total_pnl': 0, 'plr': 0, 'max_dd': 0, 'best': 0,
                        'worst': 0, 'ev': 0},
            'trades': [],
            'skipped': skipped,
            'generated_at': datetime.now().isoformat(),
            'config': {'start': start, 'end': end, 'top_n': top_n, 'capital': capital}
        }

    rets = [r['net_ret_pct'] for r in records]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    n = len(rets)
    win_n = len(wins)
    win_avg = np.mean(wins) if wins else 0
    loss_avg = np.mean(losses) if losses else 0
    cum = np.cumsum(rets)
    peak = np.maximum.accumulate(cum)
    summary = {
        'trade_count': n,
        'win_count': win_n,
        'loss_count': n - win_n,
        'win_rate': round(win_n / n * 100, 1),
        'avg_ret': round(float(np.mean(rets)), 2),
        'win_avg': round(win_avg, 2),
        'loss_avg': round(loss_avg, 2),
        'total_pnl': round(sum(r['pnl'] for r in records), 0),
        'plr': round(abs(win_avg / loss_avg), 2) if loss_avg != 0 else 0,
        'max_dd': round(float((cum - peak).min()), 2),
        'best': round(max(rets), 2),
        'worst': round(min(rets), 2),
        'ev': round(win_n/n*win_avg + (n-win_n)/n*loss_avg, 2) if losses else 0,
        'cumulative_ret': round(float(cum[-1]), 2),
    }

    # TOP/BOTTOM 5
    sorted_trades = sorted(records, key=lambda x: -x['net_ret_pct'])
    top5 = sorted_trades[:5]
    bot5 = sorted_trades[-5:][::-1]

    return {
        'summary': summary,
        'trades': records,
        'top5': top5,
        'bottom5': bot5,
        'skipped': skipped,
        'generated_at': datetime.now().isoformat(),
        'config': {
            'start': start, 'end': end, 'top_n': top_n, 'capital': capital,
            'commission_pct': COMMISSION * 100,
            'slippage_pct': SLIPPAGE * 100,
            'strategy': 'T+1 真实 (信号日涨停 → D+1 开盘买入 → D+2 开盘卖出)',
            'scoring': 'backtest_score_prev (回测评分, 6 因子)',
        }
    }


if __name__ == '__main__':
    import json
    res = run_t1_backtest(max_days=30, top_n=3)
    print(json.dumps(res['summary'], ensure_ascii=False, indent=2))
    print(f"\n笔数: {res['summary']['trade_count']}, 跳过: {len(res['skipped'])}")
