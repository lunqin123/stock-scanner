"""多 Tab 回测引擎 (P1.1 骨架)

提供统一的批量回测入口 run_tab_backtest(tab, ...),按 tab 维度调度:
1. 信号池获取 (signal_pool_fetcher)
2. 评分函数 (score_func)  -返回带 score 列的 DataFrame
3. 策略模拟 (开盘买) -复用 t1_real_backtest 的 OHLCV / 聚合
4. 缓存 + 持久化

向后兼容: run_tab_backtest('limit-up', ...) 等价于 t1_real_backtest.run_t1_backtest(...)

设计原则:
- 复用优于重写: OHLCV 拉取 / _is_limit_open / _aggregate 直接 import t1_real_backtest
- 派发表优于 if-else: SIGNAL_POOL_FETCHERS / SCORE_FUNCS 是两个 dict
- 缓存键含 tab: bt_result_{tab}_{start}_{end}_{top_n}_{capital}
"""
import sys, time, os
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime, timedelta

from cache import (
    _is_trading_day,
    get as _cache_get, put as _cache_put,
    persistent_get as _persistent_get, persistent_put as _persistent_put,
    make_key,
)
# 兼容旧 alias (代码里有些地方用 _daily_get/_daily_set 命名空间访问)
_daily_get = _persistent_get
_daily_set = _persistent_put
from config import COMMISSION_ROUNDTRIP_PCT as _COMMISSION_PCT, SLIPPAGE_PCT as _SLIPPAGE_PCT, MAX_PRICE, MAX_MARKET_CAP

# 复用 t1 的工具函数
from t1_real_backtest import (
    _get_ohlcv_batch, _is_limit_open, _next_trading_date,
    CAPITAL_DEFAULT, TOP_N_DEFAULT, MAX_WORKERS,
)

from scanner import (
    filter_non_main_board, filter_xr_xd_dr,
    score_zhaban_data, score_dtqiaoban_data,
    _score_reversal as scanner_score_reversal,
    _score_trend as scanner_score_trend,
    _score_sector as scanner_score_sector,
)
from data_manager import save_backtest_result as _save_backtest_result

# 本地归档 fallback (P1.3: 回测引擎从本地 pickle 读取历史池数据)
try:
    from archiver import _load_pool_pickle
except ImportError:
    _load_pool_pickle = None  # archiver 未安装时降级

# 是否启用本地归档 fallback (默认启用, 服务器无 akshare 历史数据时从本地读)
_LOCAL_FALLBACK_ENABLED = True

# ═══════════════════════════════════════════
#  Tab 常量
# ═══════════════════════════════════════════

TAB_LIMIT_UP = 'limit-up'
TAB_TREND = 'trend'
TAB_ZHABAN = 'zhaban'
TAB_DTQIAOBAN = 'dtqiaoban'
TAB_REVERSAL = 'reversal'
TAB_SECTOR = 'sector'

ALL_TABS = [TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR]

# ─── 各 tab 默认 best preset (用于 strategy='auto') ────────────────
# 2026-07-05: 用户确认 limit-prime / trend-elite / limit-sweet 等 preset
# 始终不如 plan_a 评分本身 (IC-driven 优化后的 plan_a 评分排序已经
# 包含了合理的过滤逻辑, 无需再用外部 preset 套娃). 删除 _AUTO_PRESETS
# 和 strategy 参数完全无效, run_tab_backtest 永远用 plan_a 评分.
TAB_NAMES_CN = {
    TAB_LIMIT_UP: '涨停扫描',
    TAB_TREND: '趋势扫描',
    TAB_ZHABAN: '炸板反包',
    TAB_DTQIAOBAN: '跌停翘板',
    TAB_REVERSAL: '涨停回调',
    TAB_SECTOR: '板块联动',
}

# 各 tab 的实现状态 (P1.1 之后逐步点亮)
_PENDING_TABS = set()  # 全部实现
# 已实现: limit-up / zhaban / dtqiaoban / reversal / trend / sector (P1.1 + P2.1 + P2.2 + P2.3)

# 这些 tab 的 score_fn 自行拉数据 (不依赖 fetcher 返回的 pool)
_SELF_FETCHING_TABS = {TAB_SECTOR}

# P1.2.1: OHLCV 批量缓存进程级开关 (默认禁用,服务器环境东方财富 spot 接口不稳定)
# 本地高性能环境可在导入后手动设 backtest_engine._SPOT_DISABLED = False 启用
_SPOT_DISABLED = True

# P1.3: 自动检测本地归档可用天数 (不再硬编码 7)
# 每个 tab 的 pool_type 对应 archive_pools/ 中的 pickle 文件名前缀
_TAB_POOL_TYPE = {
    TAB_LIMIT_UP: 'limit_up',
    TAB_REVERSAL: 'prev_pool',
    TAB_TREND: 'strong',
    TAB_ZHABAN: 'zhaban',
    TAB_DTQIAOBAN: 'dtqiaoban',
    TAB_SECTOR: 'limit_up',
}


def _detect_available_days(tab: str) -> int:
    """扫描本地数据源, 返回该 tab 实际可用的历史天数。

    数据源优先级:
      1. data/cache/engine_{pool_type}_*.pkl (回测引擎池缓存, 最准确)
      2. archive_pools/{pool_type}_*.pkl (归档目录)
      3. akshare 可用窗口 fallback (10天)
    """
    import os as _os
    import re as _re
    pool_type = _TAB_POOL_TYPE.get(tab, 'limit_up')

    # 1. 统计 data/cache/engine_{pool_type}_*.pkl 的不同日期数
    try:
        cache_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'data', 'cache')
        engine_prefix = f'engine_{pool_type}_'
        dates = set()
        if _os.path.exists(cache_dir):
            for f in _os.listdir(cache_dir):
                if f.startswith(engine_prefix) and f.endswith('.pkl') and not f.startswith('persistent_'):
                    m = _re.search(r'(\d{8})', f)
                    if m:
                        dates.add(m.group(1))
        if dates:
            return max(10, min(len(dates), 120))
    except Exception:
        pass

    # 2. fallback: archive_pools 目录
    try:
        from archiver import _ARCHIVE_POOL_DIR
        if not _os.path.exists(_ARCHIVE_POOL_DIR):
            return 10
        prefix = f'{pool_type}_'
        count = sum(1 for f in _os.listdir(_ARCHIVE_POOL_DIR)
                    if f.startswith(prefix) and f.endswith('.pkl'))
        return max(10, min(count, 120))
    except Exception:
        return 10
# P1.2.1.2: akshare 各池 API 实际可用窗口: stock_zt_pool_previous_em (~7天),
#             stock_zt_pool_zbgc_em/dtgc_em (~7-10天)
# P8: fallback 从 5→10, 让有数据的天数充分参与回测。
#     超出窗口的日期 akshare 返回空 → 自然 skip, 不影响结果质量。

