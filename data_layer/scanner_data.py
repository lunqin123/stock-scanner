"""scanner_data.py - 数据获取层

职责: 封装所有"拉取外部数据"的入口,带缓存/降级/错误处理。
约束: 只依赖 utils/cache/akshare, 不调用 scoring/factors。
"""
import sys
from datetime import date, timedelta

import akshare as ak
import pandas as pd

from scanner_utils import _cache_get, _cache_put, _fund_flow_ttl


def fetch_limit_up_pool(date_str: str = None) -> pd.DataFrame:
    """获取指定日期涨停股池。非交易日或 API 故障返回空 DataFrame。

    失败降级策略: 尝试获取上一个交易日的数据。
    """
    if date_str is None:
        date_str = date.today().strftime("%Y%m%d")
    today_str = date_str
    try:
        df = ak.stock_zt_pool_em(date=today_str)
    except Exception:
        # 降级：尝试用上交易日日期
        print("  ⚠ akshare 涨停池获取失败，尝试降级...", file=sys.stderr)
        from cache import _last_trading_date
        yesterday = _last_trading_date()
        try:
            df = ak.stock_zt_pool_em(date=yesterday)
            if df is not None and not df.empty:
                print(f"  → 降级成功，使用上交易日数据：{len(df)} 只", file=sys.stderr)
                return df
        except Exception as e:
            print(f"  [scanner_data L29] failed: {e}", file=sys.stderr)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def fetch_fund_flow_data():
    """获取同花顺全市场资金流数据, 返回 (fund_df, error_msg), 带本地缓存。

    akshare 返回列序(2026-06-11 实测):
      0:序号 1:股票代码 2:股票简称 3:最新价 4:涨跌幅 5:换手率
      6:流入资金 7:流出资金 8:净额 9:成交额
    注: 涨跌幅/换手率带 '%' 后缀, 是字符串; 最新价是 float
    """
    cached = _cache_get("fund_flow", ttl_override=_fund_flow_ttl())
    if cached is not None:
        return cached, None
    try:
        fund_df = ak.stock_fund_flow_individual()
        fund_df = fund_df.copy()
        fund_df['_code'] = fund_df.iloc[:, 1].astype(str).str.replace(r'\D', '', regex=True).str.zfill(6)
        fund_df['_price'] = fund_df.iloc[:, 3].astype(float)
        fund_df['_net'] = fund_df.iloc[:, 8].astype(str)   # 净额 = 流入 - 流出
        # 无超大单/大单细分,统一置空
        fund_df['_super'] = '0'
        fund_df['_big'] = '0'
        # 只缓存需要的列,缩小磁盘写入量
        slim = fund_df[['_code', '_price', '_net', '_super', '_big']].copy()
        _cache_put("fund_flow", slim)
        return fund_df, None
    except Exception as e:
        print(f"  ! 资金流数据获取失败: {e}", file=sys.stderr)
        return None, str(e)
