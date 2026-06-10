"""多 Tab 回测引擎 (P1.1 骨架)

提供统一的批量回测入口 run_tab_backtest(tab, ...),按 tab 维度调度:
1. 信号池获取 (signal_pool_fetcher)
2. 评分函数 (score_func)  -返回带 score 列的 DataFrame
3. 双策略模拟 (开盘买 / 尾盘买) -复用 t1_real_backtest 的 OHLCV / 聚合
4. 缓存 + 持久化

向后兼容: run_tab_backtest('limit-up', ...) 等价于 t1_real_backtest.run_t1_backtest(...)

设计原则:
- 复用优于重写: OHLCV 拉取 / _is_limit_open / _aggregate 直接 import t1_real_backtest
- 派发表优于 if-else: SIGNAL_POOL_FETCHERS / SCORE_FUNCS 是两个 dict
- 缓存键含 tab: bt_result_{tab}_{start}_{end}_{top_n}_{capital}
"""
import sys, time
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime, timedelta

from cache import (
    _is_trading_day,
    get as _cache_get, put as _cache_put,
    daily_get as _daily_get, daily_set as _daily_set,
    make_key,
)
from config import COMMISSION_ROUNDTRIP_PCT as _COMMISSION_PCT, SLIPPAGE_PCT as _SLIPPAGE_PCT

# 复用 t1 的工具函数
from t1_real_backtest import (
    _get_ohlcv_batch, _is_limit_open, _next_trading_date,
    CAPITAL_DEFAULT, TOP_N_DEFAULT, MAX_WORKERS,
)

