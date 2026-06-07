#!/usr/bin/env python3
"""
统一数据源层 — 所有 plan 共享的数据入口。

设计原则:
1. 在「拉取」阶段一次性拉取所有数据 → 缓存到 raw_scan_data.pkl
2. Plan 模块只从 inputs 读取,不直接调数据源
3. 新增数据源只需:
   a) 在本文件加 fetch_xxx() 函数
   b) 在 fetch_all_extended() 里注册
   c) 在 app._scan_limit_up_data 的 ThreadPoolExecutor 里加一个 future
   d) Plan 模块从 inputs 读取新字段即可

降级链: a-stock-data → akshare → 默认值 (永不崩溃)
"""

import sys
import pandas as pd

# ═══════════════════════════════════════════
#  北向资金 (a-stock-data > akshare > 默认)
# ═══════════════════════════════════════════

def fetch_north_flow(codes: list, date_str: str = None) -> dict:
    """
    拉取个股北向资金净流入。
    codes: 6位代码列表
    返回: {code: net_flow_yuan} 或空 dict
    """
    # 1) 尝试 a-stock-data 直连东财 push2
    try:
        from datasource import get_north_flow_batch
        result = get_north_flow_batch(codes, date_str=date_str)
        if result:
            return result
    except Exception:
        pass

    # 2) 尝试 akshare
    try:
        import akshare as ak
        df = ak.stock_hsgt_individual_em(symbol="沪股通")
        if df is not None and not df.empty:
            # 有数据但格式不确定,降级
            pass
    except Exception:
        pass

    # 3) 降级: 空 dict,调用方给默认分
    return {}


# ═══════════════════════════════════════════
#  融资融券 (a-stock-data > akshare > 默认)
# ═══════════════════════════════════════════

def fetch_margin_ratio(codes: list) -> dict:
    """
    拉取融资余额/流通市值 (%)。
    返回: {code: ratio_pct} 或空 dict
    """
    try:
        from datasource import get_margin_batch
        result = get_margin_batch(codes)
        if result:
            return result
    except Exception:
        pass

    try:
        import akshare as ak
        df = ak.stock_margin_detail_szse(date="")
        # 深市数据,格式不保证,降级跳过
    except Exception:
        pass

    return {}


# ═══════════════════════════════════════════
#  机构研报评级 (a-stock-data > akshare > 默认)
# ═══════════════════════════════════════════

def fetch_inst_rating(codes: list) -> dict:
    """
    拉取近30天机构研报评级变化。
    返回: {code: rating_str} — '上调'/'买入'/'增持'/'首次'/'中性'/'持有'/'减持'/'卖出'/''(无覆盖)
    """
    try:
        from datasource import get_rating_batch
        result = get_rating_batch(codes)
        if result:
            return result
    except Exception:
        pass

    try:
        import akshare as ak
        df = ak.stock_research_report_em()
        # 全市场研报,格式不保证
    except Exception:
        pass

    return {}


# ═══════════════════════════════════════════
#  涨停原因归因 (a-stock-data > akshare > 默认)
# ═══════════════════════════════════════════

def fetch_limit_reason(codes: list) -> dict:
    """
    拉取涨停原因归因(同花顺)。
    返回: {code: reason_str} — '政策'/'产业'/'突破'/'公告'/'业绩'/'重组'/'跟风'/'补涨'/'超跌'/'次新'/''(未知)
    """
    try:
        from datasource import get_limit_reason_batch
        result = get_limit_reason_batch(codes)
        if result:
            return result
    except Exception:
        pass

    # akshare 无对应接口,降级
    return {}


# ═══════════════════════════════════════════
#  统一入口 — app.py 调用
# ═══════════════════════════════════════════

def fetch_all_extended(filtered_df: pd.DataFrame, today_str: str = None) -> dict:
    """
    拉取所有扩展数据。app._scan_limit_up_data 在 Step 6 调用。
    返回 dict 直接 merge 到 raw_scan_data.pkl 和 plan.score() 的 inputs。

    filtered_df: 过滤后的涨停股 DataFrame
    today_str: YYYYMMDD 交易日
    """
    codes = [str(filtered_df.loc[i, '代码']).strip().zfill(6)
             for i in filtered_df.index]

    result = {
        'north_flow': {},
        'margin_ratio': {},
        'inst_rating': {},
        'limit_reason': {},
        'extended_ok': False,
    }

    # 串行拉取(减少并发风险,总耗时 < 15s)
    try:
        result['north_flow'] = fetch_north_flow(codes, today_str)
        print("  [数据源] 北向资金: " +
              f"{len(result['north_flow'])}/{len(codes)} 只",
              file=sys.stderr)
    except Exception as e:
        print(f"  [数据源] 北向资金错误: {e}", file=sys.stderr)

    try:
        result['margin_ratio'] = fetch_margin_ratio(codes)
        print("  [数据源] 融资融券: " +
              f"{len(result['margin_ratio'])}/{len(codes)} 只",
              file=sys.stderr)
    except Exception as e:
        print(f"  [数据源] 融资融券错误: {e}", file=sys.stderr)

    try:
        result['inst_rating'] = fetch_inst_rating(codes)
        print("  [数据源] 机构研报: " +
              f"{len(result['inst_rating'])}/{len(codes)} 只",
              file=sys.stderr)
    except Exception as e:
        print(f"  [数据源] 机构研报错误: {e}", file=sys.stderr)

    try:
        result['limit_reason'] = fetch_limit_reason(codes)
        print("  [数据源] 涨停归因: " +
              f"{len(result['limit_reason'])}/{len(codes)} 只",
              file=sys.stderr)
    except Exception as e:
        print(f"  [数据源] 涨停归因错误: {e}", file=sys.stderr)

    if any(len(v) > 0 for v in [
            result['north_flow'], result['margin_ratio'],
            result['inst_rating'], result['limit_reason']]):
        result['extended_ok'] = True

    return result
