"""OHLCV 获取 (2026-08-01 自 backtest_engine.py 拆分)。

职责: 按日批量/逐股获取真实历史 OHLCV; archive.db 构造兜底 (标记 _fallback,
严格模式由主循环跳过); spot 快照只允许当日使用 (历史日期一律走逐股历史 API)。
"""
import os
import sys
import pandas as pd
import akshare as ak
from datetime import datetime, timedelta

from cache import get as _cache_get, put as _cache_put
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

_SPOT_DISABLED = True
_ARCHIVE_DB_PATH = os.path.join(_PROJECT_ROOT, 'archive.db')
_ARCHIVE_OHLCV_CACHE = {}  # 进程内 LRU 简化版
def _try_archive_db_ohlcv(code: str, d_signal: str, stock_type: str = 'limit_up') -> dict:
    """从 archive.db daily_stocks 构造简化 OHLCV

    Args:
        code: 股票代码
        d_signal: 信号日 (YYYYMMDD)
        stock_type: 池类型 ('limit_up' | 'zhaban' | 'dtqiaoban' | ...)

    智能匹配策略:
        - limit_up: 直接查 d_signal 当天
        - zhaban/dtqiaoban: 查 d_signal-1 (前一天涨停 → next_day_change 即 D 那天真实涨幅)
        - reversal/trend: 查 d_signal 前 1~3 天内任何 stock_type, 优先 limit_up
    """
    cache_key = (code, d_signal, stock_type)
    if cache_key in _ARCHIVE_OHLCV_CACHE:
        return _ARCHIVE_OHLCV_CACHE[cache_key]

    try:
        import sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(_ARCHIVE_DB_PATH, timeout=2)
        cur = conn.cursor()

        if stock_type == 'limit_up':
            cur.execute(
                "SELECT price, change_pct, next_day_change, turnover "
                "FROM daily_stocks WHERE code=? AND trade_date=? AND stock_type='limit_up'",
                (code, d_signal)
            )
        elif stock_type in ('zhaban', 'dtqiaoban'):
            # v3.3i: 炸板日=D日本身就是涨停日(然后炸板), 查D/D-1/D-2三天
            # limit_up 记录的 next_day_change 可用于推算 T+1 涨幅
            dt = datetime.strptime(d_signal, '%Y%m%d')
            d_same = dt.strftime('%Y%m%d')
            d_prev = (dt - timedelta(days=1)).strftime('%Y%m%d')
            d_prev2 = (dt - timedelta(days=2)).strftime('%Y%m%d')
            cur.execute(
                "SELECT price, change_pct, next_day_change, turnover "
                "FROM daily_stocks "
                "WHERE code=? AND trade_date IN (?, ?, ?) AND stock_type='limit_up' "
                "ORDER BY ABS(julianday(trade_date) - julianday(?)) "
                "LIMIT 1",
                (code, d_same, d_prev, d_prev2, d_signal)
            )
        else:
            # reversal/trend: 向前 3 天内查任何 stock_type, 优先 limit_up
            dt = datetime.strptime(d_signal, '%Y%m%d')
            date_window = [(dt - timedelta(days=i)).strftime('%Y%m%d') for i in range(1, 4)]
            placeholders = ','.join('?' * len(date_window))
            cur.execute(
                f"SELECT price, change_pct, next_day_change, turnover, stock_type, trade_date "
                f"FROM daily_stocks WHERE code=? AND trade_date IN ({placeholders}) "
                f"ORDER BY CASE stock_type WHEN 'limit_up' THEN 0 ELSE 1 END, "
                f"ABS(julianday(trade_date) - julianday(?)) "
                f"LIMIT 1",
                [code] + date_window + [d_signal]
            )
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None

    if row is None or row[0] is None or row[0] == 0:
        # Tier1.D: daily_stocks 找不到 → 试 stock_daily (21 只重点股的 chg_pct)
        sd_ohlcv = _try_stock_daily_ohlcv(code, d_signal)
        if sd_ohlcv is not None:
            _ARCHIVE_OHLCV_CACHE[cache_key] = sd_ohlcv
            return sd_ohlcv
        _ARCHIVE_OHLCV_CACHE[cache_key] = None
        return None
    price, chg, next_d_chg, turn = row[:4]
    if next_d_chg is None:
        # Tier1.D: daily_stocks 有但 next_day_change 空 → 试 stock_daily 推 T+1
        sd_ohlcv = _try_stock_daily_ohlcv(code, d_signal)
        if sd_ohlcv is not None:
            _ARCHIVE_OHLCV_CACHE[cache_key] = sd_ohlcv
            return sd_ohlcv
        _ARCHIVE_OHLCV_CACHE[cache_key] = None
        return None

    # 构造粗略 OHLCV
    # D 收盘 = 昨收 * (1 + chg/100) = price * (1 + chg/100)
    base = float(price)
    d_close = base * (1 + (chg or 0) / 100)
    d1_close = d_close * (1 + float(next_d_chg) / 100)
    # D+1 开盘 = D 收盘 (无跳空, 一字板由 _is_limit_open 单独检测)
    buy_open = d_close
    buy_close = d1_close
    # D+2 开盘 = D+1 收盘 (T+2 数据缺失, 退化)
    sell_open = d1_close
    # 高低用 1% 振幅模拟
    buy_high = max(buy_open, buy_close) * 1.005
    buy_low = min(buy_open, buy_close) * 0.995
    sell_high = sell_open * 1.005
    sell_low = sell_open * 0.995

    result = {
        'open': round(buy_open, 2),
        'close': round(buy_close, 2),
        'high': round(buy_high, 2),
        'low': round(buy_low, 2),
        'volume': 0,
        'amount': 0,
        'turnover': float(turn or 0),
        'change_pct': float(chg or 0),
        'prev_close': base,
        # 标记是 fallback, 后续可识别
        '_fallback': 'archive_db',
    }
    _ARCHIVE_OHLCV_CACHE[cache_key] = result
    return result