# ═══════════════════════════════════════════
#  信号池获取函数 (P1.1 骨架: limit-up 和 reversal 可用,其他 TBD 待 P2)
#  P1.3: 所有 fetcher 在 akshare 返回空时 fallback 读本地 pickle 归档
# ═══════════════════════════════════════════

def _try_local_fallback(date_str: str, pool_type: str, cache_key: str) -> pd.DataFrame:
    """尝试从本地 pickle 归档加载池数据

    当 akshare API 返回空(窗口限制/网络错误)时调用此函数,
    从 archiver 每日保存的本地 pickle 中读取历史数据。

    Args:
        date_str: YYYYMMDD 信号日期
        pool_type: 'prev_pool' | 'zhaban' | 'dtqiaoban' | 'strong'
        cache_key: 缓存键,命中后写入**持久**缓存 (池数据历史不变)
    Returns:
        DataFrame or None
    """
    if not _LOCAL_FALLBACK_ENABLED or _load_pool_pickle is None:
        return None
    try:
        df = _load_pool_pickle(date_str, pool_type)
        if df is not None:
            # 数据质量检查：跳过占位值数据（如封板资金全为零的劣质归档）
            if _is_placeholder_data(df, pool_type):
                return None
            _persistent_put(cache_key, df)  # 持久化 (历史不变, 不该 2h 失效)
            return df
    except Exception:
        pass
    return None


def _is_placeholder_data(df, pool_type: str) -> bool:
    """检查 DataFrame 是否是占位值数据（而非真实的 akshare 原始数据）"""
    if df is None or df.empty:
        return True
    # 检查封板资金：如果存在且全部为 0，说明是占位数据
    for col in ['封板资金', '封单资金', '封单金额']:
        if col in df.columns:
            try:
                vals = df[col].fillna(0).astype(float)
                if vals.max() == 0 and vals.min() == 0:
                    return True  # 全部为零 → 占位数据
            except (ValueError, TypeError):
                pass
            break  # 找到一个列就够
    # 检查名称列：如果名称==代码，说明是占位数据
    if '名称' in df.columns and '代码' in df.columns:
        try:
            match = (df['名称'].astype(str) == df['代码'].astype(str)).sum()
            if match > len(df) * 0.5:  # 超过50%的名称和代码相同
                return True
        except (ValueError, TypeError):
            pass
    return False


# Tier1.C: archive.db 兜底 OHLCV
# 当 akshare 历史 OHLCV 拉不到时, 用 archive.db daily_stocks + stock_daily 构造简化 OHLCV。
#
# 数据源:
#   - daily_stocks.price:  D 日前一日(昨收) → D close = price * (1 + change_pct/100)
#   - daily_stocks.change_pct:  D 日涨跌幅 (用来推 D 收盘)
#   - daily_stocks.next_day_change:  D+1 日涨跌幅 (D+1 close - D close) / D close
#   - stock_daily.chg_pct:  T+1 (D+1) 的真实涨跌幅 (与 next_day_change 含义类似,
#                         但 next_day_change 已经填了, stock_daily 可用作交叉验证)
#   - stock_daily D+2 (再下一天) 的 chg_pct 可推 T+2 开盘卖的 return
#
# 构造:
#   signal_close (D close)  = price * (1 + change_pct/100)
#   buy_open    (D+1 开盘)  = D close  (假设无跳空, A 股一字板会触发 _is_limit_open)
#   buy_close   (D+1 收盘)  = D close * (1 + next_day_change/100)
#   sell_open   (D+2 开盘)  = buy_close (T+2 数据缺失, 退化)
# 这样 T+1 开盘买的 return = (sell_open / buy_open - 1)
#                       ≈ (D+1 close / D close - 1) = next_day_change (粗略)
# 偏差: 忽略 D+1 跳空和 D+2 走势, 实测偏差 ~1-3%, 但比 skip 强
_ARCHIVE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'archive.db')
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
            # 炸板/跌停股: 前一天是涨停 (D-1), limit_up 记录的 next_day_change = D 日真实涨幅
            dt = datetime.strptime(d_signal, '%Y%m%d')
            d_prev = (dt - timedelta(days=1)).strftime('%Y%m%d')
            d_prev2 = (dt - timedelta(days=2)).strftime('%Y%m%d')
            cur.execute(
                "SELECT price, change_pct, next_day_change, turnover "
                "FROM daily_stocks "
                "WHERE code=? AND trade_date IN (?, ?) AND stock_type='limit_up' "
                "ORDER BY ABS(julianday(trade_date) - julianday(?)) "
                "LIMIT 1",
                (code, d_prev, d_prev2, d_signal)
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

def _fetch_limit_up_pool(date_str: str) -> pd.DataFrame:
    """涨停池: 当日涨停 (stock_zt_pool_em, 含封板时间/封板资金等 plan_a 所需列)

    P3.1: 改用 stock_zt_pool_em 替代 stock_zt_pool_previous_em,
    这样 plan_a 9因子评分能拿到封板/资金等完整数据, 与前端排名一致。
    """
    key = f"engine_limit_up_{date_str}"
    cached = _cached_pool_get(key)
    if cached is not None:
        return cached
    try:
        df = ak.stock_zt_pool_em(date=date_str)
    except Exception:
        df = None
    if (df is None or df.empty) and _LOCAL_FALLBACK_ENABLED:
        df = _try_local_fallback(date_str, 'limit_up', key)
    if df is not None and hasattr(df, 'empty') and not df.empty:
        _pool_cache_put(key, df)
        return df
    _pool_cache_put(key, '__NONE__')
    return None


def _fetch_reversal_pool(date_str: str) -> pd.DataFrame:
    """反转池: 上交易日涨停今日下跌 (P3.2: 使用 prev_pool, 含涨跌幅列)"""
    key = f"engine_reversal_{date_str}"
    cached = _cached_pool_get(key)
    if cached is not None:
        return cached
    df = ak.stock_zt_pool_previous_em(date=date_str)
    if (df is None or df.empty) and _LOCAL_FALLBACK_ENABLED:
        df = _try_local_fallback(date_str, 'prev_pool', key)
    if df is None or not hasattr(df, 'empty') or df.empty:
        _pool_cache_put(key, '__NONE__')
        return None
    # 列识别
    chg_col = None
    for c in df.columns:
        if '涨跌幅' in str(c):
            chg_col = c
            break
    chg_col = chg_col or df.columns[3]
    df = filter_xr_xd_dr(df)
    df = filter_non_main_board(df)
    df['_chg'] = df[chg_col].astype(float)
    pullback = df[(df['_chg'] >= -7) & (df['_chg'] <= 1)].copy()
    if not pullback.empty:
        _pool_cache_put(key, pullback)
    else:
        _pool_cache_put(key, '__NONE__')
    return pullback


def _cached_pool_get(key: str):
    """池缓存读取: 先查持久(历史不变), 再查 2h(防今天重复拉)

    历史池数据 (limit-up / zhaban / dtqiaoban / reversal / trend) 一旦拉下来
    就不再变, 用 persistent 缓存避免 2h 失效导致回测引擎反复重拉 akshare
    (akshare 7天窗口限制 → 静默返回空 → 信号池空 → 跳过全部交易)。
    """
    cached = _persistent_get(key)
    if cached is not None:
        if isinstance(cached, str) and cached == '__NONE__':
            return None
        if hasattr(cached, 'empty'):
            try:
                if cached.empty:
                    return None
            except ValueError:
                pass
        return cached
    # Fallback: 2h 缓存 (兼容老数据)
    cached = _cache_get(key)
    if cached is None:
        return None
    if isinstance(cached, str) and cached == '__NONE__':
        return None
    if hasattr(cached, 'empty'):
        try:
            if cached.empty:
                return None
        except ValueError:
            pass
        # 把 2h 命中升级为持久 (下次免查)
        try:
            _persistent_put(key, cached)
        except Exception:
            pass
        return cached
    return None


def _pool_cache_put(key: str, value):
    """池缓存写入: 用持久缓存, 历史不变, 不该 2h 失效

    value 可能是 DataFrame 或 '__NONE__' 标记
    """
    _persistent_put(key, value)


def _fetch_zhaban_pool(date_str: str) -> pd.DataFrame:
    """炸板池: 当日炸板 (P1.3: akshare 不可用时 fallback 本地 pickle)"""
    key = f"engine_zhaban_{date_str}"
    cached = _cached_pool_get(key)
    if cached is not None:
        return cached
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
    except Exception:
        df = None
    if (df is None or df.empty) and _LOCAL_FALLBACK_ENABLED:
        df = _try_local_fallback(date_str, 'zhaban', key)
    if df is not None and hasattr(df, 'empty') and not df.empty:
        _pool_cache_put(key, df)
        return df
    _pool_cache_put(key, '__NONE__')
    return None


def _fetch_dtqiaoban_pool(date_str: str) -> pd.DataFrame:
    """跌停/翘板池: 当日跌停 (P1.3: akshare 不可用时 fallback 本地 pickle)"""
    key = f"engine_dtqiaoban_{date_str}"
    cached = _cached_pool_get(key)
    if cached is not None:
        return cached
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date_str)
    except Exception:
        df = None
    if (df is None or df.empty) and _LOCAL_FALLBACK_ENABLED:
        df = _try_local_fallback(date_str, 'dtqiaoban', key)
    if df is not None and hasattr(df, 'empty') and not df.empty:
        _pool_cache_put(key, df)
        return df
    _pool_cache_put(key, '__NONE__')
    return None


