#!/usr/bin/env python3
"""回测驱动的预测概率估计器 (2026-08-01)

用修复后的回测引擎 (backtest.backtest_engine.run_tab_backtest) 对每个榜构建
"评分分档 → 概率" 映射, 供前端卡片展示:

- 次日上涨概率: 买入后第 2 个交易日收盘 > 买入价
- 低开高走概率: 次日低开 (开盘 < 信号日收盘) 且 当日收盘 > 开盘 (条件概率)
- 5日上涨概率: 信号日往后第 5 个交易日收盘 > 买入价 (≈未来一周)

样本不足的分档 (n<5) 回退到总体概率, 并始终返回样本量 n, 保证诚实展示。
数据: 回测 trades (次日/低开高走) + 逐代码历史 K 线 (5 日收盘)。
缓存: daily JSON (make_key('proba','v1',tab=...)), 盘中不重算;
      首次请求触发后台构建, 前端轮询 "生成中" 状态。

用法:
  python -m scoring.probability_estimator --build limit-up   # 手动构建
"""
import io
import os
import sys
import threading
from datetime import datetime, timedelta

import pandas as pd

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from cache import daily_get, daily_set, make_key, _trading_date

# 概率缓存版本: 改口径/分档时 +1
PROBA_CACHE_VER = 1

# 评分分档边界 (左闭右开), 最后一档到 101
BAND_EDGES = [(0, 60), (60, 70), (70, 80), (80, 90), (90, 101)]
BAND_MIN_N = 5          # 分档样本下限, 不足回退总体

# 各榜回测参数 (min_score 与调权闭环保持一致)
TAB_MIN_SCORE = {'limit-up': 65, 'trend': 50, 'zhaban': 50,
                 'dtqiaoban': 50, 'reversal': 50}

_BUILD_LOCK = threading.Lock()
_BUILDING = set()


def _window(max_days=30):
    """最近 max_days 个交易日 (start, end) YYYYMMDD"""
    from backtest.backtest_engine import _trading_dates_in_range
    end = _trading_date().replace('-', '')
    start = (datetime.strptime(end, '%Y%m%d') - timedelta(days=max_days * 2)).strftime('%Y%m%d')
    dates = _trading_dates_in_range(start, end, max_count=max_days)
    if not dates:
        return start, end
    return dates[0], dates[-1]


def _cache_key(tab):
    return make_key('proba', 'v1', version=PROBA_CACHE_VER, tab=tab)


def _collect_trades(tab, start, end):
    """跑 30 天回测, 返回结构化 trades 列表。"""
    from backtest.backtest_engine import run_tab_backtest
    res = run_tab_backtest(
        tab=tab, start_date=start, end_date=end, top_n=3,
        min_score=TAB_MIN_SCORE.get(tab, 50), use_cache=False, capital=30000,
        buy_time='close' if tab == 'limit-up' else 'open')
    out = []
    for t in res.get('trades', []):
        # 交易记录为拍平结构 (engine 把 intraday 展开到 rec 顶层)
        sig_close = t.get('signal_close')
        if not sig_close:
            continue
        buy_px = t.get('buy_price')
        if not buy_px:
            continue
        if tab == 'limit-up':
            # 尾盘买: 次日开盘由 raw_ret 反推, 次日收盘 = sell_close
            next_open = buy_px * (1 + (t.get('raw_ret_pct') or 0) / 100.0)
            next_close = t.get('sell_close')
        else:
            # 开盘买: 次日开盘 = 买入开盘, 次日收盘 = buy_close
            gap = t.get('gap_open_pct') or 0
            next_open = sig_close * (1 + gap / 100.0)
            next_close = t.get('buy_close')
        out.append({
            'code': str(t.get('code', '')).strip().zfill(6),
            'signal_date': t.get('signal_date'),
            'buy_date': t.get('buy_date'),
            'score': float(t.get('score') or 0),
            'buy_px': float(buy_px),
            'signal_close': float(sig_close),
            'next_open': float(next_open),
            'next_close': float(next_close) if next_close else None,
        })
    return out


def _fetch_week_closes(codes, start, end):
    """逐代码拉一次历史 K 线 (不复权), 返回 {code: {YYYYMMDD: close}}。

    5 日收盘只依赖历史 K 线, 网络失败时该代码自动排除 (week n 变小)。
    数据源: 东方财富 stock_zh_a_hist 优先, 失败降级腾讯 stock_zh_a_hist_tx
    (服务器上东财接口常被封, 腾讯正常 — 见 backtest/t1_real_backtest.py)。
    """
    import socket
    socket.setdefaulttimeout(15)
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, as_completed

    s = (datetime.strptime(start, '%Y%m%d') - timedelta(days=15)).strftime('%Y%m%d')
    e = (datetime.strptime(end, '%Y%m%d') + timedelta(days=20)).strftime('%Y%m%d')
    result = {}

    def _fetch(code):
        prefix = 'sh' if code.startswith('6') else 'sz'
        s_iso = f'{s[:4]}-{s[4:6]}-{s[6:8]}'
        e_iso = f'{e[:4]}-{e[4:6]}-{e[6:8]}'
        try:
            h = ak.stock_zh_a_hist(symbol=code, period='daily',
                                   start_date=s, end_date=e, adjust='')
            if h is None or h.empty:
                raise ValueError('empty')
            dates = [str(d).replace('-', '') for d in h['日期']]
            closes = [float(v) for v in h['收盘']]
            return code, dict(zip(dates, closes))
        except Exception:
            pass
        # 腾讯降级 (东财被封锁的服务器环境)
        try:
            h = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}',
                                      start_date=s_iso, end_date=e_iso)
            if h is None or h.empty:
                return code, None
            dates = [str(d).replace('-', '') for d in h['date']]
            closes = [float(v) for v in h['close']]
            return code, dict(zip(dates, closes))
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch, c): c for c in set(codes)}
        for f in as_completed(futs):
            c, m = f.result()
            if m:
                result[c] = m
    return result


