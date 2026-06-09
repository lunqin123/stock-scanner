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
from cache import (
    _last_trading_date, _is_trading_day,
    get as _cache_get, put as _cache_put,
    persistent_get as _persistent_get, persistent_put as _persistent_put,
    daily_get as _daily_get, daily_set as _daily_set, make_key,
)
from datetime import date as _date
from data_manager import save_backtest_result as _save_backtest_result

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


def _get_prev_pool_cached(date_str):
    """拉涨停前池(昨日涨停), 用 2h 缓存(历史数据当天稳定)"""
    key = f"t1_prev_pool_{date_str}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    df = ak.stock_zt_pool_previous_em(date=date_str)
    if df is not None and not df.empty:
        _cache_put(key, df)
    return df


def _get_today_pool_cached(date_str):
    """拉今日涨停池(用 score 里 sector 分析), 用 2h 缓存"""
    key = f"t1_today_pool_{date_str}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        df = ak.stock_zt_pool_em(date=date_str)
    except Exception:
        df = None
    if df is not None and not df.empty:
        _cache_put(key, df)
    return df


def _get_daily_ohlcv(code, date_str):
    """获取单日完整 OHLCV（开/高/低/收/量/额/换手），返回 dict 或 None

    数据源级联：东方财富 → 腾讯 → 缓存 None。
    两种 API 任一挂了自动降级，不阻塞回测。
    """
    key = f"t1_ohlcv_{code}_{date_str}"
    cached = _cache_get(key)
    if cached is not None:
        return cached if cached != '__NONE__' else None

    def _parse_em(df):
        """东方财富格式"""
        row = df.iloc[0]
        return {
            'open': float(row['开盘']), 'close': float(row['收盘']),
            'high': float(row['最高']), 'low': float(row['最低']),
            'volume': int(row['成交量']), 'amount': float(row['成交额']),
            'turnover': float(row['换手率']), 'change_pct': float(row['涨跌幅']),
        }

    def _parse_tx(df):
        """腾讯格式"""
        row = df.iloc[0]
        return {
            'open': float(row['open']), 'close': float(row['close']),
            'high': float(row['high']), 'low': float(row['low']),
            'volume': int(row['volume']), 'amount': float(row.get('amount', 0)),
            'turnover': float(row.get('turnover', 0)),
            'change_pct': float(row.get('change', row.get('涨跌幅', 0))),
        }

    fmt = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'
    prefix = 'sh' if code.startswith('6') else 'sz'

    # ⑴ 东方财富
    for attempt in range(2):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period='daily',
                                     start_date=date_str, end_date=date_str, adjust='')
            if df is not None and not df.empty:
                result = _parse_em(df)
                _cache_put(key, result)
                return result
            break
        except Exception:
            time.sleep(1)

    # ⑵ 腾讯降级
    for attempt in range(2):
        try:
            df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}',
                                        start_date=fmt, end_date=fmt)
            if df is not None and not df.empty:
                df['日期'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
                row_df = df[df['日期'] == date_str]
                if not row_df.empty:
                    result = _parse_tx(row_df)
                    _cache_put(key, result)
                    return result
            break
        except Exception:
            time.sleep(1)

    _cache_put(key, '__NONE__')
    return None


def _get_ohlcv_batch(code, dates):
    """批量获取多日 OHLCV，合并为一次 API 调用

    先查逐日缓存 t1_ohlcv_{code}_{date}，缺失的合为一个日期范围拉取。
    缓存 key 兼容 _get_daily_ohlcv，新旧混用无冲突。
    返回 {date_str: ohlcv_dict}，缺失日期不在结果中。
    """
    today_str = _date.today().strftime('%Y%m%d')
    result = {}
    missing = []
    for d in dates:
        key = f"t1_ohlcv_{code}_{d}"
        # 历史日期优先读持久化缓存 (无 TTL), 今天走 2h TTL
        if d != today_str:
            cached = _persistent_get(key)
            if cached is None:
                cached = _cache_get(key)  # fallback 2h 缓存
        else:
            cached = _cache_get(key)
        if cached is not None:
            if cached != '__NONE__':
                result[d] = cached
        else:
            missing.append(d)
    if not missing:
        return result

    start, end = min(missing), max(missing)

    def _ohlcv_cache_df(df, is_tx=False):
        for _, row in df.iterrows():
            if is_tx:
                d = str(row.get('日期', '')).replace('-', '')
                if d not in missing:
                    continue
                o = {'open': float(row['open']), 'close': float(row['close']),
                     'high': float(row['high']), 'low': float(row['low']),
                     'volume': int(row.get('volume', 0) or 0),
                     'amount': float(row.get('amount', 0) or 0),
                     'turnover': float(row.get('turnover', 0) or 0),
                     'change_pct': float(row.get('change', row.get('涨跌幅', 0)) or 0)}
            else:
                d = str(row['日期']).replace('-', '')
                if d not in missing:
                    continue
                o = {'open': float(row['开盘']), 'close': float(row['收盘']),
                     'high': float(row['最高']), 'low': float(row['最低']),
                     'volume': int(row['成交量']), 'amount': float(row['成交额']),
                     'turnover': float(row['换手率']), 'change_pct': float(row['涨跌幅'])}
            _cache_put(f"t1_ohlcv_{code}_{d}", o)
            if d != today_str:
                _persistent_put(f"t1_ohlcv_{code}_{d}", o)
            result[d] = o

    # 腾讯源优先 (服务器东方财富被封, 腾讯正常)
    prefix = 'sh' if code.startswith('6') else 'sz'
    fmt_s = f'{start[:4]}-{start[4:6]}-{start[6:8]}'
    fmt_e = f'{end[:4]}-{end[4:6]}-{end[6:8]}'
    for attempt in range(2):
        try:
            df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}',
                                        start_date=fmt_s, end_date=fmt_e)
            if df is not None and not df.empty:
                df['日期'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
                _ohlcv_cache_df(df, is_tx=True)
                for d in missing:
                    if d not in result:
                        _cache_put(f"t1_ohlcv_{code}_{d}", '__NONE__')
                        if d != today_str:
                            _persistent_put(f"t1_ohlcv_{code}_{d}", '__NONE__')
                return result
            break
        except Exception:
            time.sleep(0.5)

    # 东方财富降级 (本地/国内环境更快)
    for attempt in range(1):  # 只试1次, 不在服务器上反复重试
        try:
            df = ak.stock_zh_a_hist(symbol=code, period='daily',
                                     start_date=start, end_date=end, adjust='')
            if df is not None and not df.empty:
                _ohlcv_cache_df(df)
                for d in missing:
                    if d not in result:
                        _cache_put(f"t1_ohlcv_{code}_{d}", '__NONE__')
                        if d != today_str:
                            _persistent_put(f"t1_ohlcv_{code}_{d}", '__NONE__')
                return result
            break
        except Exception:
            pass

    for d in missing:
        _cache_put(f"t1_ohlcv_{code}_{d}", '__NONE__')
        if d != today_str:
            _persistent_put(f"t1_ohlcv_{code}_{d}", '__NONE__')
    return result


def _is_limit_open(buy_ohlcv, signal_close):
    """判断 D+1 开盘是否一字涨停（买不到）

    开盘价 ≥ 昨日收盘 × 1.095 视为涨停开盘。
    """
    if buy_ohlcv is None or signal_close is None or signal_close <= 0:
        return False
    gap = (buy_ohlcv['open'] / signal_close - 1) * 100
    return gap >= 9.5


def run_t1_backtest(
    start_date: str = None,
    end_date: str = None,
    top_n: int = TOP_N_DEFAULT,
    capital: float = CAPITAL_DEFAULT,
    max_days: int = 5,
    use_cache: bool = True,
):
    """T+1 真实回测主入口

    start_date/end_date: 区间 (默认最近 30 天)
    use_cache: True 走 daily_set/daily_get 缓存(盘后写, 多次刷新秒回)
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

    # 整体结果缓存(盘后命中, 多次刷新秒回)
    if use_cache:
        cache_key = make_key("t1", "result",
                             start=start, end=end, top_n=top_n, capital=int(capital))
        cached = _daily_get(cache_key)
        if cached and 'summary' in cached:
            return cached

    trade_dates = _trading_dates_in_range(start, end, max_count=max_days)
    if not trade_dates:
        return {'summary': {}, 'trades': [], 'generated_at': datetime.now().isoformat(),
                'error': '区间内无交易日'}

    records_open = []    # 策略A: D+1 开盘买
    records_close = []   # 策略B: D+1 尾盘买（收盘买）
    skipped = []
    unbuyable_count = 0  # 因涨停开盘无法买入的笔数

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
            prev = _get_prev_pool_cached(d_signal)
            if prev is None or prev.empty:
                skipped.append({'signal': d_signal, 'reason': 'prev池空'})
                continue
            df_res, summary = backtest_score_prev(prev, date_str=d_signal)
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
            # 排序列：评分降序，名称列升序做 tiebreaker
            name_sort_col = '名称' if '名称' in df_res.columns else (df_res.columns[2] if len(df_res.columns) > 2 else df_res.columns[-1])
            top = df_res.sort_values([score_col, name_sort_col], ascending=[False, True], kind='mergesort').head(top_n)
            code_col = '代码' if '代码' in df_res.columns else (df_res.columns[1] if len(df_res.columns) > 1 else df_res.columns[0])
            name_col_r = '名称' if '名称' in df_res.columns else (df_res.columns[2] if len(df_res.columns) > 2 else df_res.columns[1])
            for rank, (_, row) in enumerate(top.iterrows(), 1):
                code = str(row.get(code_col, '') or row.iloc[0]).strip().zfill(6)
                name = str(row.get(name_col_r, '') or row.iloc[0])
                sc = float(row.get(score_col, 0))

                # ── 批量获取 OHLCV（一次 API 调用覆盖 3 个日期） ──
                ohlcv_map = _get_ohlcv_batch(code, [d_signal, d_buy, d_sell])
                signal_ohlcv = ohlcv_map.get(d_signal)
                buy_ohlcv = ohlcv_map.get(d_buy)
                sell_ohlcv = ohlcv_map.get(d_sell)
                if not all([signal_ohlcv, buy_ohlcv, sell_ohlcv]):
                    continue

                signal_close = signal_ohlcv['close']
                buyable = not _is_limit_open(buy_ohlcv, signal_close)
                gap_pct = round((buy_ohlcv['open'] / signal_close - 1) * 100, 1)
                if not buyable:
                    unbuyable_count += 1

                # ── 公共日内信息 ──
                intraday = {
                    'buy_high': round(buy_ohlcv['high'], 2),
                    'buy_low': round(buy_ohlcv['low'], 2),
                    'buy_close': round(buy_ohlcv['close'], 2),
                    'buy_volume': buy_ohlcv['volume'],
                    'buy_turnover': round(buy_ohlcv['turnover'], 2),
                    'sell_high': round(sell_ohlcv['high'], 2),
                    'sell_low': round(sell_ohlcv['low'], 2),
                    'sell_close': round(sell_ohlcv['close'], 2),
                    'signal_close': round(signal_close, 2),
                    'gap_open_pct': gap_pct,
                    'buyable': buyable,
                }

                sell_px = sell_ohlcv['open']  # 统一卖价

                # ── 策略A: 开盘买（D+1 开盘买入，D+2 开盘卖出） ──
                if buyable:
                    buy_px = buy_ohlcv['open']
                    raw_ret = (sell_px / buy_px - 1) * 100
                    net_ret = raw_ret - _COMMISSION_PCT - _SLIPPAGE_PCT
                    records_open.append({
                        'signal_date': d_signal, 'buy_date': d_buy, 'sell_date': d_sell,
                        'rank': rank, 'code': code, 'name': name, 'score': round(sc, 1),
                        'buy_price': round(buy_px, 2), 'sell_price': round(sell_px, 2),
                        'raw_ret_pct': round(raw_ret, 2), 'net_ret_pct': round(net_ret, 2),
                        'pnl': round(capital * net_ret / 100, 0), **intraday,
                    })

                # ── 策略B: 尾盘买（D+1 收盘买入，D+2 开盘卖出） ──
                close_buy_px = buy_ohlcv['close']
                raw_ret_c = (sell_px / close_buy_px - 1) * 100
                net_ret_c = raw_ret_c - _COMMISSION_PCT - _SLIPPAGE_PCT
                records_close.append({
                    'signal_date': d_signal, 'buy_date': d_buy, 'sell_date': d_sell,
                    'rank': rank, 'code': code, 'name': name, 'score': round(sc, 1),
                    'buy_price': round(close_buy_px, 2), 'sell_price': round(sell_px, 2),
                    'raw_ret_pct': round(raw_ret_c, 2), 'net_ret_pct': round(net_ret_c, 2),
                    'pnl': round(capital * net_ret_c / 100, 0), **intraday,
                })
        except Exception as e:
            skipped.append({'signal': d_signal, 'reason': f'错误: {str(e)[:50]}'})
        time.sleep(0.5)

    # ── 聚合辅助 ──
    def _aggregate(records, label):
        if not records:
            return None
        rets = [r['net_ret_pct'] for r in records]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        n = len(rets)
        win_n = len(wins)
        win_avg_v = np.mean(wins) if wins else 0
        loss_avg_v = np.mean(losses) if losses else 0
        cum = np.cumsum(rets)
        peak = np.maximum.accumulate(cum)
        return {
            'trade_count': n,
            'win_count': win_n, 'loss_count': n - win_n,
            'win_rate': round(win_n / n * 100, 1),
            'avg_ret': round(float(np.mean(rets)), 2),
            'win_avg': round(win_avg_v, 2),
            'loss_avg': round(loss_avg_v, 2),
            'total_pnl': round(sum(r['pnl'] for r in records), 0),
            'plr': round(abs(win_avg_v / loss_avg_v), 2) if loss_avg_v != 0 else 0,
            'max_dd': round(float((cum - peak).min()), 2),
            'best': round(max(rets), 2),
            'worst': round(min(rets), 2),
            'ev': round(win_n/n*win_avg_v + (n-win_n)/n*loss_avg_v, 2) if losses else 0,
            'cumulative_ret': round(float(cum[-1]), 2),
        }

    sum_open = _aggregate(records_open, '开盘买')
    sum_close = _aggregate(records_close, '尾盘买')

    # 两个策略都空才返回空
    if sum_open is None and sum_close is None:
        return {
            'summary': {'trade_count': 0, 'win_rate': 0, 'avg_ret': 0,
                        'total_pnl': 0, 'plr': 0, 'max_dd': 0, 'best': 0,
                        'worst': 0, 'ev': 0},
            'trades': [],
            'skipped': skipped,
            'generated_at': datetime.now().isoformat(),
            'config': {'start': start, 'end': end, 'top_n': top_n, 'capital': capital}
        }

    # TOP/BOTTOM 5（用开盘买的排序）
    sorted_trades = sorted(records_open, key=lambda x: -x['net_ret_pct']) if records_open else []
    top5 = sorted_trades[:5]
    bot5 = sorted_trades[-5:][::-1]

    result = {
        'summary': sum_open or sum_close,
        'trades': records_open or records_close,
        'top5': top5, 'bottom5': bot5,
        'skipped': skipped,
        'generated_at': datetime.now().isoformat(),
        'config': {
            'start': start, 'end': end, 'top_n': top_n, 'capital': capital,
            'commission_pct': _COMMISSION_PCT,
            'slippage_pct': _SLIPPAGE_PCT,
            'strategy': 'T+1 真实 (信号日涨停 → D+1 开盘买入 → D+2 开盘卖出)',
            'scoring': 'backtest_score_prev (回测评分, 6 因子)',
        },
        # 新增：双策略对比
        'comparison': {
            'open_buy': {
                'summary': sum_open,
                'trades': records_open,
            },
            'close_buy': {
                'summary': sum_close,
                'trades': records_close,
            },
            'unbuyable_count': unbuyable_count,
        },
    }
    # 整体结果缓存(盘后写, 跨日失效; use_cache 才写)
    if use_cache:
        cache_key = make_key("t1", "result",
                             start=start, end=end, top_n=top_n, capital=int(capital))
        _daily_set(cache_key, result)
    # 持久化回测结果到 data/backtest_results.json（跨日保留）
    try:
        _save_backtest_result(result)
    except Exception as _e:
        print(f"  [回测持久化] 写入失败: {_e}", file=sys.stderr)
    return result


def _big_winrate_banner(wr, label, trades, cum_ret):
    """生成一个大大的胜率横幅"""
    bar_w = 28
    fill = int(wr / 100 * bar_w) if wr > 0 else 0
    bar = '#' * fill + ' ' * (bar_w - fill)
    grade = ( 'S' if wr >= 70 else 'A' if wr >= 55 else
              'B' if wr >= 40 else 'C' if wr >= 25 else 'D' )
    return '\n'.join([
        f"  {'=' * 50}",
        f"   {label}",
        f"  {'=' * 50}",
        f"     [{bar}]",
        f"     {wr:>5.1f}%  WIN RATE  [{grade}]   {trades}笔  累计{cum_ret:+.2f}%",
        f"  {'=' * 50}",
    ])


if __name__ == '__main__':
    import json
    res = run_t1_backtest(max_days=30, top_n=3)
    cmp = res.get('comparison', {})
    ob = cmp.get('open_buy', {}).get('summary', {})
    cb = cmp.get('close_buy', {}).get('summary', {})
    uc = cmp.get('unbuyable_count', 0)

    # ── 大大大的胜率横幅 ──
    if ob:
        print(_big_winrate_banner(
            ob['win_rate'], '策略A: 开盘买 (D+1开盘 -> D+2开盘)',
            ob['trade_count'], ob['cumulative_ret']))
    if cb:
        print(_big_winrate_banner(
            cb['win_rate'], '策略B: 尾盘买 (D+1收盘 -> D+2开盘)',
            cb['trade_count'], cb['cumulative_ret']))
    if uc:
        print(f'  [!] 因一字涨停无法买入: {uc} 笔')

    sep = '=' * 55
    print(f'\n{sep}')
    print(f'  策略对比: 开盘买 vs 尾盘买')
    print(f'  区间: {res["config"]["start"]} ~ {res["config"]["end"]}')
    print(f'  TOP{res["config"]["top_n"]} | 因涨停开盘无法买入: {uc} 笔')
    print(f'{sep}')

    if ob:
        print(f'\n  策略A: 开盘买（D+1 开盘 → D+2 开盘）')
        print(f'  {"笔数:":<8} {ob["trade_count"]}')
        print(f'  {"胜率:":<8} {ob["win_rate"]}% ({ob["win_count"]}赢/{ob["loss_count"]}亏)')
        print(f'  {"总盈亏:":<8} ¥{ob["total_pnl"]:+,.0f}')
        print(f'  {"累计收益:":<8} {ob["cumulative_ret"]:+.2f}%')
        print(f'  {"平均收益:":<8} {ob["avg_ret"]:+.2f}%')
        print(f'  {"盈亏比:":<8} {ob["plr"]}')
        print(f'  {"最大回撤:":<8} {ob["max_dd"]:.2f}%')
        print(f'  {"期望值:":<8} {ob["ev"]:+.2f}%')
        print(f'  {"最优:":<8} {ob["best"]:+.2f}%  最差: {ob["worst"]:+.2f}%')
    else:
        print(f'\n  策略A: 开盘买 — 无有效交易（全部涨停开盘无法买入）')

    if cb:
        print(f'\n  策略B: 尾盘买（D+1 收盘 → D+2 开盘）')
        print(f'  {"笔数:":<8} {cb["trade_count"]}')
        print(f'  {"胜率:":<8} {cb["win_rate"]}% ({cb["win_count"]}赢/{cb["loss_count"]}亏)')
        print(f'  {"总盈亏:":<8} ¥{cb["total_pnl"]:+,.0f}')
        print(f'  {"累计收益:":<8} {cb["cumulative_ret"]:+.2f}%')
        print(f'  {"平均收益:":<8} {cb["avg_ret"]:+.2f}%')
        print(f'  {"盈亏比:":<8} {cb["plr"]}')
        print(f'  {"最大回撤:":<8} {cb["max_dd"]:.2f}%')
        print(f'  {"期望值:":<8} {cb["ev"]:+.2f}%')
        print(f'  {"最优:":<8} {cb["best"]:+.2f}%  最差: {cb["worst"]:+.2f}%')
    print(f'\n{sep}')
    print(f'  跳过: {len(res.get("skipped", []))} 个信号日')
    print(f'{sep}\n')

    # 详细: 打印开盘买明细
    if ob and ob['trade_count'] > 0:
        print(f'\n  ── 开盘买逐笔明细 ──')
        print(f'  {"信号日":<10} {"代码":<8} {"名称":<8} {"评分":<6} {"买入":<8} {"卖出":<8} {"收益率":<8} {"可买":<6} {"开盘溢价":<8}')
        for t in ob.get('trades', []):
            print(f'  {t["signal_date"]:<10} {t["code"]:<8} {t["name"]:<8} {t["score"]:<6.1f} '
                  f'{t["buy_price"]:<8.2f} {t["sell_price"]:<8.2f} {t["net_ret_pct"]:<+8.2f}% '
                  f'{"Y" if t.get("buyable", True) else "N":<6} {t.get("gap_open_pct", 0):<+8.1f}%')