def _fetch_trend_pool(date_str: str) -> pd.DataFrame:
    """强势/趋势池: 当日强势股 (P1.3: akshare 不可用时 fallback 本地 pickle)"""
    key = f"engine_trend_{date_str}"
    cached = _cached_pool_get(key)
    if cached is not None:
        return cached
    try:
        df = ak.stock_zt_pool_strong_em(date=date_str)
    except Exception:
        df = None
    if (df is None or df.empty) and _LOCAL_FALLBACK_ENABLED:
        df = _try_local_fallback(date_str, 'strong', key)
    if df is not None and hasattr(df, 'empty') and not df.empty:
        _pool_cache_put(key, df)
        return df
    _pool_cache_put(key, '__NONE__')
    return None


def _fetch_sector_pool(date_str: str):
    """板块 tab 不需要单独的 pool (由 _score_sector 直接生成个股级 DF)

    返回 None 让 _score_sector 自己拉数据,简化流程。
    """
    return None


SIGNAL_POOL_FETCHERS = {
    TAB_LIMIT_UP: _fetch_limit_up_pool,
    TAB_REVERSAL: _fetch_reversal_pool,
    TAB_ZHABAN: _fetch_zhaban_pool,
    TAB_DTQIAOBAN: _fetch_dtqiaoban_pool,
    TAB_TREND: _fetch_trend_pool,
    TAB_SECTOR: _fetch_sector_pool,
}

# ═══════════════════════════════════════════
#  V2 因子注入 helper
# ═══════════════════════════════════════════
# 目标: 给所有 5 个 tab 的回测评分叠加 mc/pd position_factor, 复用 ACE0597
# 的 limit-up tab 接入路径, 让 trend/reversal/zhaban/dtqiaoban 也吃到
# V2 因子预测力 (数据驱动分析见: commit ace0597, n=2231 票, ρ=+0.0906 p<0.001)

_BACKTEST_USE_V2_DEFAULT = True


def _apply_v2_to_score(df: pd.DataFrame, score_col: str, today_str: str,
                       use_v2: bool = None) -> pd.DataFrame:
    """对 ``df[score_col]`` 乘上 position_factor 调整。

    factor = (0.85 + mc/50) * (0.90 + pd/50)
        mc=10 pd=10 → 1.05 × 1.10 = 1.155 (+15.5%)
        mc=0  pd=0  → 0.85 × 0.90 = 0.765 (-23.5%)
        mc=pd=5 (中性) → 0.95 × 1.00 = 0.95 (-5%, 与 baseline 校准)

    历史票 mc/pd 拿不到时降为 5.0 → factor=0.95。这是 archive.db 在 v8/v9/v10
    cache 升级后被 backfill_archive.py (commit 7b1549f) 补回的覆盖 — 现在 mc/pd
    真值命中率 ~50%, 50% 票用 5.0 默认温和调整。

    Args:
        df: 评分后 DataFrame (含 '代码'/'最新价'/'名称' 列)
        score_col: 要调整的评分列名 (如 '动量评分', '反转评分', '总分', '翘板评分')
        today_str: YYYYMMDD 格式, 内部转 YYYY-MM-DD 给 factors_v2
        use_v2: None=读 _BACKTEST_USE_V2_DEFAULT, True/False=强制
    """
    if df is None or df.empty:
        return df
    if use_v2 is None:
        use_v2 = _BACKTEST_USE_V2_DEFAULT
    if not use_v2:
        return df
    try:
        from plans.factors_v2 import compute_v2_factors as _compute_v2
        today_iso = f'{today_str[:4]}-{today_str[4:6]}-{today_str[6:8]}'
        v2 = _compute_v2(df, today_iso)
        mc = v2['momentum_consistency']
        pd_ = v2['pullback_depth']
        mc_factor = 0.85 + mc / 50.0
        pd_factor = 0.90 + pd_ / 50.0
        position = (mc_factor * pd_factor).reindex(df.index, fill_value=0.95)
        df = df.copy()
        df[score_col] = (df[score_col].astype(float) * position).round(1)
    except Exception as e:
        # V2 不可用 (历史不足等) — 静默回退到 baseline, 不阻塞回测
        print(f'  [_apply_v2_to_score] {today_str} 跳过: {type(e).__name__}: {str(e)[:80]}',
              file=__import__('sys').stderr)
    return df


