"""scanner_filters.py - 数据过滤器层

职责: 提供所有"过滤不合格股票"的纯函数,统一板块/除权/价格/一字板逻辑。
约束: 仅依赖 config 和 pandas; 不发起网络请求。
"""
import sys

import pandas as pd

from config import MAX_MARKET_CAP, MAX_PRICE


# ─── 板块黑名单：非主板代码前缀（统一过滤，唯一真相来源） ───
# 科创板 68xxxx, 北交所 8xxxxx/92xxxx/94xxxx, 创业板 30xxxx/301xxx
# INCLUDE_CHINEXT=True 时, 把 '68' / '30' 从黑名单移出(只过滤 8/9 北交所)
# 注: 用 import config (而非 from config import) 让运行时修改 INCLUDE_CHINEXT 生效
import config

def _get_non_main_prefixes():
    """运行时读 config.INCLUDE_CHINEXT, 改完即时生效 (避免 import 固化)"""
    return ('68', '8', '9', '30') if not config.INCLUDE_CHINEXT else ('8', '9')

# 兼容历史: 模块级常量
NON_MAIN_BOARD_PREFIXES = _get_non_main_prefixes()


# ─── 除权除息前缀：当日价格机械性下调，非市场驱动 ───
# XR=除权, XD=除息, DR=除权除息
# 主涨停扫描不应过滤(填权行情可能涨停)，但反转/炸板/翘板必须排除
XR_XD_DR_PREFIXES = ('XR', 'XD', 'DR')


def filter_non_main_board(df: pd.DataFrame,
                          code_col: str = '代码',
                          name_col: str = '名称') -> pd.DataFrame:
    """统一板块过滤：排除 ST、北交所，可选排除科创板/创业板。
    所有扫描模式/端点必须调用此函数，不重复发明过滤逻辑。

    INCLUDE_CHINEXT=False (默认): 排除 ST + 科创板 + 北交所 + 创业板 (历史行为)
    INCLUDE_CHINEXT=True:        排除 ST + 北交所 (保留科创板/创业板 20% 涨停)
    """
    df = df.copy()
    # ST / *ST / 退市
    for nc in [name_col, '股票名称']:
        if nc in df.columns:
            names = df[nc].astype(str)
            st_mask = names.str.startswith(('ST', '*ST', '退', '退市'), na=False)
            df = df[~st_mask]
            break
    # 非主板代码 (运行时读 config, 改 INCLUDE_CHINEXT 后下次调用即时生效)
    if code_col in df.columns:
        df = df[~df[code_col].astype(str).str.startswith(_get_non_main_prefixes())]
    return df


def filter_xr_xd_dr(df: pd.DataFrame, name_col: str = '名称') -> pd.DataFrame:
    """过滤除权除息日股票：价格波动为机械性调整，非市场驱动。
    用于反转/炸板/翘板等"日内波动"评分场景，避免除权价格下调被误判为"回调洗盘"/"炸板分歧"。

    注意：主涨停扫描不应使用此函数——除权日涨停可能是"填权行情"，属真强势。
    """
    df = df.copy()
    for nc in [name_col, '股票名称']:
        if nc in df.columns:
            names = df[nc].astype(str)
            xr_mask = names.str.startswith(XR_XD_DR_PREFIXES, na=False)
            excluded = int(xr_mask.sum())
            if excluded > 0:
                print(f"  [XR/XD/DR过滤] 排除 {excluded} 只除权除息股", file=sys.stderr)
            df = df[~xr_mask]
            break
    return df


def pre_filter(df: pd.DataFrame) -> pd.DataFrame:
    """涨停扫描的前置过滤：排除非主板 + 一字板 + 市值过大 + 尾盘板。
    用于 fetch_limit_up_pool() 之后的标准管道第一步。
    """
    df = filter_non_main_board(df)

    if '换手率' in df.columns and '封板资金' in df.columns and '流通市值' in df.columns:
        # 一字板: 换手极低 + 封单相对流通市值比例高
        turnover = df['换手率'].fillna(0).astype(float)
        seal_ratio = df['封板资金'].fillna(0).astype(float) / df['流通市值'].fillna(0).astype(float).replace(0, float('inf'))
        yizi_mask = (turnover < 0.5) & (seal_ratio > 0.1)
        df = df[~yizi_mask]

    if '流通市值' in df.columns:
        from config import MAX_MARKET_CAP as _MAX_CAP
        cap_mask = df['流通市值'] > _MAX_CAP * 1e8
        df = df[~cap_mask]

    if '首次封板时间' in df.columns:
        from config import MAX_LATE_SEAL as _MAX_LATE
        late_mask = df['首次封板时间'].astype(str) >= _MAX_LATE
        df = df[~late_mask]

    return df


def filter_by_price(df: pd.DataFrame, fund_df) -> pd.DataFrame:
    """基于同花顺数据过滤股价超过 MAX_PRICE 的股票。
    fund_df: fetch_fund_flow_data() 返回的同花顺 DataFrame, 需含 _code/_price 列。
    """
    if fund_df is None or df is None or df.empty:
        return df
    code_to_price = {}
    for _, row in fund_df.iterrows():
        code_to_price[row['_code']] = float(row['_price'])

    mask = pd.Series(True, index=df.index)
    excluded = 0
    for idx in df.index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        price = code_to_price.get(code, 0)
        if price > MAX_PRICE:
            mask[idx] = False
            excluded += 1
    if excluded > 0:
        print(f"  → 股价过滤排除 {excluded} 只 (>{MAX_PRICE}元)", file=sys.stderr)
    return df[mask]