def _try_stock_daily_ohlcv(code: str, d_signal: str) -> dict:
    """Tier1.D: 从 stock_daily 表取 T+1/T+2 真实 chg_pct 构造 OHLCV

    stock_daily 只覆盖 21 只重点关注股 (5/21~6/11), 是 score_stock_history 攒的
    历史表现数据。包含 T+1 / T+2 / T+3 ... 的真实日涨跌幅。

    相比 daily_stocks.next_day_change 的优势: 给定信号日 D, 可同时拿到 D+1 和 D+2 的 chg_pct,
    拼出真正的 T+1 开盘买 T+2 开盘卖 的 return。
    """
    try:
        import sqlite3
        from datetime import datetime, timedelta
        dt = datetime.strptime(d_signal, '%Y%m%d')
        d1 = (dt + timedelta(days=1)).strftime('%Y%m%d')
        d2 = (dt + timedelta(days=2)).strftime('%Y%m%d')
        conn = sqlite3.connect(_ARCHIVE_DB_PATH, timeout=2)
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, chg_pct FROM stock_daily "
            "WHERE code=? AND trade_date IN (?, ?, ?)",
            (code, d_signal, d1, d2)
        )
        rows = dict(cur.fetchall())
        conn.close()
    except Exception:
        return None

    if not rows:
        return None
    # 缺 T+1 (D+1) 的 chg_pct 就没法算 T+1 收益
    if d1 not in rows or rows[d1] is None:
        return None

    # 构造: 假设信号日 D 收盘 100 (归一化), 然后按 chg_pct 推
    # 实际: 缺 D 收盘价, 用归一化 (后续 pnl 计算会按比例还原)
    # 简化: 用 100 当 D 收盘基准
    base = 100.0
    d_close = base
    d1_close = base * (1 + float(rows[d1]) / 100)
    d2_open = d1_close  # T+1 收 → T+2 开 假设无跳空
    if d2 in rows and rows[d2] is not None:
        d2_close = d1_close * (1 + float(rows[d2]) / 100)
    else:
        d2_close = d1_close  # T+2 数据缺失, 退化

    result = {
        'open': round(base, 2),       # buy_open (D+1 开盘 ≈ D 收盘)
        'close': round(d1_close, 2),  # buy_close (D+1 收盘)
        'high': round(d1_close * 1.005, 2),
        'low': round(d1_close * 0.995, 2),
        'volume': 0,
        'amount': 0,
        'turnover': 0,
        'change_pct': 0,
        'prev_close': base,
        # 给 T+2 也填好让 sell_ohlcv 有数据
        '_sell_open': round(d2_open, 2),
        '_sell_close': round(d2_close, 2),
        # 标记是 fallback
        '_fallback': 'stock_daily',
        '_normalized': True,  # 价格是归一化 100 起的, 实际买入金额按比例折算
    }
    return result