# ═══════════════════════════════════════════
#  评分函数 (P1.1: limit-up / zhaban / dtqiaoban 可用)
# ════════════════════════════════════

def _score_limit_up(df: pd.DataFrame, date_str: str):
    """涨停评分: plan_a 9因子 (与前端排名一致)

    P3.1: 替代 backtest_score_prev (6因子简化版),
    直接调用 plan_a 完整评分管道, 回测胜率与实盘推荐对齐。
    fund_df/history_scores/lhb_bonus 等实时数据用降级模式(全零/默认值)。
    """
    if df is None or df.empty:
        return None

    # plan_a 内部 auto_verify_backtest 需要 YYYYMMDD 格式
    today_fmt = date_str

    # 1. 过滤 (保留全池做 scoring_base, 与 scan pipeline 对齐归一化)
    from scanner import filter_non_main_board
    scoring_base = filter_non_main_board(df.copy())
    if scoring_base.empty:
        return None

    cap_col = '流通市值' if '流通市值' in scoring_base.columns else None
    if cap_col and cap_col in scoring_base.columns:
        scoring_base = scoring_base[scoring_base[cap_col].astype(float) <= 200 * 1e8]
    price_col = '最新价' if '最新价' in scoring_base.columns else (scoring_base.columns[4] if len(scoring_base.columns) > 4 else None)
    if price_col and price_col in scoring_base.columns:
        scoring_base = scoring_base[scoring_base[price_col].astype(float) <= MAX_PRICE]
    if scoring_base.empty:
        return None

    filtered = scoring_base.copy()  # 回测等权买入, 不做本金过滤

    # 2. plan_a 因子计算 (在 scoring_base 上归一化, 与前端一致)
    from plans.plan_a import compute_factors, apply_scores
    principal = 30000

    # P3.2: 尝试加载历史归档的实时数据
    try:
        from archiver import load_scan_inputs
        archived = load_scan_inputs(date_str)
    except Exception:
        archived = None

    if archived is not None:
        fund_df = archived.get('fund_df')
        sentiment_score = archived.get('sentiment_score', 3.0)
        sentiment_level = archived.get('sentiment_level', 'neutral')
        sentiment_detail = archived.get('sentiment_detail', {})
        sentiment_ok = archived.get('sentiment_ok', True)
        # lhb_bonus/history_scores 重新对齐到当前 filtered.index
        lhb_raw = archived.get('lhb_bonus', pd.Series(0.0, index=filtered.index))
        if lhb_raw is not None and hasattr(lhb_raw, 'reindex'):
            lhb_bonus = lhb_raw.reindex(filtered.index, fill_value=0.0)
        else:
            lhb_bonus = pd.Series(0.0, index=filtered.index)
        hist_raw = archived.get('history_scores', pd.Series(2.5, index=filtered.index))
        if hist_raw is not None and hasattr(hist_raw, 'reindex'):
            history_scores = hist_raw.reindex(filtered.index, fill_value=2.5)
        else:
            history_scores = pd.Series(2.5, index=filtered.index)
    else:
        fund_df = None
        sentiment_score = 3.0
        sentiment_level = 'neutral'
        sentiment_detail = {}
        sentiment_ok = True
        lhb_bonus = pd.Series(0.0, index=filtered.index)
        history_scores = pd.Series(2.5, index=filtered.index)

    factors = compute_factors(scoring_base, fund_df=fund_df, principal=principal)

    # v2 因子 (持续性 + 回撤位置) — 回测路径此前缺失, 导致 mc/pd 一律 5.0,
    # 等价于 baseline (use_v2=False). 现补回显式注入, 与 plan_a.score() 前端路径对齐。
    from plans.factors_v2 import compute_v2_factors as _compute_v2
    v2_factors = _compute_v2(scoring_base, today_fmt)
    factors['momentum_consistency'] = v2_factors['momentum_consistency']
    factors['pullback_depth'] = v2_factors['pullback_depth']
    n_with_hist = int((v2_factors['momentum_consistency'] != 5.0).sum())
    print(f"  [PlanA v2 / backtest] {n_with_hist}/{len(scoring_base)} 票有历史, mc/pd 启用",
          file=sys.stderr)

    # 使用涨停专用权重（板块热度提权、封板强度降权）
    from weight_manager import _load_tab_weights
    limit_up_weights = _load_tab_weights('limit-up')

    # use_v2=True 已经内置在 apply_scores 路径 (factors 里已注入 mc/pd),
    # 无需再走 _apply_v2_to_score (那是给其它 tab 评分函数末位套用的 helper)
    total_scores, base_scores, danger_flags, weights = apply_scores(
        filtered, factors, sentiment_score, history_scores, lhb_bonus, today_fmt,
        weights=limit_up_weights, use_v2=_BACKTEST_USE_V2_DEFAULT)

    # 3. 附加评分列到 DataFrame
    filtered = filtered.copy()
    filtered['plan_a总分'] = total_scores.round(1)
    filtered['plan_a基础分'] = base_scores.round(1)
    # 危险信号标记
    filtered['_danger'] = ''
    for idx, flags in danger_flags.items():
        if flags and idx in filtered.index:
            filtered.loc[idx, '_danger'] = ','.join(flags)

    return filtered  # 含 'plan_a总分' 列


def _score_zhaban(df: pd.DataFrame, date_str: str):
    """炸板评分: score_zhaban_data + 可调权 (P5)

    P8 修复: 尝试加载历史存档的资金流数据，避免评分使用实时数据产生未来偏差。
    """
    if df is None or df.empty:
        return None
    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)
    if df.empty:
        return None
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    cap_col = '流通市值' if '流通市值' in df.columns else None
    if cap_col and cap_col in df.columns:
        df = df[df[cap_col].astype(float) <= MAX_MARKET_CAP * 1e8]
    df = df[df[price_col].astype(float) <= MAX_PRICE]
    if df.empty:
        return None
    try:
        from weight_manager import _load_tab_weights
        w = _load_tab_weights('zhaban')
    except Exception:
        w = None
    # 尝试加载存档资金流数据（回测时信号日的历史数据）
    fund_df = None
    try:
        from archiver import load_scan_inputs
        archived = load_scan_inputs(date_str)
        if archived is not None:
            fund_df = archived.get('fund_df')
    except Exception:
        pass
    # NOTE: V2 因子只在 limit-up tab 注入, 不在此 tab 扰动排序 (数据验证:
    # 全 tab 接入 V2 让总 PnL 净亏 -5K, 因为独立评分函数被 0.95 缩放扰乱)
    return score_zhaban_data(df, date_str, weights=w, fund_df=fund_df)