def _band_of(score):
    for lo, hi in BAND_EDGES:
        if lo <= score < hi:
            return f'{lo}-{hi}'
    return '90-100'


def _build_bands(trades, week_map):
    """从 trades 构建分档概率 + 总体概率。"""
    if not trades:
        return {'overall': None, 'bands': []}

    def _week_close_at(code, signal_date):
        """信号日往后第 5 个交易日的收盘价 (≈未来一周); 超出历史范围返回 None。"""
        closes = week_map.get(code)
        if not closes:
            return None
        dates = sorted(closes.keys())
        try:
            i = dates.index(signal_date)
        except ValueError:
            return None
        if i + 5 < len(dates):
            return closes[dates[i + 5]]
        return None

    def _metrics(rows):
        n = len(rows)
        if n == 0:
            return None
        up1 = sum(1 for r in rows if r['next_close'] and r['next_close'] > r['buy_px'])
        lo = [r for r in rows if r['next_open'] < r['signal_close']]
        lo_hi = sum(1 for r in lo if r['next_close'] and r['next_close'] > r['next_open'])
        wk = [(r, _week_close_at(r['code'], r['signal_date'])) for r in rows]
        wk = [(r, c) for r, c in wk if c is not None]
        wk_up = sum(1 for r, c in wk if c > r['buy_px'])
        return {
            'n': n,
            'next_day_up': round(up1 / n * 100, 1),
            'low_open_rate': round(len(lo) / n * 100, 1),
            'low_open_high_walk': round(lo_hi / len(lo) * 100, 1) if lo else None,
            'low_open_n': len(lo),
            'week_up': round(wk_up / len(wk) * 100, 1) if wk else None,
            'week_n': len(wk),
        }

    overall = _metrics(trades)
    bands = []
    for lo, hi in BAND_EDGES:
        rows = [r for r in trades if lo <= r['score'] < hi]
        if not rows:
            continue
        m = _metrics(rows)
        # 样本不足 → 关键概率回退到总体 (保留分档 n 供展示)
        if m['n'] < BAND_MIN_N and overall:
            for k in ('next_day_up', 'low_open_high_walk', 'week_up'):
                m[k] = overall[k]
        m['band'] = f'{lo}-{hi}'
        bands.append(m)
    return {'overall': overall, 'bands': bands}


def build_probabilities(tab=None, force=False):
    """构建并缓存一个/全部榜的概率表。返回 {tab: data}。"""
    tabs = [tab] if tab else ['limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal']
    start, end = _window(30)
    out = {}
    for t in tabs:
        key = _cache_key(t)
        if not force and daily_get(key):
            out[t] = daily_get(key)
            continue
        trades = _collect_trades(t, start, end)
        week_map = _fetch_week_closes([r['code'] for r in trades], start, end)
        data = {
            'tab': t, 'window': [start, end], 'n_trades': len(trades),
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            **_build_bands(trades, week_map),
        }
        # force=True 时强制覆盖 (daily_set 默认存在即跳过)
        daily_set(key, data, force=force)
        out[t] = data
        print(f'  [proba] {t}: {len(trades)} 笔, '
              f'次日上涨 {data["overall"]["next_day_up"] if data["overall"] else "-"}%, '
              f'5日 n={data["overall"]["week_n"] if data["overall"] else 0}', file=sys.stderr)
    return out


def get_probabilities(tab):
    """API 入口: 有缓存直接返回; 否则触发后台构建并返回 building 状态。"""
    cached = daily_get(_cache_key(tab))
    if cached:
        return cached
    if tab not in _BUILDING:
        with _BUILD_LOCK:
            if tab not in _BUILDING:
                _BUILDING.add(tab)
                threading.Thread(target=_build_async, args=(tab,), daemon=True).start()
    return {'status': 'building', 'tab': tab,
            'message': '概率表生成中(需回测+历史数据), 请稍后刷新'}


def _build_async(tab):
    try:
        build_probabilities(tab, force=True)
    except Exception as e:
        print(f'  [proba] {tab} 构建失败: {e}', file=sys.stderr)
    finally:
        _BUILDING.discard(tab)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--build', nargs='?', const='', default=None,
                    help='构建指定榜(默认全部), 如 --build limit-up')
    args = ap.parse_args()
    if args.build is not None:
        tab = args.build or None
        res = build_probabilities(tab, force=True)
        print(f'构建完成: {list(res.keys())}')
    else:
        print('用法: python -m scoring.probability_estimator --build [tab]')