def _get_daily_ohlcv_batch(date_str: str) -> dict:
    """P1.2: 按日期批量拉取当日所有股票的 OHLCV

    返回: {code: {open, close, high, low, volume, amount, turnover, change_pct}}
    注: akshare stock_zh_a_spot_em 是当日实时快照,收盘后数据稳定
    注2: stock_zh_a_spot_em 不支持历史 date → 历史回测仍需逐股调用

    P1.2.1 修复: 服务器上东方财富 spot_em 反复 Connection aborted
    (akshare urllib3 默认重试 3 次,每次连接断开耗 30+ 秒)
    → 改用**进程级开关** _SPOT_DISABLED 控制是否启用批量缓存。
    默认禁用,确保服务器跑得通;本地高性能环境可手动开启。

    P1.24.3 修复: 当 date_str == today, stock_zh_a_spot_em 返回的"今开"实际是当日 9:30 开盘价,
    用作"次日开盘"是错的 (跨日数据错位), 直接返回空 → 降级到 _get_ohlcv_batch 逐股 (用历史 API)
    """
    global _SPOT_DISABLED
    if _SPOT_DISABLED:
        return {}  # 主循环会降级到 _get_ohlcv_batch 逐股

    # 正确性修复 (2026-08-01): 之前逻辑反了 —— 拒绝 today、却把"今日快照"
    # 当作任意历史日期的 OHLCV 缓存, 导致历史回测买卖价全部变成今天的数据
    # (系统性失真: 不同信号日的 buy/sell 价格完全相同)。
    # 现在: 只有 date_str == 今天 才可能返回快照; 历史日期一律返回空,
    # 主循环自动降级到逐股历史 API (t1._get_ohlcv_batch, 腾讯/东财真实历史)。
    # 注意: 回测的 d_buy/d_sell 均为未来日期, 今天也几乎不会被使用。
    from datetime import datetime as _dt
    if date_str != _dt.now().strftime('%Y%m%d'):
        return {}

    cache_key = f"daily_ohlcv_all_{date_str}"
    cached = _cache_get(cache_key)
    if cached is not None and not isinstance(cached, str):
        return cached

    result = {}
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        print(f"  [OHLCV 批量] {date_str} spot_em 失败(降级逐股): {type(e).__name__}", file=sys.stderr)
        # 一次失败就禁用,避免后续重复触发 urllib3 重试
        _SPOT_DISABLED = True
        _cache_put(cache_key, '__NONE__')
        return {}

    if df is None or df.empty:
        _cache_put(cache_key, '__NONE__')
        return {}

    # 列名识别 (spot_em 列名固定)
    # 常见列: 代码/名称/最新价/涨跌幅/涨跌额/成交量/成交额/振幅/最高/最低/今开/昨收/换手率
    col_map = {
        'code': '代码', 'name': '名称',
        'open': '今开', 'high': '最高', 'low': '最低',
        'close': '最新价', 'prev_close': '昨收',
        'volume': '成交量', 'amount': '成交额',
        'turnover': '换手率', 'change_pct': '涨跌幅',
    }
    for _, row in df.iterrows():
        try:
            code = str(row.get(col_map['code'], '')).strip().zfill(6)
            if not code or len(code) != 6:
                continue
            prev_close = float(row.get(col_map['prev_close'], 0)) if pd.notna(row.get(col_map['prev_close'], 0)) else 0
            result[code] = {
                'open': float(row.get(col_map['open'], 0)) if pd.notna(row.get(col_map['open'], 0)) else 0,
                'close': float(row.get(col_map['close'], 0)) if pd.notna(row.get(col_map['close'], 0)) else 0,
                'high': float(row.get(col_map['high'], 0)) if pd.notna(row.get(col_map['high'], 0)) else 0,
                'low': float(row.get(col_map['low'], 0)) if pd.notna(row.get(col_map['low'], 0)) else 0,
                'volume': int(row.get(col_map['volume'], 0)) if pd.notna(row.get(col_map['volume'], 0)) else 0,
                'amount': float(row.get(col_map['amount'], 0)) if pd.notna(row.get(col_map['amount'], 0)) else 0,
                'turnover': float(row.get(col_map['turnover'], 0)) if pd.notna(row.get(col_map['turnover'], 0)) else 0,
                'change_pct': float(row.get(col_map['change_pct'], 0)) if pd.notna(row.get(col_map['change_pct'], 0)) else 0,
                'prev_close': prev_close,
            }
        except Exception:
            continue

    _cache_put(cache_key, result)
    return result