def _score_dtqiaoban(df: pd.DataFrame, date_str: str):
    """跌停翘板评分: score_dtqiaoban_data + 可调权 (P5)"""
    if df is None or df.empty:
        return None
    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)
    if df.empty:
        return None
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    cap_col = '流通市值' if '流通市值' in df.columns else None
    if cap_col and cap_col in df.columns:
        df = df[df[cap_col].astype(float) <= MAX_MARKET_CAP * 1e8]
    df = df[df[price_col].astype(float) <= MAX_PRICE]
    if df.empty:
        return None
    try:
        from weight_manager import _load_tab_weights
        w = _load_tab_weights('dtqiaoban')
    except Exception:
        w = None
    # NOTE: V2 因子不在此 tab 注入 (见 _score_zhaban 注释, 数据验证接入恶化总 PnL)
    return score_dtqiaoban_data(df, weights=w)


def _score_reversal(df: pd.DataFrame, date_str: str):
    """反转评分: scanner._score_reversal + 可调权 (P5)"""
    try:
        from weight_manager import _load_tab_weights
        w = _load_tab_weights('reversal')
    except Exception:
        w = None
    # NOTE: V2 因子不在此 tab 注入 (见 _score_zhaban 注释)
    return scanner_score_reversal(df, today_str=date_str, weights=w)


def _score_trend(df: pd.DataFrame, date_str: str):
    """趋势评分: scanner._score_trend + 可调权 (P4)

    df 来自 _fetch_trend_pool (stock_zt_pool_strong_em 当日强势池)
    _score_trend 内部已含板块/价格/市值过滤 + 评分
    """
    if df is None or df.empty:
        return None

    df = df.copy()
    code_col = '代码' if '代码' in df.columns else df.columns[1]
    df = filter_non_main_board(df, code_col=code_col)

    cap_col = '流通市值' if '流通市值' in df.columns else None
    if cap_col and cap_col in df.columns:
        df = df[df[cap_col].astype(float) <= 200 * 1e8]

    price_col = '最新价' if '最新价' in df.columns else (df.columns[4] if len(df.columns) > 4 else None)
    if price_col and price_col in df.columns:
        df = df[df[price_col].astype(float) <= MAX_PRICE]

    change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
    changes = df[change_col].astype(float)
    df = df[(changes >= 2.5) & (changes < 8.5)]

    if df.empty:
        return None

    # P4: 用可调权评分
    try:
        from weight_manager import load_trend_weights
        w = load_trend_weights()
    except Exception:
        w = None
    # NOTE: V2 因子不在此 tab 注入 (见 _score_zhaban 注释)
    return scanner_score_trend(df, weights=w)


def _score_sector(df: pd.DataFrame, date_str: str):
    """板块 tab 回测评分: P2.3 已抽到 scanner._score_sector

    df 参数被忽略 (板块 tab 自己拉数据生成个股级 DF)
    date_str 用于调用 scanner._score_sector(date_str)
    """
    try:
        return scanner_score_sector(date_str, top_n=TOP_N_DEFAULT)
    except Exception as e:
        print(f"  [sector] {date_str} 评分失败: {e}", file=sys.stderr)
        return None


SCORE_FUNCS = {
    TAB_LIMIT_UP: _score_limit_up,
    TAB_REVERSAL: _score_reversal,
    TAB_ZHABAN: _score_zhaban,
    TAB_DTQIAOBAN: _score_dtqiaoban,
    TAB_TREND: _score_trend,
    TAB_SECTOR: _score_sector,
}

# 各 tab 的评分列名
SCORE_COLUMNS = {
    TAB_LIMIT_UP: 'plan_a总分',  # P3.1: 改用 plan_a 9因子, 与前端一致
    TAB_REVERSAL: '反转评分',
    TAB_ZHABAN: '总分',         # score_zhaban_data 输出
    TAB_DTQIAOBAN: '翘板评分',
    TAB_TREND: '动量评分',
    TAB_SECTOR: '板块强度',
}

# ═══════════════════════════════════════════
#  OHLCV 批量缓存 (P1.2 性能优化)
# ═══════════════════════════════════════════

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

    # P1.24.3: 拒绝用 spot_em 处理 today (今日实时数据无"次日"语义)
    from datetime import datetime as _dt
    if date_str == _dt.now().strftime('%Y%m%d'):
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


# ═══════════════════════════════════════════
#  交易日工具
# ═══════════════════════════════════════════

def _trading_dates_in_range(start_str: str, end_str: str, max_count: int = 60):
    """[start, end] 区间所有交易日,正序"""
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


# ═══════════════════════════════════════════
#  聚合辅助 (复用于 t1 的逻辑)
# ═══════════════════════════════════════════

def _aggregate(records, label='backtest'):
    if not records:
        return None
    rets = [r['net_ret_pct'] for r in records]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    n = len(rets)
    win_n = len(wins)
    win_avg_v = float(np.mean(wins)) if wins else 0
    loss_avg_v = float(np.mean(losses)) if losses else 0
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


# ═══════════════════════════════════════════
#  主入口: run_tab_backtest
# ═══════════════════════════════════════════

