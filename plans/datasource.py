#!/usr/bin/env python3
"""
数据源接口层 — 每个 source_xxx(date_str) → DataFrame, Plan 按需声明调用。

设计原则:
1. 每个数据源独立函数, Plan 通过 SOURCES 注册表按名调用
2. 降级链: a-stock-data → akshare → 空 DataFrame (永不崩溃)
3. app.py 根据 plan.PLAN_SOURCES 决定拉哪些源
"""

import sys
import pandas as pd

SOURCES = {}


def source_north_flow(date_str: str) -> pd.DataFrame:
    """北向资金个股净流入。返回 DataFrame(code, net_flow_yuan) 或空"""
    try:
        from datasource import get_north_flow_batch
        result = get_north_flow_batch([], date_str=date_str)
        if result:
            return pd.DataFrame(list(result.items()), columns=['code', 'net_flow_yuan'])
    except Exception:
        pass
    try:
        import akshare as ak
        df = ak.stock_hsgt_individual_em(symbol="沪股通")
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['north_flow'] = source_north_flow


def source_margin_ratio(date_str: str) -> pd.DataFrame:
    """融资融券余额/流通市值(%)"""
    try:
        from datasource import get_margin_batch
        result = get_margin_batch([])
        if result:
            return pd.DataFrame(list(result.items()), columns=['code', 'ratio_pct'])
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['margin_ratio'] = source_margin_ratio


def source_inst_rating(date_str: str) -> pd.DataFrame:
    """机构研报评级变化(近30天)"""
    try:
        from datasource import get_rating_batch
        result = get_rating_batch([])
        if result:
            return pd.DataFrame(list(result.items()), columns=['code', 'rating'])
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['inst_rating'] = source_inst_rating


def source_limit_reason(date_str: str) -> pd.DataFrame:
    """涨停原因归因(同花顺)"""
    try:
        from datasource import get_limit_reason_batch
        result = get_limit_reason_batch([])
        if result:
            return pd.DataFrame(list(result.items()), columns=['code', 'reason'])
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['limit_reason'] = source_limit_reason
