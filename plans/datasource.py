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


# ═══════════════════════════════════════════
#  Phase 2: akshare 直连 (今天可用)
# ═══════════════════════════════════════════

def source_margin_akshare(date_str: str) -> pd.DataFrame:
    """
    融资融券个股余额 (akshare 直连, 沪深两市)。
    返回 DataFrame(code, margin_balance, market_cap) → Plan B 自己算 ratio。
    """
    frames = []
    try:
        import akshare as ak
        # 沪市
        dt = date_str.replace('-', '') if date_str else ''
        if dt:
            df_sse = ak.stock_margin_detail_sse(date=dt)
            if df_sse is not None and not df_sse.empty:
                frames.append(df_sse)
        # 深市
            df_szse = ak.stock_margin_detail_szse(date=dt)
            if df_szse is not None and not df_szse.empty:
                frames.append(df_szse)
    except Exception:
        pass
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    # 标准化列名
    for c in result.columns:
        cl = str(c).lower()
        if '股票代码' in str(c) or 'code' in cl or '代码' in str(c):
            result.rename(columns={c: 'code'}, inplace=True)
        if '融资余额' in str(c) or 'margin_balance' in cl:
            result.rename(columns={c: 'margin_balance'}, inplace=True)
    return result

SOURCES['margin_akshare'] = source_margin_akshare


def source_north_flow_market(date_str: str) -> pd.DataFrame:
    """
    北向资金市场总览 (akshare, 市场级)。
    返回 DataFrame(date, net_flow_沪股通, net_flow_深股通) → Plan B 判断当日北向方向。
    """
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em()
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['north_flow_market'] = source_north_flow_market


def source_industry_fund_flow(date_str: str) -> pd.DataFrame:
    """
    行业资金流向 (akshare 同花顺)。
    返回 DataFrame(行业名称, 主力净流入) → Plan B 判断行业资金方向。
    """
    try:
        import akshare as ak
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流向")
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['industry_fund_flow'] = source_industry_fund_flow