def run_tab_backtest(
    tab: str,
    start_date: str = None,
    end_date: str = None,
    top_n: int = TOP_N_DEFAULT,
    min_score: float = 50.0,
    sell_n: int = 3,
    capital: float = CAPITAL_DEFAULT,
    max_days: int = 30,
    use_cache: bool = True,
    strategy: str = None,
    use_v2: bool = True,
):
    """多 tab 回测主入口 (2026-07-05 清理: 不再支持外部 strategy preset)

    唯一过滤逻辑 = plan_a 评分 + min_score 阈值 + (sell_n 卖出日).
    历史 strategy='limit-prime'/'trend-elite'/'limit-sweet' preset 已删除,
    strategy 参数保留仅作向后兼容 (被忽略, 即不应用任何额外 preset).

    Args:
        tab: TAB_LIMIT_UP / TAB_TREND / TAB_ZHABAN / TAB_DTQIAOBAN / TAB_REVERSAL / TAB_SECTOR
        start_date/end_date: YYYYMMDD, 默认最近 30 个交易日
        top_n: 每个信号日取前 N 名
        min_score: 最低评分门槛
        sell_n: 卖出日偏移 (2=T+2, 3=T+3, 4=T+4, 5=T+5)
        capital: 单笔本金
        max_days: 默认 30 天
        use_cache: True 走 daily cache
        strategy: [已忽略] 历史 preset 名, 保留兼容性. 真正过滤只靠 min_score.
        use_v2: limit-up tab 是否启用 v2 持续性+回撤位置因子

    Returns:
        dict: {summary, trades, top5, bottom5, skipped, comparison, generated_at, config}
    """
    # ── 校验 tab ──
    if tab not in ALL_TABS:
        return {
            'summary': {}, 'trades': [], 'skipped': [],
            'generated_at': datetime.now().isoformat(),
            'error': f'未知 tab: {tab}, 支持: {ALL_TABS}',
        }

    # ── 校验评分函数是否已实现 ──
    if tab in _PENDING_TABS:
        return {
            'summary': {}, 'trades': [], 'skipped': [],
            'generated_at': datetime.now().isoformat(),
            'error': f'tab={tab} 的评分函数尚未实现 (当前阶段已实现: {[t for t in ALL_TABS if t not in _PENDING_TABS]})',
        }

    # 2026-07-05: _AUTO_PRESETS / strategy_filters 已删除.
    # strategy 参数保留兼容性但实际不应用任何外部 preset.

    # ── 默认日期 ──
    # 自动检测本地归档可用天数 (超7天时无需手动改配置)
    tab_max = _detect_available_days(tab)
    if max_days > tab_max:
        max_days = tab_max

    if end_date is None:
        from cache import _trading_date as _get_td
        end = _get_td().replace('-', '')
    else:
        end = end_date

    if start_date is None:
        sd = datetime.strptime(end, '%Y%m%d') - timedelta(days=max_days * 2)
        start = sd.strftime('%Y%m%d')
    else:
        start = start_date

    # ── 整体结果缓存 ──
    # 注意: use_v2 必须进 cache_key, use_cache=False 强制重算
    if use_cache:
        cache_key = make_key("bt", "result", tab=tab,
                             start=start, end=end, top_n=top_n,
                             min_score=int(min_score), sell_n=sell_n, capital=int(capital),
                             use_v2="v2" if use_v2 else "nov2")
        cached = _daily_get(cache_key)
        if cached and 'summary' in cached:
            return cached

    trade_dates = _trading_dates_in_range(start, end, max_count=max_days)
    if not trade_dates:
        return {
            'summary': {}, 'trades': [], 'skipped': [],
            'generated_at': datetime.now().isoformat(),
            'error': '区间内无交易日',
        }

    # ── 主循环 ──
    fetcher = SIGNAL_POOL_FETCHERS[tab]
    score_fn = SCORE_FUNCS[tab]
    score_col = SCORE_COLUMNS[tab]
    # 2026-07-05: 删除策略B(尾盘买)/策略C(休盘+止损), 仅保留策略A(开盘买)
    records_open, skipped, unbuyable_count = [], [], 0

    for d_signal in trade_dates:
        d_buy = _next_trading_date(d_signal)
        if d_buy is None or d_buy > trade_dates[-1]:
            skipped.append({'signal': d_signal, 'reason': '买入日超出区间'})
            continue
        # 多时点卖出：sell_n=T+N 卖出(N 是信号日后的偏移交易日数)
        # 例: sell_n=2 → d_buy 后 1 个交易日(T+2) ; sell_n=3 → d_buy 后 2 个交易日(T+3)
        # BUG-8 修复: 之前 range(sell_n) 实际算到 T+(N+1), 导致默认 sell_n=3 的回测
        # 把边界最后几天的信号全部 "卖出日超出区间" 跳过, 用户看到"没最新交易"
        d_sell = d_buy
        for _si in range(max(0, sell_n - 1)):
            d_sell = _next_trading_date(d_sell)
            if d_sell is None:
                break
        if d_sell is None:
            skipped.append({'signal': d_signal, 'reason': '卖出日无效'})
            continue
        # 策略A需要真正的T+N卖出日, d_sell超区间则跳过(否则同日买卖无意义)
        if d_sell is None or d_sell > trade_dates[-1]:
            skipped.append({'signal': d_signal, 'reason': '卖出日超出区间'})
            continue

        try:
            pool = fetcher(d_signal)
            # 板块 tab 等特殊场景: pool 为 None, 由 score_fn 自行处理 (内部拉数据)
            if pool is None:
                if tab in _SELF_FETCHING_TABS:
                    df_scored = score_fn(None, d_signal)
                else:
                    skipped.append({'signal': d_signal, 'reason': '信号池空'})
                    continue
            else:
                try:
                    if hasattr(pool, 'empty') and pool.empty:
                        skipped.append({'signal': d_signal, 'reason': '信号池空'})
                        continue
                except ValueError:
                    pass
                df_scored = score_fn(pool, d_signal)

            if df_scored is None:
                skipped.append({'signal': d_signal, 'reason': '评分后空'})
                continue
            try:
                if hasattr(df_scored, 'empty') and df_scored.empty:
                    skipped.append({'signal': d_signal, 'reason': '评分后空'})
                    continue
            except ValueError:
                pass

            # 评分列容错查找
            actual_score_col = None
            for cand in [score_col, '回测评分', '综合分', '总分', '评分', '翘板评分', '反转评分', '动量评分', '板块强度']:
                if cand in df_scored.columns:
                    actual_score_col = cand
                    break
            if actual_score_col is None:
                skipped.append({'signal': d_signal, 'reason': f'找不到评分列 (尝试过 {score_col})'})
                continue

            # 名称列容错
            name_col = None
            for cand in ['名称', '股票名称']:
                if cand in df_scored.columns:
                    name_col = cand
                    break
            name_col = name_col or df_scored.columns[2]
            code_col = None
            for cand in ['代码']:
                if cand in df_scored.columns:
                    code_col = cand
                    break
            code_col = code_col or df_scored.columns[1]

            # 取 top_n（按评分门槛过滤）
            eligible = df_scored[df_scored[actual_score_col] >= min_score]
            skipped_count = max(0, len(df_scored) - len(eligible))
            top = eligible.sort_values(actual_score_col, ascending=False).head(top_n)

            # ── P1.2 优化: 提前批量拉取 3 个日期的全市场 OHLCV ──
            ohlcv_dates = [d_signal, d_buy, d_sell]
            daily_ohlcv = {}
            for d in ohlcv_dates:
                daily_ohlcv[d] = _get_daily_ohlcv_batch(d)

            for rank, (_, row) in enumerate(top.iterrows(), 1):
                code = str(row.get(code_col, '') or row.iloc[0]).strip().zfill(6)
                name = str(row.get(name_col, '') or row.iloc[0])
                sc = float(row.get(actual_score_col, 0))

                # 优先用批量缓存,缺失则降级逐股拉取
                signal_ohlcv = daily_ohlcv.get(d_signal, {}).get(code)
                buy_ohlcv = daily_ohlcv.get(d_buy, {}).get(code)
                sell_ohlcv = daily_ohlcv.get(d_sell, {}).get(code)
                if not all([signal_ohlcv, buy_ohlcv, sell_ohlcv]):
                    # 降级: 逐股拉取
                    ohlcv_map = _get_ohlcv_batch(code, [d_signal, d_buy, d_sell])
                    signal_ohlcv = signal_ohlcv or ohlcv_map.get(d_signal)
                    buy_ohlcv = buy_ohlcv or ohlcv_map.get(d_buy)
                    sell_ohlcv = sell_ohlcv or ohlcv_map.get(d_sell)
                if not all([signal_ohlcv, buy_ohlcv, sell_ohlcv]):
                    # Tier1.C: archive.db next_day_change fallback
                    # akshare 历史 OHLCV 7天窗口 + 2h 缓存失效 → 拉不到时,
                    # 用 archive.db daily_stocks 里已存的 next_day_change 当 T+1 粗略收益
                    # (按 tab 选 stock_type: limit-up→limit_up, zhaban→limit_up(同池), reversal→prev_pool→limit_up)
                    stock_type_for_arch = {
                        TAB_LIMIT_UP: 'limit_up',
                        TAB_ZHABAN: 'limit_up',     # 炸板前一日是涨停
                        TAB_REVERSAL: 'limit_up',   # 反转基础池也是涨停
                        TAB_DTQIAOBAN: 'limit_up',  # 跌停前一日多半是涨停
                        TAB_TREND: 'limit_up',      # 趋势池从前涨停过滤
                        TAB_SECTOR: 'limit_up',
                    }.get(tab, 'limit_up')
                    arch_ohlcv = _try_archive_db_ohlcv(code, d_signal, stock_type_for_arch)
                    if arch_ohlcv is not None:
                        if not signal_ohlcv: signal_ohlcv = arch_ohlcv
                        if not buy_ohlcv: buy_ohlcv = arch_ohlcv
                        if not sell_ohlcv: sell_ohlcv = arch_ohlcv
                if not all([signal_ohlcv, buy_ohlcv, sell_ohlcv]):
                    missing = []
                    if not signal_ohlcv: missing.append(f'signal={d_signal}')
                    if not buy_ohlcv: missing.append(f'buy={d_buy}')
                    if not sell_ohlcv: missing.append(f'sell={d_sell}')
                    skipped.append({'signal': d_signal, 'reason': f'{name}({code}) OHLCV缺失: {", ".join(missing)}'})
                    continue

                signal_close = signal_ohlcv['close']
                gap_pct = round((buy_ohlcv['open'] / signal_close - 1) * 100, 1)
                # 买入过滤: 一字板排除; 跳空高开出货陷阱
                # Tier1.D: stock_daily fallback 用归一化价格 (基准100), 跳空不可信 → 跳过该过滤
                # Plan B: gap_trap 过滤与 Plan B 的 gap 规则冲突 (Plan B 认为 gap[3,5)最赚),
                #         Plan B 启用时禁用 gap_trap, 由 should_buy_plan_b 统一判断
                is_normalized = signal_ohlcv.get('_normalized', False)
                limit_open = (not is_normalized) and _is_limit_open(buy_ohlcv, signal_close)
                _plan_b_active = True  # Plan B 启用, gap_trap 交给 should_buy_plan_b 判断
                if _plan_b_active:
                    gap_trap = False
                else:
                    _gap_trap_threshold = 8.0 if tab == TAB_ZHABAN else 5.0
                    gap_trap = (not is_normalized) and (gap_pct > _gap_trap_threshold)
                buyable = not limit_open and not gap_trap
                if not buyable:
                    unbuyable_count += 1
                    if gap_trap:
                        skipped.append({'signal': d_signal, 'reason': f'{name} 跳空{gap_pct:+.1f}%>{_gap_trap_threshold:.0f}%高开陷阱'})

                if sell_ohlcv is None:
                    missing.append(f'sell={d_sell}')
                    skipped.append({'signal': d_signal, 'reason': f'{name}({code}) OHLCV缺失: sell={d_sell}'})
                    continue

                # ── Plan B: 数据驱动硬过滤 (2026-07-05) ──
                # 基于服务器1872笔26天回测分析, 用实盘可知特征过滤
                if not is_normalized:
                    try:
                        from plans.plan_b import should_buy_plan_b
                        if not should_buy_plan_b(gap_pct, signal_close, tab):
                            skipped.append({'signal': d_signal, 'reason': f'{name} plan_b过滤: gap={gap_pct:+.1f}% price={signal_close:.1f} tab={tab}'})
                            continue
                    except ImportError:
                        pass

                intraday = {
                    'buy_high': round(buy_ohlcv['high'], 2),
                    'buy_low': round(buy_ohlcv['low'], 2),
                    'buy_close': round(buy_ohlcv['close'], 2),
                    'buy_turnover': round(buy_ohlcv['turnover'], 2),
                    'sell_high': round(sell_ohlcv.get('high', 0), 2) if sell_ohlcv else 0,
                    'sell_low': round(sell_ohlcv.get('low', 0), 2) if sell_ohlcv else 0,
                    'sell_close': round(sell_ohlcv.get('close', 0), 2) if sell_ohlcv else 0,
                    'signal_close': round(signal_close, 2),
                    'gap_open_pct': gap_pct,
                    'buyable': buyable,
                }

                sell_px = sell_ohlcv.get('_sell_open') or sell_ohlcv['open']

                # ── 策略A: 开盘买 ──
                # D+1 开盘买入, D+N 开盘卖出
                if buyable:
                    buy_px = buy_ohlcv['open']
                    raw_ret = (sell_px / buy_px - 1) * 100
                    net_ret = raw_ret - _COMMISSION_PCT - _SLIPPAGE_PCT
                    rec = {
                        'signal_date': d_signal, 'buy_date': d_buy, 'sell_date': d_sell,
                        'rank': rank, 'code': code, 'name': name, 'score': round(sc, 1),
                        'buy_price': round(buy_px, 2), 'sell_price': round(sell_px, 2),
                        'raw_ret_pct': round(raw_ret, 2), 'net_ret_pct': round(net_ret, 2),
                        'pnl': round(capital * net_ret / 100, 0), **intraday,
                    }
                    # P4: 趋势因子分列(供调权)
                    for fk in ['trend_chg','trend_turnover','trend_amount','trend_vr','trend_nh','trend_ma',
                               'rev_turnover','rev_consecutive','rev_pullback','rev_sector',
                               'zb_seal','zb_money','zb_feature','zb_turnover','zb_sector',
                               'dt_deal','dt_seal','dt_cont','dt_turnover','dt_time']:
                        val = row.get(fk)
                        if val is not None:
                            rec[fk] = round(float(val), 1)
                    records_open.append(rec)

        except Exception as e:
            skipped.append({'signal': d_signal, 'reason': f'错误: {str(e)[:80]}'})
        time.sleep(0.5)

    # ── 聚合 ──
    sum_open = _aggregate(records_open, '开盘买')

    # ── 近30天聚合 ──
    cutoff_30d = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
    records_open_30d = [r for r in records_open if r['signal_date'] >= cutoff_30d]
    sum_open_30d = _aggregate(records_open_30d, '开盘买(近30天)')

    if sum_open is None:
        empty_summary = {'trade_count': 0, 'win_rate': 0, 'avg_ret': 0,
                    'total_pnl': 0, 'plr': 0, 'max_dd': 0, 'best': 0,
                    'worst': 0, 'ev': 0, 'cumulative_ret': 0}
        result = {
            'summary': dict(empty_summary),
            'summary_30d': dict(empty_summary),
            'trades': [],
            'skipped': skipped,
            'generated_at': datetime.now().isoformat(),
            'config': {'tab': tab, 'start': start, 'end': end, 'top_n': top_n, 'sell_n': sell_n, 'capital': capital},
            'error': '无有效交易',
        }
        return result

    # ── 策略过滤器 (2026-07-05 删除: 不再用外部 preset) ──
    # 历史 limit-prime / trend-elite / limit-sweet 已删除, 唯一过滤逻辑
    # = plan_a 评分 + min_score 阈值 + sell_n 卖出日.
    # strategy 参数保留兼容但被忽略 (永远等价 None).

    sorted_trades = sorted(records_open, key=lambda x: -x['net_ret_pct']) if records_open else []
    # P1.24.3 修复: top5/bot5 在数据少时 overlap (用户报"买/卖反"实为 bot5 混入赚票)
    # 正确做法: 总数 N < 10 时, top5 取前 ceil(N/2) 条, bot5 取后 floor(N/2) 条
    # 总数 N >= 10 时, top5/bot5 各 5 条, 互斥 (因 N>=10 时重叠概率为 0)
    n_total = len(sorted_trades)
    if n_total == 0:
        top5, bot5 = [], []
    elif n_total < 10:
        top_n_cnt = (n_total + 1) // 2  # ceil(n/2): 3→2, 4→2, 5→3, ...
        bot_n = n_total - top_n_cnt
        top5 = sorted_trades[:top_n_cnt]
        bot5 = sorted_trades[-bot_n:][::-1] if bot_n > 0 else []
    else:
        top5 = sorted_trades[:5]
        bot5 = sorted_trades[-5:][::-1]

    result = {
        'summary': sum_open,
        'summary_30d': sum_open_30d,
        'trades': records_open,
        'top5': top5, 'bottom5': bot5,
        'skipped': skipped,
        'generated_at': datetime.now().isoformat(),
        'config': {
            'tab': tab, 'start': start, 'end': end,
            'top_n': top_n, 'min_score': min_score, 'sell_n': sell_n, 'capital': capital,
            'commission_pct': _COMMISSION_PCT,
            'slippage_pct': _SLIPPAGE_PCT,
            'strategy': 'T+1 真实 (信号日 → D+1 开盘买 → D+N 开盘卖)',
        },
        'comparison': {
            'open_buy': {'summary': sum_open, 'trades': records_open},
            'unbuyable_count': unbuyable_count,
        },
    }

    # 缓存
    if use_cache:
        _daily_set(cache_key, result)

    # 持久化
    try:
        _save_backtest_result(result)
    except Exception as _e:
        print(f"  [引擎持久化] 写入失败: {_e}", file=sys.stderr)

    # 保存 tab 表现 → 供 tab 仓位权重参考 (不做因子调权)
    try:
        from weight_manager import save_tab_performance
        save_tab_performance(tab, result.get('summary', {}))
    except Exception:
        pass

    # 因子级自动调权 — ⛔ 已禁用，权重已手动优化锁定
    # from scanner import get_market_status
    # if get_market_status() == 'trading':
    #     return result
    # if tab == TAB_TREND and records_open:
    #     from weight_manager import adjust_trend_weights_from_backtest
    #     new_w, msg = adjust_trend_weights_from_backtest(records_open)
    # if tab == TAB_REVERSAL and records_open:
    #     from weight_manager import adjust_reversal_weights_from_backtest
    #     new_w, msg = adjust_reversal_weights_from_backtest(records_open)
    # if tab in (TAB_ZHABAN, TAB_DTQIAOBAN) and records_open:
    #     from weight_manager import adjust_tab_weights_from_backtest
    #     new_w, msg = adjust_tab_weights_from_backtest(tab, records_open)

    return result


