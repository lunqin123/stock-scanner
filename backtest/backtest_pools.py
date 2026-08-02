"""信号池拉取 + 本地归档 fallback (2026-08-01 自 backtest_engine.py 拆分)。

每个 tab 一个 fetcher: akshare 拉取 → 失败时读本地 pickle 归档 →
命中结果写入持久缓存 (历史池数据不变)。SIGNAL_POOL_FETCHERS 供主循环派发。
"""
import os
import sys
import pandas as pd
import akshare as ak

from cache import (
    persistent_get as _persistent_get,
    persistent_put as _persistent_put,
    get as _cache_get,
)
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
from backtest_tabs import (
    TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR,
)
from scanner import filter_non_main_board, filter_xr_xd_dr

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

try:
    from archiver import _load_pool_pickle
except ImportError:
    _load_pool_pickle = None  # archiver 未安装时降级

_LOCAL_FALLBACK_ENABLED = True

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
      2. archive.db daily_stocks 表 (每日存档, 数据丰富)
      3. archive_pools/{pool_type}_*.pkl (归档目录)
      4. akshare 可用窗口 fallback (10天)
    """
    import os as _os
    import re as _re
    pool_type = _TAB_POOL_TYPE.get(tab, 'limit_up')

    # 1. 统计 data/cache/engine_{pool_type}_*.pkl 的不同日期数
    try:
        cache_dir = _os.path.join(_PROJECT_ROOT, 'data', 'cache')
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

    # 2. archive.db daily_stocks 表 (用户实际存储的每日数据)
    try:
        _db_path = _os.path.join(_PROJECT_ROOT, 'archive.db')
        if _os.path.exists(_db_path):
            import sqlite3
            conn = sqlite3.connect(_db_path, timeout=2)
            cur = conn.cursor()
            # pool_type → archive.db stock_type 映射
            _stock_type_map = {
                'limit_up': 'limit_up',
                'prev_pool': 'limit_up',   # 反转也用 limit_up
                'strong': 'trend',
                'zhaban': 'zhaban',
                'dtqiaoban': 'dtqiaoban',
            }
            st = _stock_type_map.get(pool_type, pool_type)
            cur.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM daily_stocks WHERE stock_type=?",
                (st,)
            )
            row = cur.fetchone()
            conn.close()
            if row and row[0] >= 5:
                return max(10, min(row[0], 120))
    except Exception:
        pass

    # 3. fallback: 检查 archive.db 总天数 (所有类型共享的时间窗口)
    try:
        _db_path = _os.path.join(_PROJECT_ROOT, 'archive.db')
        if _os.path.exists(_db_path):
            import sqlite3
            conn = sqlite3.connect(_db_path, timeout=2)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT trade_date) FROM daily_stocks")
            row = cur.fetchone()
            conn.close()
            if row and row[0] >= 5:
                return max(10, min(row[0], 120))
    except Exception:
        pass

    # 4. 最后 fallback: akshare 实际可用窗口
    return 10
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