from scanner import (
    filter_non_main_board,
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
    """扫描本地归档目录, 返回该 tab 实际可用的历史天数。

    优先用本地 pickle 数量 (自动增长),
    无归档时回退到 akshare 保守值 5。
    """
    try:
        import os as _os
        from archiver import _ARCHIVE_POOL_DIR
        pool_type = _TAB_POOL_TYPE.get(tab, 'limit_up')
        if not _os.path.exists(_ARCHIVE_POOL_DIR):
            return 5
        # 统计该 pool_type 的 pickle 文件数
        prefix = f'{pool_type}_'
        count = sum(1 for f in _os.listdir(_ARCHIVE_POOL_DIR)
                    if f.startswith(prefix) and f.endswith('.pkl'))
        return max(5, min(count, 60))  # 至少5天, 最多60天
    except Exception:
        return 5
# P1.2.1.2: 重要发现 (2026-06-09 23:15 调试得出):
# akshare 各池 API 实际可用窗口: stock_zt_pool_previous_em (~7天), stock_zt_pool_zbgc_em/dtgc_em (~7-10天)
# 实测 7 天前的 date 参数返回 0 行,不是抛异常而是"静默返回空"
# → 默认 max_days 全部从 30 改为 5,确保至少有 D/D+1/D+2 三个交易日可交易
# → 如需更长回测,等 akshare 数据范围扩大或换数据源

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
        cache_key: 2h 缓存键,命中后写入缓存避免重复读盘
    Returns:
        DataFrame or None
    """
    if not _LOCAL_FALLBACK_ENABLED or _load_pool_pickle is None:
        return None
    try:
        df = _load_pool_pickle(date_str, pool_type)
        if df is not None:
            _cache_put(cache_key, df)  # 写入 2h 缓存,后续请求秒回
            return df
    except Exception:
        pass
    return None

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
        _cache_put(key, df)
        return df
    _cache_put(key, '__NONE__')
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
        _cache_put(key, '__NONE__')
        return None
    # 列识别
    chg_col = None
    for c in df.columns:
        if '涨跌幅' in str(c):
            chg_col = c
            break
    chg_col = chg_col or df.columns[3]
    df['_chg'] = df[chg_col].astype(float)
    pullback = df[(df['_chg'] >= -7) & (df['_chg'] <= 1)].copy()
    if not pullback.empty:
        _cache_put(key, pullback)
    else:
        _cache_put(key, '__NONE__')
    return pullback


def _cached_pool_get(key: str):
    """缓存读取: 返回 DataFrame 或 None,避免 __NONE__ 触发 DataFrame 比较歧义"""
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
        return cached
    return None


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
        _cache_put(key, df)
        return df
    _cache_put(key, '__NONE__')
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
        _cache_put(key, df)
        return df
    _cache_put(key, '__NONE__')
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
        _cache_put(key, df)
        return df
    _cache_put(key, '__NONE__')
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
#  评分函数 (P1.1: limit-up / zhaban / dtqiaoban 可用)
# ═══════════════════════════════════════════

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
        scoring_base = scoring_base[scoring_base[price_col].astype(float) <= 200]
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

    total_scores, base_scores, danger_flags, weights = apply_scores(
        filtered, factors, sentiment_score, history_scores, lhb_bonus, today_fmt)

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
    """炸板评分: score_zhaban_data"""
    if df is None or df.empty:
        return None
    df = filter_non_main_board(df)
    if df.empty:
        return None
    # price filter
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    df = df[df[price_col].astype(float) <= 200]  # MAX_PRICE
    if df.empty:
        return None
    return score_zhaban_data(df, date_str)


def _score_dtqiaoban(df: pd.DataFrame, date_str: str):
    """跌停翘板评分: score_dtqiaoban_data"""
    if df is None or df.empty:
        return None
    df = filter_non_main_board(df)
    if df.empty:
        return None
    return score_dtqiaoban_data(df)


def _score_reversal(df: pd.DataFrame, date_str: str):
    """反转评分: P2.1 已抽到 scanner._score_reversal"""
    return scanner_score_reversal(df, today_str=date_str)


def _score_trend(df: pd.DataFrame, date_str: str):
    """趋势评分: P2.2 已抽到 scanner._score_trend

    df 来自 _fetch_trend_pool (stock_zt_pool_strong_em 当日强势池)
    _score_trend 内部已含板块/价格/市值过滤 + 评分
    """
    if df is None or df.empty:
        return None

    # 列识别 (与 scan_trend 主函数一致)
    df = df.copy()
    code_col = '代码' if '代码' in df.columns else df.columns[1]
    df = filter_non_main_board(df, code_col=code_col)

    cap_col = '流通市值' if '流通市值' in df.columns else None
    if cap_col and cap_col in df.columns:
        df = df[df[cap_col].astype(float) <= 200 * 1e8]  # MAX_MARKET_CAP

    price_col = '最新价' if '最新价' in df.columns else (df.columns[4] if len(df.columns) > 4 else None)
    if price_col and price_col in df.columns:
        df = df[df[price_col].astype(float) <= 200]  # MAX_PRICE

    change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
    changes = df[change_col].astype(float)
    df = df[(changes >= 2.5) & (changes < 8.5)]

    if df.empty:
        return None
    return scanner_score_trend(df)


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
    """
    global _SPOT_DISABLED
    if _SPOT_DISABLED:
        return {}  # 主循环会降级到 _get_ohlcv_batch 逐股

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
    capital: float = CAPITAL_DEFAULT,
    max_days: int = 30,
    use_cache: bool = True,
):
    """多 tab 回测主入口

    Args:
        tab: TAB_LIMIT_UP / TAB_TREND / TAB_ZHABAN / TAB_DTQIAOBAN / TAB_REVERSAL / TAB_SECTOR
        start_date/end_date: YYYYMMDD, 默认最近 30 个交易日
        top_n: 每个信号日取前 N 名
        capital: 单笔本金
        max_days: 默认 30 天
        use_cache: True 走 daily cache

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

    # ── 默认日期 ──
    # 自动检测本地归档可用天数 (超7天时无需手动改配置)
    tab_max = _detect_available_days(tab)
    if max_days > tab_max:
        max_days = tab_max

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

    # ── 整体结果缓存 ──
    if use_cache:
        cache_key = make_key("bt", "result", tab=tab,
                             start=start, end=end, top_n=top_n, capital=int(capital))
        cached = _daily_get(cache_key)
        if cached and 'summary' in cached:
            # 缓存命中也保存 tab 表现 (确保自动调权有数据)
            try:
                from weight_manager import save_tab_performance
                save_tab_performance(tab, cached.get('summary', {}))
            except Exception:
                pass
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
    records_open, records_close, records_stop, skipped, unbuyable_count = [], [], [], [], 0
    # 休盘买策略仅适用于趋势/反转 (信号日非涨停, 可买入)
    _close_buy_tabs = {TAB_TREND, TAB_REVERSAL}

    for d_signal in trade_dates:
        d_buy = _next_trading_date(d_signal)
        if d_buy is None or d_buy > trade_dates[-1]:
            skipped.append({'signal': d_signal, 'reason': '买入日超出区间'})
            continue
        d_sell = _next_trading_date(d_buy)
        # 趋势/反转: 策略C(休盘买)只需D+1, d_sell超区间也继续跑
        if tab in _close_buy_tabs and (d_sell is None or d_sell > trade_dates[-1]):
            d_sell = d_buy  # 策略A/B会被跳过, 仅策略C可用
        elif d_sell is None or d_sell > trade_dates[-1]:
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

            # 取 top_n
            top = df_scored.sort_values(actual_score_col, ascending=False).head(top_n)

            # ── P1.2 优化: 提前批量拉取 3 个日期的全市场 OHLCV ──
            daily_ohlcv = {}
            for d in [d_signal, d_buy, d_sell]:
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
                    continue

                signal_close = signal_ohlcv['close']
                gap_pct = round((buy_ohlcv['open'] / signal_close - 1) * 100, 1)
                # 买入过滤: 一字板排除; 跳空>5%高开出货陷阱
                limit_open = _is_limit_open(buy_ohlcv, signal_close)
                gap_trap = gap_pct > 5.0
                buyable = not limit_open and not gap_trap
                if not buyable:
                    unbuyable_count += 1
                    if gap_trap:
                        skipped.append({'signal': d_signal, 'reason': f'{name} 跳空{gap_pct:+.1f}%>5%高开陷阱'})

                intraday = {
                    'buy_high': round(buy_ohlcv['high'], 2),
                    'buy_low': round(buy_ohlcv['low'], 2),
                    'buy_close': round(buy_ohlcv['close'], 2),
                    'buy_turnover': round(buy_ohlcv['turnover'], 2),
                    'sell_high': round(sell_ohlcv['high'], 2),
                    'sell_low': round(sell_ohlcv['low'], 2),
                    'sell_close': round(sell_ohlcv['close'], 2),
                    'signal_close': round(signal_close, 2),
                    'gap_open_pct': gap_pct,
                    'buyable': buyable,
                }

                sell_px = sell_ohlcv['open']

                # ── 策略A: 开盘买 ──
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

                # ── 策略B: 尾盘买 ──
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

                # ── 策略C: 休盘买+止损 (趋势/反转专用) ──
                # 信号日收盘买入 → 次日低开≥3%止损开盘卖, 否则收盘卖
                if tab in _close_buy_tabs:
                    stop_loss_pct = -3.0  # 止损线: 低开3%
                    close_buy_price = signal_ohlcv['close']
                    d1_open = buy_ohlcv['open']
                    d1_close = buy_ohlcv['close']
                    gap_from_buy = (d1_open / close_buy_price - 1) * 100

                    if gap_from_buy <= stop_loss_pct:
                        sell_price_c = d1_open
                        exit_type = '止损'
                    else:
                        sell_price_c = d1_close
                        exit_type = '收盘'

                    raw_ret_s = (sell_price_c / close_buy_price - 1) * 100
                    net_ret_s = raw_ret_s - _COMMISSION_PCT - _SLIPPAGE_PCT
                    records_stop.append({
                        'signal_date': d_signal, 'buy_date': d_signal, 'sell_date': d_buy,
                        'rank': rank, 'code': code, 'name': name, 'score': round(sc, 1),
                        'buy_price': round(close_buy_price, 2),
                        'sell_price': round(sell_price_c, 2),
                        'raw_ret_pct': round(raw_ret_s, 2),
                        'net_ret_pct': round(net_ret_s, 2),
                        'pnl': round(capital * net_ret_s / 100, 0),
                        'exit_type': exit_type,
                        'gap_from_buy': round(gap_from_buy, 1),
                        'buy_high': round(buy_ohlcv['high'], 2),
                        'buy_low': round(buy_ohlcv['low'], 2),
                        'buy_close': round(d1_close, 2),
                        'signal_close': round(close_buy_price, 2),
                        'gap_open_pct': round(gap_from_buy, 1),
                        'buyable': True,
                    })

        except Exception as e:
            skipped.append({'signal': d_signal, 'reason': f'错误: {str(e)[:80]}'})
        time.sleep(0.5)

    # ── 聚合 ──
    sum_open = _aggregate(records_open, '开盘买')
    sum_close = _aggregate(records_close, '尾盘买')
    sum_stop = _aggregate(records_stop, '休盘买+止损')

    # ── 近30天聚合 ──
    cutoff_30d = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
    records_open_30d = [r for r in records_open if r['signal_date'] >= cutoff_30d]
    records_close_30d = [r for r in records_close if r['signal_date'] >= cutoff_30d]
    records_stop_30d = [r for r in records_stop if r['signal_date'] >= cutoff_30d]
    sum_open_30d = _aggregate(records_open_30d, '开盘买(近30天)')
    sum_close_30d = _aggregate(records_close_30d, '尾盘买(近30天)')
    sum_stop_30d = _aggregate(records_stop_30d, '休盘买+止损(近30天)')

    if sum_open is None and sum_close is None and sum_stop is None:
        empty_summary = {'trade_count': 0, 'win_rate': 0, 'avg_ret': 0,
                    'total_pnl': 0, 'plr': 0, 'max_dd': 0, 'best': 0,
                    'worst': 0, 'ev': 0, 'cumulative_ret': 0}
        result = {
            'summary': dict(empty_summary),
            'summary_30d': dict(empty_summary),
            'trades': [],
            'skipped': skipped,
            'generated_at': datetime.now().isoformat(),
            'config': {'tab': tab, 'start': start, 'end': end, 'top_n': top_n, 'capital': capital},
            'error': '无有效交易',
        }
        return result

    sorted_trades = sorted(records_open, key=lambda x: -x['net_ret_pct']) if records_open else []
    top5 = sorted_trades[:5]
    bot5 = sorted_trades[-5:][::-1] if len(sorted_trades) >= 5 else sorted_trades[::-1]

    result = {
        'summary': sum_open or sum_close,
        'summary_30d': sum_open_30d or sum_close_30d,
        'trades': records_open or records_close,
        'top5': top5, 'bottom5': bot5,
        'skipped': skipped,
        'generated_at': datetime.now().isoformat(),
        'config': {
            'tab': tab, 'start': start, 'end': end,
            'top_n': top_n, 'capital': capital,
            'commission_pct': _COMMISSION_PCT,
            'slippage_pct': _SLIPPAGE_PCT,
            'strategy': 'T+1 真实 (信号日 → D+1 开盘/尾盘买 → D+2 开盘卖)',
        },
        'comparison': {
            'open_buy': {'summary': sum_open, 'trades': records_open},
            'close_buy': {'summary': sum_close, 'trades': records_close},
            'stop_loss': {'summary': sum_stop, 'trades': records_stop},
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

    # 保存 tab 表现 → 自动调权
    try:
        from weight_manager import save_tab_performance
        save_tab_performance(tab, result.get('summary', {}))
    except Exception:
        pass

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