# ═══════════════════════════════════════════
#  向后兼容: run_t1_backtest = run_tab_backtest('limit-up', ...)
# ═══════════════════════════════════════════

def run_t1_backtest(start_date=None, end_date=None, top_n=TOP_N_DEFAULT,
                     capital=CAPITAL_DEFAULT, max_days=30, use_cache=True):
    """涨停 tab 的 T+1 回测 - 向后兼容别名"""
    return run_tab_backtest(
        tab=TAB_LIMIT_UP,
        start_date=start_date, end_date=end_date,
        top_n=top_n, capital=capital, max_days=max_days, use_cache=use_cache,
    )


# ═══════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import argparse, json
    parser = argparse.ArgumentParser(description='多 Tab 回测引擎')
    parser.add_argument('--tab', default='limit-up',
                        choices=ALL_TABS + ['all'],
                        help='回测 tab,默认 limit-up')
    parser.add_argument('--days', type=int, default=5, help='回测天数 (默认 5,aksahre 实际可用窗口限制)')
    parser.add_argument('--top', type=int, default=TOP_N_DEFAULT, help='每日 TOP N')
    parser.add_argument('--capital', type=float, default=CAPITAL_DEFAULT, help='单笔本金')
    args = parser.parse_args()

    tabs_to_run = ALL_TABS if args.tab == 'all' else [args.tab]
    for tab in tabs_to_run:
        print(f"\n{'='*70}")
        print(f"  Tab: {tab} ({TAB_NAMES_CN[tab]}) | TOP{args.top} | {args.days}天回测")
        print('='*70)
        res = run_tab_backtest(tab=tab, top_n=args.top, capital=args.capital,
                               max_days=args.days, use_cache=False)
        if 'error' in res and not res.get('trades'):
            print(f"  [跳过] {res.get('error')}")
            continue
        s = res['summary']
        print(f"  笔数: {s.get('trade_count', 0)}")
        print(f"  胜率: {s.get('win_rate', 0)}%")
        print(f"  累计收益: {s.get('cumulative_ret', 0):+.2f}%")
        print(f"  总盈亏: ¥{s.get('total_pnl', 0):+,.0f}")
        print(f"  盈亏比: {s.get('plr', 0)}")
        print(f"  最大回撤: {s.get('max_dd', 0):.2f}%")
        print(f"  期望值: {s.get('ev', 0):+.2f}%")
        print(f"  最优: {s.get('best', 0):+.2f}%  最差: {s.get('worst', 0):+.2f}%")
        cmp = res.get('comparison', {})
        print(f"  一字板跳过: {cmp.get('unbuyable_count', 0)} 笔")
        print(f"  跳过信号日: {len(res.get('skipped', []))} 个")