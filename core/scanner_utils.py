"""scanner_utils.py - 纯工具函数层

职责: 提供与业务无关的纯函数和轻量时间/缓存工具,被其他 scanner_* 模块复用。
约束: 仅依赖 config / 标准库 / pandas, 不依赖其他 scanner_* 模块。
"""
import sys
import os
import time
from datetime import date, datetime, timezone, timedelta

import pandas as pd

from config import (
    _CST, _CACHE_DIR, _CACHE_TTL,
    MARKET_OPEN_MINUTES, MORNING_CLOSE_MINUTES, AFTERNOON_OPEN_MINUTES, AFTERNOON_CLOSE_MINUTES,
    SEAL_TIME_RANGE, MAX_LATE_SEAL, MAX_MARKET_CAP, MAX_PRICE, TOP_N,
)


# ═══════════════════════════════════════════
#  格式化工具
# ═══════════════════════════════════════════

def money_str(val) -> str:
    """金额格式化：1亿以上→X.XX亿，1万以上→X万，否则→整数"""
    try:
        v = float(val)
        if abs(v) >= 1e8: return f"{v/1e8:.2f}亿"
        if abs(v) >= 1e4: return f"{v/1e4:.0f}万"
        return f"{v:.0f}"
    except (ValueError, TypeError):
        return str(val)


# ═══════════════════════════════════════════
#  封板时间评分
# ═══════════════════════════════════════════

def seal_time_score(t: str) -> float:
    """封板时间评分 0-10: 与 _vectorized_seal_time_score 统一阶梯逻辑, 缩放到 0-10。
    早盘≤10:00=10.0, 尾盘>14:00=0.0。所有扫描模式共享。"""
    t = str(t).strip()
    try:
        if len(t) < 4:
            return 5.0
        minutes = int(t[:2]) * 60 + int(t[2:4])
        # 与 _vectorized_seal_time_score 相同阶梯, 缩放因子 10/12
        if minutes <= 0:
            return 5.0
        elif minutes <= 600:        # ≤10:00 → 12 分
            return 10.0
        elif minutes <= 630:        # 10:00-10:30 → 9 分
            return 7.5
        elif minutes <= 690:        # 10:30-11:30 → 6 分
            return 5.0
        elif minutes <= 780:        # 11:30-13:00 → 4 分
            return 3.3
        elif minutes <= 840:        # 13:00-14:00 → 2 分
            return 1.7
        else:                       # >14:00 → 0 分
            return 0.0
    except Exception:
        return 5.0


def _vectorized_seal_time_score(series: pd.Series) -> pd.Series:
    """封板时间阶梯化向量化版 (0-10): 与标量版 seal_time_score 范围统一 (BUG-6 修复)
    输入: 形如 '092500'/'14:25:00' 的字符串 Series
    输出: 同索引的 0-10 分 Series
    """
    s = series.astype(str)
    # 安全解析 HHMM -> minutes (无法解析的填 0)
    h = pd.to_numeric(s.str[:2], errors='coerce').fillna(0).astype(int)
    m = pd.to_numeric(s.str[2:4], errors='coerce').fillna(0).astype(int)
    minutes = h * 60 + m
    # 阶梯: 与标量版 seal_time_score 统一, 缩放到 0-10
    score = pd.Series(0.0, index=series.index)
    score[minutes <= 0] = 5.0  # 无法解析的默认中位 5
    score[(minutes > 0) & (minutes <= 600)] = 10.0  # ≤10:00 → 10
    score[(minutes > 600) & (minutes <= 630)] = 7.5  # 10:00-10:30 → 7.5
    score[(minutes > 630) & (minutes <= 690)] = 5.0  # 10:30-11:30 → 5
    score[(minutes > 690) & (minutes <= 780)] = 3.3  # 11:30-13:00 → 3.3
    score[(minutes > 780) & (minutes <= 840)] = 1.7  # 13:00-14:00 → 1.7
    # >14:00 留 0
    return score


# ═══════════════════════════════════════════
#  本地缓存 (日内, 根据市场状态动态 TTL)
# ═══════════════════════════════════════════

def _fund_flow_ttl() -> int:
    """资金流缓存 TTL:
    - 盘中 5 分钟(资金持续变动)
    - 盘后 4 小时(数据稳定)
    - 非交易日 24 小时
    """
    from cache import _is_trading_day
    now = datetime.now(_CST)
    if not _is_trading_day(now.strftime("%Y%m%d")):
        return 86400
    minute = now.hour * 60 + now.minute
    if (MARKET_OPEN_MINUTES <= minute < MORNING_CLOSE_MINUTES) or (AFTERNOON_OPEN_MINUTES <= minute < AFTERNOON_CLOSE_MINUTES):
        return 300
    return 14400


def _cache_put(name, df):
    """写本地 pickle 缓存 (缩字段: 列<20 时整表保存, 否则只保存 slim)"""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        slim = df.copy() if hasattr(df, 'columns') and len(df.columns) < 20 else df
        slim.to_pickle(os.path.join(_CACHE_DIR, f"{name}.pkl"))
    except Exception as e:
        print(f"  [scanner_utils] cache_put failed: {e}", file=sys.stderr)


def _cache_get(name, ttl_override: int = None):
    """读本地 pickle 缓存 (TTL 默认 _CACHE_TTL, 可指定 ttl_override)"""
    if ttl_override is None: ttl_override = _CACHE_TTL
    path = os.path.join(_CACHE_DIR, f"{name}.pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl_override:
            return pd.read_pickle(path)
    except Exception as e:
        print(f"  [scanner_utils] cache_get failed: {e}", file=sys.stderr)
    return None


# ═══════════════════════════════════════════
#  时间工具
# ═══════════════════════════════════════════

def get_today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def get_market_status(now=None):
    """
    检测当前市场状态。
    返回: 'trading' (盘中 9:30-15:00), 'lunch' (午休 11:30-13:00),
          'closed' (盘后), 'weekend' (周末), 'holiday' (节假日)
    """
    from cache import _is_trading_day
    if now is None:
        now = datetime.now(_CST)
    today_str = now.strftime("%Y%m%d")

    # 周末
    if now.weekday() >= 5:
        return 'weekend'

    # 非交易日
    if not _is_trading_day(today_str):
        return 'holiday'

    minute = now.hour * 60 + now.minute

    # 午休
    if MORNING_CLOSE_MINUTES <= minute < AFTERNOON_OPEN_MINUTES:
        return 'lunch'

    # 盘中
    if MARKET_OPEN_MINUTES <= minute < MORNING_CLOSE_MINUTES:
        return 'trading'
    if AFTERNOON_OPEN_MINUTES <= minute < AFTERNOON_CLOSE_MINUTES:
        return 'trading'

    # 盘前 / 盘后
    return 'closed'


def get_default_mode():
    """
    自动检测默认扫描模式:
    - 盘中 → 'trend' (趋势动量股，能买进)
    - 盘后/非交易日 → 'after_hours' (涨停多因子，次日预测)
    """
    status = get_market_status()
    if status == 'trading':
        return 'trend'
    return 'after_hours'
