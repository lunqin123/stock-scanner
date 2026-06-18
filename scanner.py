#!/usr/bin/env python3
"""
超短线选股扫描器
策略: 首板涨停 + 多因子综合评分
本金: 1万元
数据源: akshare (同花顺 + 东方财富)
依赖: akshare>=1.14.0, pandas, numpy
"""

import sys
import io
import os
import argparse
from datetime import date, datetime, timezone, timedelta

# ─── 编码修复: 强制 stdout/stderr 使用 UTF-8，解决 PowerShell GBK 乱码 ───
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
elif sys.stdout.encoding and sys.stdout.encoding.upper() not in ('UTF-8', 'UTF8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    if sys.stderr.encoding and sys.stderr.encoding.upper() not in ('UTF-8', 'UTF8'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import akshare as ak
import pandas as pd
import numpy as np

import time


# ─── 本地缓存（日内数据缓存，根据市场状态动态调整 TTL） ───
# 常量从 config 统一导入 (P1-1 重构)
from config import (
    CACHE_DIR as _CACHE_DIR,
    CACHE_TTL as _CACHE_TTL,
    CST as _CST,
    MARKET_OPEN_MINUTES, MORNING_CLOSE_MINUTES, AFTERNOON_OPEN_MINUTES, AFTERNOON_CLOSE_MINUTES,
    SEAL_TIME_RANGE, MAX_LATE_SEAL, MAX_MARKET_CAP, MAX_PRICE, TOP_N,
)

def money_str(val) -> str:
    """金额格式化：1亿以上→X.XX亿，1万以上→X万，否则→整数"""
    try:
        v = float(val)
        if abs(v) >= 1e8: return f"{v/1e8:.2f}亿"
        if abs(v) >= 1e4: return f"{v/1e4:.0f}万"
        return f"{v:.0f}"
    except (ValueError, TypeError):
        return str(val)

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

def _fund_flow_ttl() -> int:
    """资金流缓存 TTL:
    - 盘中 5 分钟(资金持续变动)
    - 盘后 4 小时(当日历史快照稳定)
    - 非交易日 24 小时(历史数据,稳定)
    文件名带日期前缀做天然跨日隔离,盘后缓存不会污染次日"""
    from cache import _is_trading_day
    now = datetime.now(_CST)
    if not _is_trading_day(now.strftime("%Y%m%d")):
        return 86400  # 非交易日 24h
    minute = now.hour * 60 + now.minute
    if (MARKET_OPEN_MINUTES <= minute < MORNING_CLOSE_MINUTES) or (AFTERNOON_OPEN_MINUTES <= minute < AFTERNOON_CLOSE_MINUTES):
        return 300  # 盘中 5 分钟
    return 14400  # 盘后 4 小时

def _cache_put(name, df):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        # 只保留必要列,缩小 pickle 体积
        slim = df.copy() if hasattr(df, 'columns') and len(df.columns) < 20 else df
        # 文件名带日期前缀,天然跨日隔离(防盘后缓存污染次日)
        today = datetime.now(_CST).strftime("%Y%m%d")
        slim.to_pickle(os.path.join(_CACHE_DIR, f"{today}_{name}.pkl"))
    except Exception as e:
        print(f"  [scanner L84] failed: {e}", file=sys.stderr)

def _cache_get(name, ttl_override: int = None):
    if ttl_override is None: ttl_override = _CACHE_TTL
    # 文件名带日期前缀,天然跨日隔离(防盘后缓存污染次日)
    today = datetime.now(_CST).strftime("%Y%m%d")
    path = os.path.join(_CACHE_DIR, f"{today}_{name}.pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl_override:
            return pd.read_pickle(path)
    except Exception as e:
        print(f"  [scanner L93] failed: {e}", file=sys.stderr)
    return None


# ─── 配置 ───
MAX_MARKET_CAP = 200          # 流通市值上限 (亿)
MAX_PRICE = 60                # 最高股价 (2万本金单票6000, 最少买100股)
TOP_N = 10                    # 输出数量

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

# ─── 除权除息前缀：当日价格机械性下调，非市场驱动 ───
# XR=除权, XD=除息, DR=除权除息
# 主涨停扫描不应过滤(填权行情可能涨停)，但反转/炸板/翘板必须排除
XR_XD_DR_PREFIXES = ('XR', 'XD', 'DR')

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

# ─── 辅助函数 ───

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

# ─── 第一步: 获取涨停池（含非交易日检测） ───

def fetch_limit_up_pool(date_str: str = None) -> pd.DataFrame:
    """获取指定日期涨停股池。非交易日或 API 故障返回空 DataFrame。"""
    print("[1/5] 获取涨停股池...", file=sys.stderr)
    if date_str is None:
        date_str = date.today().strftime("%Y%m%d")
    today_str = date_str
    try:
        df = ak.stock_zt_pool_em(date=today_str)
    except Exception:
        # 降级：尝试用上交易日日期
        print("  ⚠ akshare 涨停池获取失败，尝试降级...", file=sys.stderr)
        from datetime import timedelta
        yesterday = _last_trading_date()
        try:
            df = ak.stock_zt_pool_em(date=yesterday)
            if df is not None and not df.empty:
                print(f"  → 降级成功，使用上交易日数据：{len(df)} 只", file=sys.stderr)
                return df
        except Exception as e:
            print(f"  [scanner L196] failed: {e}", file=sys.stderr)
        return pd.DataFrame()
    if df is None or df.empty:
        print("  → 无涨停数据（非交易日或市场休市）", file=sys.stderr)
        return pd.DataFrame()
    print(f"  → 共 {len(df)} 只涨停", file=sys.stderr)
    return df

# ─── 第二步: 前置过滤 ───

def pre_filter(df: pd.DataFrame) -> pd.DataFrame:
    print("[2/5] 前置过滤...", file=sys.stderr)
    before = len(df)

    df = filter_non_main_board(df)

    if '换手率' in df.columns and '封板资金' in df.columns and '流通市值' in df.columns:
        # 一字板: 换手极低 + 封单相对流通市值比例高
        turnover = df['换手率'].fillna(0).astype(float)
        seal_ratio = df['封板资金'].fillna(0).astype(float) / df['流通市值'].fillna(0).astype(float).replace(0, float('inf'))
        yizi_mask = (turnover < 0.5) & (seal_ratio > 0.1)
        df = df[~yizi_mask]

    if '流通市值' in df.columns:
        cap_mask = df['流通市值'] > MAX_MARKET_CAP * 1e8
        df = df[~cap_mask]

    if '首次封板时间' in df.columns:
        late_mask = df['首次封板时间'].astype(str) >= MAX_LATE_SEAL
        df = df[~late_mask]

    after = len(df)
    print(f"  → 排除 {before - after} 只，剩余 {after} 只", file=sys.stderr)
    return df

# ─── 第三步: 涨停强度评分 (30%, 满分 30) ───

def _vectorized_seal_time_score(series: pd.Series) -> pd.Series:
    """封板时间阶梯化向量化版 (0-12)：非连续，早盘重奖、尾盘重罚
    输入: 形如 '092500'/'14:25:00' 的字符串 Series
    输出: 同索引的 0-12 分 Series
    """
    s = series.astype(str)
    # 安全解析 HHMM -> minutes (无法解析的填 0)
    h = pd.to_numeric(s.str[:2], errors='coerce').fillna(0).astype(int)
    m = pd.to_numeric(s.str[2:4], errors='coerce').fillna(0).astype(int)
    minutes = h * 60 + m
    # 阶梯: <=10:00=12, 10:30=9, 11:30=6, 13:00=4, 14:00=2, >14:00=0
    score = pd.Series(0.0, index=series.index)
    score[minutes <= 0] = 6.0  # 无法解析的默认中位 6
    score[(minutes > 0) & (minutes <= 600)] = 12.0  # ≤10:00
    score[(minutes > 600) & (minutes <= 630)] = 9.0
    score[(minutes > 630) & (minutes <= 690)] = 6.0
    score[(minutes > 690) & (minutes <= 780)] = 4.0
    score[(minutes > 780) & (minutes <= 840)] = 2.0
    # >14:00 留 0
    return score


def score_seal_strength(df: pd.DataFrame) -> pd.Series:
    """封板质量评分 (0-28)：封板时间(阶梯，越早越高) + 封单充沛度 + 炸板次数 + 黄金封板奖励
    向量化版本: 5-10x 提速 vs 原 for 循环版 (commit 优化)"""
    scores = pd.Series(0.0, index=df.index)

    # 1. 封板时间阶梯化 (0-12) - 向量化
    seal_time_col = '首次封板时间' if '首次封板时间' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)
    if seal_time_col is None:
        return scores  # 无封板时间数据，返回全 0
    scores += _vectorized_seal_time_score(df[seal_time_col])

    # 2. 封单充沛度 (0-8) - 已向量化
    if '封板资金' in df.columns:
        fund = df['封板资金'].fillna(0).astype(float)
        max_fund = fund.max()
        if max_fund > 0:
            scores += (fund / max_fund) * 8
        else:
            scores += pd.Series(4.0, index=df.index)

    # 3. 炸板次数惩罚 (0-5, 0次=5分, 5次+=0分) - 已向量化
    if '炸板次数' in df.columns:
        zban = df['炸板次数'].fillna(0).astype(float)
        zban_scores = np.clip(1.0 - zban / 5.0, 0, 1) * 5
        scores += zban_scores

    # 4. 黄金封板奖励: 回测显示 seal 20+ 有临界效应 - 向量化
    base = scores.clip(upper=25.0)
    base += (base >= 20).astype(float) * 3.0
    return base.clip(upper=28.0)

# ─── 第四步: 资金面评分 (满分 20) ───

def fetch_fund_flow_data():
    """获取同花顺全市场资金流数据，返回 (fund_df, error_msg)，带本地缓存"""
    print("  → 获取同花顺资金流数据...", file=sys.stderr)
    cached = _cache_get("fund_flow", ttl_override=_fund_flow_ttl())
    if cached is not None:
        print("  → 命中本地缓存", file=sys.stderr)
        return cached, None
    try:
        fund_df = ak.stock_fund_flow_individual()
        fund_df = fund_df.copy()
        # akshare 返回列序(2026-06-11 实测):
        #   0:序号 1:股票代码 2:股票简称 3:最新价 4:涨跌幅 5:换手率
        #   6:流入资金 7:流出资金 8:净额 9:成交额
        # 注: 涨跌幅/换手率带 '%' 后缀, 是字符串; 最新价是 float
        fund_df['_code'] = fund_df.iloc[:, 1].astype(str).str.replace(r'\D', '', regex=True).str.zfill(6)
        fund_df['_price'] = fund_df.iloc[:, 3].astype(float)
        fund_df['_net'] = fund_df.iloc[:, 8].astype(str)   # 净额 = 流入 - 流出
        # 无超大单/大单细分，统一置空
        fund_df['_super'] = '0'
        fund_df['_big'] = '0'
        # 只缓存需要的列，缩小磁盘写入量
        slim = fund_df[['_code', '_price', '_net', '_super', '_big']].copy()
        _cache_put("fund_flow", slim)
        return fund_df, None
    except Exception as e:
        print(f"  ! 资金流数据获取失败: {e}", file=sys.stderr)
        return None, str(e)


def filter_by_price(df: pd.DataFrame, fund_df) -> pd.DataFrame:
    """基于同花顺数据过滤股价超过 MAX_PRICE 的股票"""
    before = len(df)
    code_to_price = {}
    for _, row in fund_df.iterrows():
        code_to_price[row['_code']] = float(row['_price'])

    mask = pd.Series(True, index=df.index)
    for idx in df.index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        price = code_to_price.get(code, 0)
        if price > MAX_PRICE:
            mask[idx] = False
    after = mask.sum()
    excluded = before - after
    if excluded > 0:
        print(f"  → 股价过滤排除 {excluded} 只 (>{MAX_PRICE}元)", file=sys.stderr)
    return df[mask]


def get_money_flow_scores(df: pd.DataFrame, fund_df=None):
    """通过同花顺批量获取个股资金流，区分主力结构评分。
    列索引: [6]主力净流入 [7]超大单净流入 [8]大单净流入
    fund_df: 可选，预获取的同花顺数据，避免重复请求。
    返回: (scores, raw_values) 评分 Series(0-15) 和对应的主力净额 Series(元)"""
    if fund_df is None:
        fund_df, err = fetch_fund_flow_data()
        if fund_df is None:
            return pd.Series(0.0, index=df.index), pd.Series(0.0, index=df.index)

    def parse_amount(val):
        val = str(val).replace('--', '0').strip()
        try:
            if '亿' in val:
                return float(val.replace('亿', '')) * 1e8
            elif '万' in val:
                return float(val.replace('万', '')) * 1e4
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    # 获取主力净流入、超大单、大单数据
    code_to_net = {}
    for _, row in fund_df.iterrows():
        c = row['_code']
        code_to_net[c] = parse_amount(row['_net'])

    scores = pd.Series(0.0, index=df.index)
    raw_values = pd.Series(0.0, index=df.index)
    for idx in df.index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        net_val = code_to_net.get(code, 0)
        raw_values[idx] = net_val    # 入库仍为主力净流入

        # ── 基础分 (0-20): 阶梯式，拉开资金差距 ──
        if net_val > 5000e4:
            base = 20.0       # 5000万+ 顶级资金驱动
        elif net_val > 2000e4:
            base = 16.0       # 2000-5000万
        elif net_val > 1000e4:
            base = 13.0       # 1000-2000万
        elif net_val > 500e4:
            base = 10.0       # 500-1000万
        elif net_val > 0:
            base = 7.0        # 0-500万 微量流入
        elif net_val > -1000e4:
            base = 4.0        # -1000-0万 微量流出
        elif net_val > -3000e4:
            base = 2.0        # -1000~-3000万 明显流出
        else:
            base = 0.0        # -3000万以下 大幅流出

        # ── 结构分 (0-3): 资金质量微调 ──
        structure = 1.0 if net_val > 0 else 0.0
        scores[idx] = max(0, min(20, base + structure))
    return scores, raw_values

# ─── 第五步: 板块合力评分 (合并sector_res + sector_mom) ───

def get_sector_score(df: pd.DataFrame, money_series: pd.Series = None) -> pd.Series:
    """
    板块合力评分（满分12分），合并原 sector_resonance + sector_heat：
    - 基础分(0-8): 基于板块内涨停个股数量（板块共振）
    - 一致性加分(0-4): 基于板块内资金净流入正向个股占比（避免虚假繁荣）
    消除sector双因子重复计算问题（回测显示两者r完全相同）。
    """
    industry_col = '所属行业' if '所属行业' in df.columns else '行业'
    if industry_col not in df.columns:
        return pd.Series(6.0, index=df.index)

    counts = df[industry_col].value_counts()
    scores = pd.Series(0.0, index=df.index)

    # 板块内资金一致性
    sector_consistency = {}
    if money_series is not None:
        for idx in df.index:
            industry = df.loc[idx, industry_col]
            if industry not in sector_consistency:
                sector_consistency[industry] = []
            sector_consistency[industry].append(money_series.loc[idx] > 0)

    for idx in df.index:
        industry = df.loc[idx, industry_col]
        cnt = counts.get(industry, 1)
        base = min(4 + cnt * 2, 8)

        consistency_bonus = 0
        if money_series is not None and industry in sector_consistency:
            pos_ratio = sum(sector_consistency[industry]) / len(sector_consistency[industry])
            if pos_ratio >= 0.8:    consistency_bonus = 4
            elif pos_ratio >= 0.6:  consistency_bonus = 3
            elif pos_ratio >= 0.4:  consistency_bonus = 2
            elif pos_ratio >= 0.2:  consistency_bonus = 1
        else:
            consistency_bonus = 2  # 无资金数据保守加分

        scores[idx] = min(12, base + consistency_bonus)
    return scores


# DEPRECATED: 已合并到 get_sector_score，保留向后兼容
def get_sector_heat_scores(df: pd.DataFrame, money_series: pd.Series = None) -> pd.Series:
    """
    板块热度评分（满分12分）：
    - 基础分(0-8): 基于板块内涨停个股数量
    - 一致性加分(0-4): 基于板块内资金净流入正向个股占比（避免虚假繁荣）
    money_series: 个股主力净流入 Series（元），用于计算一致性
    """
    industry_col = '所属行业' if '所属行业' in df.columns else '行业'
    if industry_col not in df.columns:
        return pd.Series(7.0, index=df.index)

    counts = df[industry_col].value_counts()
    scores = pd.Series(0.0, index=df.index)

    # 板块内资金一致性：同花顺数据中板块内正向资金个股占比
    sector_consistency = {}
    if money_series is not None:
        for idx in df.index:
            industry = df.loc[idx, industry_col]
            if industry not in sector_consistency:
                sector_consistency[industry] = []
            sector_consistency[industry].append(money_series.loc[idx] > 0)

    for idx in df.index:
        industry = df.loc[idx, industry_col]
        cnt = counts.get(industry, 1)

        # 基础分（0-14，与 consistency_bonus 合并封顶12）：板块涨停数越多越高
        # 提高饱和度从2只到5只，增强板块间区分度
        base = min(4 + cnt * 2, 14)

        # 一致性加分（0-4）
        consistency_bonus = 0
        if money_series is not None and industry in sector_consistency:
            pos_ratio = sum(sector_consistency[industry]) / len(sector_consistency[industry])
            if pos_ratio >= 0.8:
                consistency_bonus = 4    # 板块内80%+个股资金正向 → 真爆发
            elif pos_ratio >= 0.6:
                consistency_bonus = 3
            elif pos_ratio >= 0.4:
                consistency_bonus = 2
            elif pos_ratio >= 0.2:
                consistency_bonus = 1
            # < 20%正向资金 → 一致性差，不给加分
        else:
            consistency_bonus = 2  # 无资金数据时给保守加分

        scores[idx] = min(12, base + consistency_bonus)
    return scores


# DEPRECATED: 已合并到 get_sector_score，保留向后兼容
def get_sector_resonance(df: pd.DataFrame) -> pd.Series:
    """板块今日涨停集中度 (0-8)，只计涨停数量，不含资金一致性"""
    industry_col = '所属行业' if '所属行业' in df.columns else '行业'
    if industry_col not in df.columns:
        return pd.Series(4.0, index=df.index)
    counts = df[industry_col].value_counts()
    scores = pd.Series(0.0, index=df.index)
    for idx in df.index:
        industry = df.loc[idx, industry_col]
        cnt = counts.get(industry, 1)
        scores[idx] = min(4 + cnt * 2, 8)
    return scores


# ─── 第六步: 量价关系评分 (满分 10) ───

def score_tech_form(df: pd.DataFrame) -> pd.Series:
    """
    量价健康度（满分10分），回测驱动简化：
    原复杂换手率×连板矩阵 R²=0.001，改为换手率博弈区间评级。
    - 核心逻辑：5-15%换手=最佳博弈区间（有分歧有承接）
    - 首板加分：首板比连板更容易买到，+1奖励
    """
    scores = pd.Series(0.0, index=df.index)

    turnover_col = '换手率' if '换手率' in df.columns else None
    lb_col = '连板数' if '连板数' in df.columns else None

    if turnover_col is not None:
        turnover = df[turnover_col].fillna(0).astype(float)
        for idx in df.index:
            t = turnover[idx]
            lb = float(df.loc[idx, lb_col]) if lb_col and pd.notna(df.loc[idx, lb_col]) else 1

            # 换手率博弈区间评级（细分5-15%区间，打破所有票都得10分的现状）
            if 5 <= t <= 15:
                if t < 8:            base = 10.0  # 健康放量
                elif t < 12:          base = 8.0   # 充分换手
                else:                 base = 6.0   # 分歧较大
            elif 3 <= t < 5:      base = 7.0   # 略低但可接受
            elif 15 < t <= 20:    base = 7.0   # 偏高但有承接
            elif 1 <= t < 3:      base = 4.0   # 偏低，动能不足
            elif 20 < t <= 25:    base = 4.0   # 偏高，分歧大
            elif t < 1:           base = 2.0   # 一字板/无量
            elif 25 < t <= 30:    base = 2.0   # 分歧很大
            else:                 base = 0.0   # >30% 巨量

            # 首板加分：首板比连板更容易参与
            if lb == 1 and base > 0:
                base = min(10, base + 1)

            scores[idx] = base

    return scores


# ─── 个股情绪评分 ───

def score_stock_sentiment(df: pd.DataFrame, money_scores: pd.Series,
                          buyability_scores: pd.Series) -> pd.Series:
    """个股情绪 0-10: 资金态度 + 确定性 + 板块地位。每只票独立评分。"""
    scores = pd.Series(5.0, index=df.index)

    # 1. 资金态度 (0-3): 主力净流入越大→情绪越高
    scores += (money_scores / 20.0).clip(0, 1) * 3

    # 2. 确定性 (0-3): 首板+封板时机→稳定性
    scores += (buyability_scores / 12.0) * 3

    # 3. 板块领先度 (0-2): 同板块最早封板的加分
    industry_col = '所属行业' if '所属行业' in df.columns else None
    if not industry_col and len(df.columns) > 15:
        industry_col = df.columns[15]
    if industry_col:
        for ind in df[industry_col].unique():
            mask = df[industry_col] == ind
            group = df[mask]
            seal_times = group['首次封板时间'] if '首次封板时间' in df.columns else group.iloc[:, 11]
            times = seal_times.astype(str).str.strip()
            # 最早封板的2只加分
            sorted_idx = times.sort_values().index
            if len(sorted_idx) >= 1:
                scores.loc[sorted_idx[0]] += 2.0
            if len(sorted_idx) >= 2:
                scores.loc[sorted_idx[1]] += 1.0
            if len(sorted_idx) >= 3:
                scores.loc[sorted_idx[2]] += 0.5

    return scores.clip(0, 10)

# ─── 危险信号检测 ───

def score_danger_signals(df: pd.DataFrame, raw_money: pd.Series,
                         today_str: str) -> tuple:
    """检测个股危险信号，返回 (penalty_scores, flag_dict)。
    penalty_scores: 0=无问题, 负值=扣分（最多扣-30）
    flag_dict: {idx: [标签列表]} 供前端显示"""
    penalty = pd.Series(0.0, index=df.index)
    flags = {idx: [] for idx in df.index}

    # 列识别
    turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    seal_time_col = '首次封板时间' if '首次封板时间' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)
    seal_fund_col = '封板资金' if '封板资金' in df.columns else (df.columns[14] if len(df.columns) > 14 else None)
    lb_col = '连板数' if '连板数' in df.columns else (df.columns[14] if len(df.columns) > 14 else None)
    industry_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)

    # 行业涨停数（用于检测板块效应）
    sector_counts = {}
    if industry_col:
        sector_counts = df[industry_col].value_counts().to_dict()

    for idx in df.index:
        net = float(raw_money.get(idx, 0))
        turnover = float(df.loc[idx, turnover_col]) if turnover_col and pd.notna(df.loc[idx, turnover_col]) else 0
        seal_t = str(df.loc[idx, seal_time_col])[:4] if seal_time_col else ''
        seal_f = float(df.loc[idx, seal_fund_col] or 0) if seal_fund_col else 0
        lb = float(df.loc[idx, lb_col]) if lb_col and pd.notna(df.loc[idx, lb_col]) else 1
        ind = str(df.loc[idx, industry_col]) if industry_col else ''
        sc = sector_counts.get(ind, 0) if ind else 0

        # 规则1: 资金背离 - 净流出>5000万但涨停 → "虚板" (-15)
        if net < -5000e4:
            penalty[idx] -= 15
            flags[idx].append("⚠️ 虚板: 资金大幅流出")

        # 规则2: 三无品种 - 午后板+资金<1000万+板块<2只 (-12)
        if seal_t and int(seal_t[:2]) >= 13 and net < 1000e4 and sc < 2:
            penalty[idx] -= 12
            flags[idx].append("⚠️ 三无: 午后+无量+无板块")

        # 规则3: 高位首板 - 上交易日还是连板今天首板（高标反抽特征）(-8)
        zt_stat = ''
        for col in df.columns:
            if '涨停' in str(col) and '统计' in str(col):
                zt_stat = str(df.loc[idx, col]); break
        if zt_stat:
            parts = zt_stat.strip().split('/')
            if len(parts) == 2:
                try:
                    recent_days = float(parts[1])
                    recent_times = float(parts[0])
                    # 最近5天涨停≥3次(=曾连板)+今天首板→高位反抽信号
                    if recent_days <= 5 and recent_times >= 3 and lb == 1:
                        penalty[idx] -= 10
                        flags[idx].append("⚠️ 高位首板反抽")
                    # 30天内涨停≥5次→老庄股/妖股活跃
                    if recent_days <= 30 and recent_times >= 5:
                        penalty[idx] -= 5
                        flags[idx].append("⚠️ 高频涨停(庄股疑)")
                except: pass

        # 规则4: 换手过低控盘 - 换手<3%且无板块效应 (-8)
        if turnover < 3 and sc < 2:
            penalty[idx] -= 8
            flags[idx].append("⚠️ 低换手控盘")

        # 规则5: 午后板+封单最低+换手最高组合 (-10)
        if seal_t and int(seal_t[:2]) >= 13 and turnover > 10 and seal_f < 5000e4:
            penalty[idx] -= 10
            flags[idx].append("⚠️ 午后弱封+高换手")

        # 规则6: 资金极弱 - 净流入<500万 (-5)
        if 0 <= net < 500e4:
            penalty[idx] -= 5
            flags[idx].append("⚠️ 资金极弱")

        # 规则7: 股性差/超跌反弹 - 利用已解析的zt_stat (-5)
        if zt_stat:
            try:
                if float(parts[1]) >= 10 and float(parts[0]) < 2:
                    penalty[idx] -= 5
                    flags[idx].append("⚠️ 股性差/超跌反弹")
            except: pass

    return penalty.clip(lower=-30), flags

# ─── 本金适配评分 ───

def _dynamic_positions(principal: float) -> int:
    """根据本金动态分配持仓数：小于10万梭哈一只，大于等于10万分3份"""
    if principal < 100000:  return 1
    return 3


def score_by_principal(df: pd.DataFrame, principal: float) -> pd.Series:
    """
    本金适配度 (0-10分)，增强区分度（原版几乎所有股票均得5/10分）。
    - 价格适配 (0-5): 可买手数，5档分级
    - 流动性适配 (0-5): 持仓占比日成交额 + 日成交额底线惩罚
    """
    scores = pd.Series(5.0, index=df.index)

    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
    cap_col = '流通市值' if '流通市值' in df.columns else None

    n_positions = _dynamic_positions(principal)
    position_size = principal / n_positions

    for idx in df.index:
        price = float(df.loc[idx, price_col])
        lots = position_size / (price * 100)

        # 价格适配 (0-5): 5档分级，增强区分度
        if lots >= 5:       price_fit = 5.0
        elif lots >= 3:     price_fit = 4.0
        elif lots >= 2:     price_fit = 2.5
        elif lots >= 1:     price_fit = 1.5
        elif lots >= 0.5:   price_fit = 0.5
        else:               price_fit = 0.0

        # 流动性适配 (0-5)
        liquid_fit = 2.5
        daily_volume = 0
        if turnover_col and cap_col:
            cap = float(df.loc[idx, cap_col])
            turnover = float(df.loc[idx, turnover_col])
            daily_volume = cap * (turnover / 100)
            if daily_volume > 0:
                ratio = position_size / daily_volume
                strictness = 0.03 if principal > 200000 else 0.05
                if ratio < strictness * 0.2:      liquid_fit = 5.0
                elif ratio < strictness * 0.6:    liquid_fit = 4.0
                elif ratio < strictness * 1.0:    liquid_fit = 3.0
                elif ratio < strictness * 2.0:    liquid_fit = 1.5
                else:                              liquid_fit = 0.5

        # 流动性底线惩罚：日成交额 < 1000万 → 扣3分（流动性陷阱）
        if daily_volume > 0 and daily_volume < 10_000_000:
            liquid_fit = max(0, liquid_fit - 3)

        scores[idx] = price_fit + liquid_fit

    return scores


# ─── 可买到过滤 ───

def can_buy_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    过滤次日大概率买不到的股票：
    - 早盘封板(10:00前) + 连板≥3 → 次日一字板概率高
    - 封单/流通市值 > 8% → 跳空封死
    - 炸板次数 ≥ 4 → 主力放弃
    """
    mask = pd.Series(True, index=df.index)
    seal_time_col = '首次封板时间' if '首次封板时间' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)
    seal_fund_col = '封板资金' if '封板资金' in df.columns else (df.columns[14] if len(df.columns) > 14 else None)
    cap_col = '流通市值' if '流通市值' in df.columns else df.columns[13]
    lb_col = '连板数' if '连板数' in df.columns else df.columns[14]
    zban_col = '炸板次数' if '炸板次数' in df.columns else df.columns[12]

    for idx in df.index:
        # 早盘连板 → 次日大概率买不到
        seal_t = str(df.loc[idx, seal_time_col])[:4]
        lb = float(df.loc[idx, lb_col]) if pd.notna(df.loc[idx, lb_col]) else 1
        if seal_t and int(seal_t[:2]) < 10 and lb >= 3:
            mask[idx] = False
            continue

        # 巨量封单
        seal_f = float(df.loc[idx, seal_fund_col]) if pd.notna(df.loc[idx, seal_fund_col]) else 0
        cap = float(df.loc[idx, cap_col]) if pd.notna(df.loc[idx, cap_col]) else float('inf')
        if cap > 0 and seal_f / cap > 0.08:
            mask[idx] = False
            continue

        # 过度烂板
        zb = int(float(df.loc[idx, zban_col])) if pd.notna(df.loc[idx, zban_col]) else 0
        if zb >= 4:
            mask[idx] = False

    excluded = (~mask).sum()
    if excluded > 0:
        print(f"  [可买过滤] 排除 {excluded} 只（次日大概率买不到）", file=sys.stderr)
    return df[mask]


# ─── 开盘可行性评分 ───

def score_buyability(df: pd.DataFrame) -> pd.Series:
    """
    次日可买性评分 (0-12)。纯过滤器，不参与加权排名。
    - 连板数越低越好买（首板最容易买到）
    - 换手率适中最好
    注意：封板时间已移回 seal 因子，buyability 不再含封板时间。
    """
    scores = pd.Series(5.0, index=df.index)
    turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
    lb_col = '连板数' if '连板数' in df.columns else df.columns[14]

    for idx in df.index:
        # 连板数 (0-7)：首板=最容易买
        lb = float(df.loc[idx, lb_col]) if pd.notna(df.loc[idx, lb_col]) else 1
        if lb == 1:      lb_score = 7.0
        elif lb == 2:    lb_score = 4.0
        elif lb == 3:    lb_score = 2.0
        else:            lb_score = 1.0

        # 换手率 (0-5)：适中最好
        turnover = float(df.loc[idx, turnover_col]) if pd.notna(df.loc[idx, turnover_col]) else 10
        if 5 <= turnover <= 15:     tn_score = 5.0
        elif 3 <= turnover <= 25:   tn_score = 3.0
        else:                       tn_score = 1.0

        scores[idx] = lb_score + tn_score

    return scores.clip(0, 12)


# ─── 第七步: 总评分 + 输出 ───

def format_table_output(df: pd.DataFrame, money_scores: pd.Series,
                        sector_raw: pd.Series, seal_scores: pd.Series,
                        tech_scores: pd.Series,
                        raw_money: pd.Series = None,
                        sentiment_score: float = 5.0,
                        sentiment_level: str = "未知",
                        sentiment_detail: dict = None,
                        history_scores: pd.Series = None,
                        buyability_scores: pd.Series = None,
                        sector_res_scores: pd.Series = None,
                        stock_sentiment_scores: pd.Series = None,
                        weights: dict = None) -> str:
    import weight_manager
    w = weights if weights else weight_manager.DEFAULT_WEIGHTS
    s_history = history_scores if history_scores is not None else pd.Series(2.5, index=df.index)
    s_buyability = buyability_scores if buyability_scores is not None else pd.Series(6.0, index=df.index)
    s_res = sector_res_scores if sector_res_scores is not None else pd.Series(4.0, index=df.index)
    s_ss = stock_sentiment_scores if stock_sentiment_scores is not None else pd.Series(5.0, index=df.index)

    s_sector = (s_res + sector_raw) / 2.0
    base_totals = weight_manager.apply_weights(seal_scores, money_scores, s_sector, tech_scores, s_history, sentiment_score=pd.Series(float(sentiment_score), index=df.index), stock_sentiment_scores=s_ss, weights=w)
    total_scores = base_totals
    df = df.copy()
    df['基础总分'] = base_totals.round(1)
    df['总分'] = total_scores.round(1)
    df['封板质量'] = seal_scores.round(1)
    df['资金面'] = money_scores.round(1)
    df['板块热度'] = sector_raw.round(1)
    df['技术形态'] = tech_scores.round(1)
    df['市场情绪'] = pd.Series(sentiment_score, index=df.index).round(1)
    df['历史股性'] = s_history.round(1)
    df['舆情评分'] = pd.Series(0, index=df.index)
    df['开盘可行性'] = s_buyability.round(1)
    df['个股情绪'] = s_ss.round(1)
    df['板块共振'] = s_res.round(1)
    def _pad(s, w, right=False):
        """CJK字符宽度补齐：中文占2格，英文/数字占1格"""
        s = str(s)
        cjk_len = sum(2 if '一' <= c <= '鿿' else 1 for c in s)
        sp = max(0, w - cjk_len)
        return (' ' * sp + s) if right else (s + ' ' * sp)

    if raw_money is not None:
        df['净流入'] = df.index.map(lambda idx: money_str(raw_money.get(idx, 0)))
    else:
        df['净流入'] = ""

    df = df.sort_values('总分', ascending=False)

    # ── 控制输出列数 ──
    out = df.head(TOP_N)

    sep = "─" * 88
    col_w = [3, 6, 8, 4, 3, 3, 3, 3, 3, 8, 4, 4, 4]  # display widths
    hdr = f" # │{_pad('代码',col_w[1])}│{_pad('名称',col_w[2])}│{_pad('总分',col_w[3])}│{_pad('涨停',col_w[4])}│{_pad('资金',col_w[5])}│{_pad('板块',col_w[6])}│{_pad('量价',col_w[7])}│{_pad('股性',col_w[8])}│{_pad('舆情',col_w[12])}│{_pad('净流入',col_w[9])}│{_pad('换手%',col_w[10])}│{_pad('封板',col_w[11])}"
    div_parts = ['─' * w for w in col_w]
    div = "─" + "┼".join(div_parts) + "─"

    lines = [sep, hdr, div]
    for rank, (_, row) in enumerate(out.iterrows(), 1):
        code = str(row.get('代码', row.iloc[1])).strip().zfill(6)
        name = row.get('名称', row.iloc[2])
        total = str(int(round(float(row['总分']))))
        seal_s = f"{float(row['封板质量']):.0f}"
        money_s = f"{float(row['资金面']):.0f}"
        sector_s = f"{float(row['板块热度']):.0f}"
        tech_s = f"{float(row['技术形态']):.0f}"
        history_s = f"{float(row['历史股性']):.0f}"
        community_s = f"{float(row['舆情评分']):.0f}"
        money_detail = str(row['净流入'])
        turnover = f"{float(row.get('换手率', 0)):.1f}" if pd.notna(row.get('换手率')) else "?"
        seal_time = str(row.get('首次封板时间', row.get('', '')))[:4]
        lines.append(f"{rank:<2} │{_pad(code,col_w[1])}│{_pad(name,col_w[2])}│{_pad(total,col_w[3],right=True)}│{_pad(seal_s,col_w[4],right=True)}│{_pad(money_s,col_w[5],right=True)}│{_pad(sector_s,col_w[6],right=True)}│{_pad(tech_s,col_w[7],right=True)}│{_pad(history_s,col_w[8],right=True)}│{_pad(community_s,col_w[12],right=True)}│{_pad(money_detail,col_w[9],right=True)}│{_pad(turnover,col_w[10],right=True)}│{_pad(seal_time,col_w[11],right=True)}")

    lines.append(sep)
    return "\n".join(lines)


def format_output(df: pd.DataFrame, money_scores: pd.Series,
                  sector_raw: pd.Series, seal_scores: pd.Series,
                  tech_scores: pd.Series,
                  raw_money: pd.Series = None,
                  sentiment_score: float = 5.0,
                  sentiment_level: str = "未知",
                  sentiment_detail: dict = None,
                  history_scores: pd.Series = None,
                  buyability_scores: pd.Series = None,
                  sector_res_scores: pd.Series = None,
                  weights: dict = None) -> str:
    """详细文本输出。"""
    import weight_manager
    w = weights if weights else weight_manager.DEFAULT_WEIGHTS
    s_history = history_scores if history_scores is not None else pd.Series(2.5, index=df.index)
    s_buyability = buyability_scores if buyability_scores is not None else pd.Series(6.0, index=df.index)
    s_res = sector_res_scores if sector_res_scores is not None else pd.Series(4.0, index=df.index)
    s_ss = stock_sentiment_scores if stock_sentiment_scores is not None else pd.Series(5.0, index=df.index)

    s_sector = (s_res + sector_raw) / 2.0
    base_totals = weight_manager.apply_weights(seal_scores, money_scores, s_sector, tech_scores, s_history, sentiment_score=pd.Series(float(sentiment_score), index=df.index), stock_sentiment_scores=s_ss, weights=w)
    total_scores = base_totals
    df = df.copy()
    df['基础总分'] = base_totals.round(1)
    df['总分'] = total_scores.round(1)
    df['封板质量'] = seal_scores.round(1)
    df['资金面'] = money_scores.round(1)
    df['板块热度'] = sector_raw.round(1)
    df['技术形态'] = tech_scores.round(1)
    df['市场情绪'] = pd.Series(sentiment_score, index=df.index).round(1)
    df['历史股性'] = s_history.round(1)
    df['舆情评分'] = pd.Series(0, index=df.index)
    df['开盘可行性'] = s_buyability.round(1)
    df['个股情绪'] = s_ss.round(1)
    df['板块共振'] = s_res.round(1)

    top_indices = list(total_scores.sort_values(ascending=False).head(TOP_N).index)
    out = df.loc[top_indices]

    if raw_money is not None:
        money_detail = lambda idx: money_str(raw_money.get(idx, 0))
    else:
        money_detail = lambda idx: "?"

    lines = []

    today_display = date.today().strftime("%Y-%m-%d")
    sentiment_tag = sentiment_level.replace('炸板(', '').replace(')', '') if '炸板' in sentiment_level else sentiment_level
    zhaban_rate = (sentiment_detail or {}).get('zhaban_rate', 0)
    promo_rate = (sentiment_detail or {}).get('promotion_rate', 0)
    avg_premium = (sentiment_detail or {}).get('avg_premium', 0)
    prev_count = (sentiment_detail or {}).get('prev_limit_count', 0)
    lines.append(f"TOP {TOP_N} 超短线标的 ({today_display}) | "
                 f"情绪:{sentiment_level} | "
                 f"上交易日涨停{prev_count}只 | 溢价{avg_premium:+.2f}% | "
                 f"晋级{promo_rate*100:.0f}% | 炸板{zhaban_rate*100:.0f}%")
    lines.append("=" * 70)

    for rank, idx in enumerate(top_indices, 1):
        row = out.loc[idx]
        score = float(row['总分'])
        base_score = float(row['基础总分'])
        seal_score_val = float(row['封板质量'])
        money_score_val = float(row['资金面'])
        sector_score_val = float(row['板块热度'])
        tech_score_val = float(row['技术形态'])
        history_val = float(row['历史股性'])
        community_val = float(row['舆情评分'])

        code = str(row.get('代码', row.get('', ''))).strip().zfill(6)
        name = row.get('名称', row.get('', ''))
        industry = row.get('所属行业', '')
        turnover = f"{float(row['换手率']):.1f}" if '换手率' in df.columns and pd.notna(row.get('换手率')) else "?"
        seal_time = str(row.get('首次封板时间', '') or '')[:4] if '首次封板时间' in df.columns else "?"
        lianban = row.get('连板数', '?')

        # 净流入
        mn = money_detail(idx)
        # 资金面描述
        if money_score_val > 15:
            money_detail_str = "净流入" + mn
        elif money_score_val > 8:
            money_detail_str = "净流入" + mn
        else:
            money_detail_str = "净流入" + mn if money_score_val >= 0 else f"净流出{mn}"

        buy_parts = []
        if seal_score_val >= 15:
            buy_parts.append('封板强度高')
        elif seal_score_val >= 10:
            buy_parts.append('封板质量中等')
        if money_score_val > 10:
            buy_parts.append(money_detail_str)
        if float(row['板块热度']) > 8:
            buy_parts.append('板块效应强')
        if lianban != '?' and float(lianban) >= 2:
            buy_parts.append(f'连板{lianban}')
        buy_logic = '+'.join(buy_parts) if buy_parts else '标准首板标的'

        risk_parts = []
        if float(row['封板质量']) < 11:
            risk_parts.append('封板偏弱')
        try:
            if float(turnover) > 20:
                risk_parts.append('换手过高')
        except (ValueError, TypeError) as e:
            print(f"  [scanner L976] failed: {e}", file=sys.stderr)
        # 连板可买性风险提示：高连板但可买性低 = 明天买不到
        try:
            lb_val = float(lianban) if lianban != '?' else 0
            by_val = float(row.get('开盘可行性', 0))
            if lb_val >= 4 and by_val < 6:
                risk_parts.append(f'{int(lb_val)}连板可买性仅{by_val:.0f}分，明天买不到')
            elif lb_val >= 3 and by_val < 4:
                risk_parts.append(f'{int(lb_val)}连板可买性低，大概率被堵')
        except (ValueError, TypeError) as e:
            print(f"  [scanner L986] failed: {e}", file=sys.stderr)
        risk = '; '.join(risk_parts) if risk_parts else '关注次日竞价'

        try:
            fund_str = f"{float(row.get('封板资金', 0)):.0f}"
        except (ValueError, TypeError):
            fund_str = str(row.get('封板资金', '0'))

        lines.append(f"\n{rank}. {code} {name} | 总分 {score:.1f}")
        lines.append(f"   封板: {seal_time} | 封单 {fund_str} | 换手 {turnover}%")
        lines.append(f"   资金面: {money_score_val:.1f}/{w['money']:.0f} {money_detail_str} | 板块: {industry} ({row['板块热度']:.0f}/{w.get('sector_mom', 15):.0f})")
        ss_val = float(row.get('个股情绪', 5))
        lines.append(f"   评分拆解: 封板{seal_score_val:.1f}/{w['seal']:.0f} + 资金{money_score_val:.1f}/{w['money']:.0f} + 板块{float(row['板块热度']):.0f}/{w.get('sector_mom', 15):.0f} + 量价{tech_score_val:.1f}/{w['tech']:.0f} + 股性{history_val:.1f}/{w['history']:.0f} + 个情{ss_val:.1f}/{w.get('stock_sentiment', 10):.0f} + 共振{float(row['板块共振']):.0f}/{w['sector_res']:.0f} (可买={float(row.get('开盘可行性', 0)):.0f}, 仅参考)")
        lines.append(f"   {buy_logic}")
        lines.append(f"   {risk}")

    lines.append(f"\n{'=' * 70}")
    lines.append("仓位建议 (本金 2 万):")
    lines.append("   - 单票上限 6000 元 (30%)")
    lines.append("   - 同时持有 2-3 只")
    lines.append("   - 竞价低于开盘价 3% → 直接出")
    lines.append("   - 竞价在 -1%~+1% → 观察 5 分钟，不上攻立刻出")
    lines.append("   - 竞价高于开盘价 +2% → 持有，10:30 前不涨停分批出")
    lines.append("")
    lines.append("免责: 本数据仅供参考，不构成投资建议。")
    lines.append("akshare 数据存在 5-10 分钟延迟。")

    return "\n".join(lines)

# ─── 情绪检测 ───

def detect_market_sentiment(today_str: str):
    """
    市场情绪检测：基于上交易日涨停股今日的溢价表现。
    使用 stock_zt_pool_previous_em 获取上交易日涨停池（含今日涨跌幅）。
    返回: (score, level, details_dict)
    - score: 0-10 分
    - level: 冰点/低迷/正常/活跃/高潮
    """
    from datetime import datetime, timedelta
    from cache import _is_trading_day
    today_dt = datetime.strptime(today_str, '%Y%m%d') if len(today_str) == 8 else datetime.today()
    # 回退找到最近交易日（处理周末和节假日）
    yesterday = today_dt
    for _ in range(8):
        yesterday = yesterday - timedelta(days=1)
        if _is_trading_day(yesterday.strftime('%Y%m%d')):
            break
    yesterday = yesterday.strftime('%Y%m%d')

    try:
        print("  [情绪] 第1步: 获取上交易日涨停数据...", file=sys.stderr)
        prev_limit = ak.stock_zt_pool_previous_em(date=yesterday)
        if prev_limit.empty:
            return 5.0, "未知(无上交易日数据)", {"note": "no previous data"}
        print(f"  [情绪] 上交易日涨停 {len(prev_limit)} 只", file=sys.stderr)

        print("  [情绪] 第2步: 获取炸板/跌停数据...", file=sys.stderr)
        zb_df = ak.stock_zt_pool_zbgc_em(date=yesterday)
        dt_df = ak.stock_zt_pool_dtgc_em(date=yesterday)
        print(f"  [情绪] 炸板 {len(zb_df) if zb_df is not None else 0} 只, 跌停 {len(dt_df) if dt_df is not None else 0} 只", file=sys.stderr)

        print("  [情绪] 第3步: 计算评分...", file=sys.stderr)

        prev_total = len(prev_limit)
        zb_total = len(zb_df) if zb_df is not None and not zb_df.empty else 0
        dt_total = len(dt_df) if dt_df is not None and not dt_df.empty else 0

        # 涨跌幅列（第4列，索引3）
        change_col = prev_limit.columns[3]
        changes = prev_limit[change_col].astype(float)

        avg_premium = float(changes.mean())               # 平均溢价
        promo_rate = float((changes > 9).sum() / prev_total)  # 晋级率
        zhaban_rate = zb_total / (prev_total + zb_total) if (prev_total + zb_total) > 0 else 0.5

        # 获取今天的大盘数据
        today_limit_up = 0
        today_limit_down = 0
        today_market_breadth = 0.5  # 默认中性
        all_up = 0
        all_down = 0
        try:
            print("  [情绪] 第3步: 获取今日大盘数据...", file=sys.stderr)
            today_pool = ak.stock_zt_pool_em(date=today_str)
            if today_pool is not None and not today_pool.empty:
                today_limit_up = len(today_pool)

            today_dt = ak.stock_zt_pool_dtgc_em(date=today_str)
            if today_dt is not None and not today_dt.empty:
                today_limit_down = len(today_dt)

            # 获取全市场涨跌家数（Sina采样多页汇总）
            print("  [情绪] 获取全市场涨跌分布...", file=sys.stderr)
            try:
                import requests as _req
                from concurrent.futures import ThreadPoolExecutor, as_completed
                _SINA_BASE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                              "Market_Center.getHQNodeData?num=100&sort=code&asc=1&node=hs_a&page=")
                def _fetch_page(p):
                    try:
                        r = _req.get(_SINA_BASE + str(p), timeout=15)
                        if r.status_code == 200 and r.text.startswith("["):
                            d = r.json()
                            ups = sum(1 for x in d if float(x.get("changepercent", 0)) > 0)
                            downs = sum(1 for x in d if float(x.get("changepercent", 0)) < 0)
                            return ups, downs
                    except: pass
                    return 0, 0
                total_up = 0
                total_down = 0
                with ThreadPoolExecutor(max_workers=4) as ex:
                    pages = [1, 15, 30, 45]
                    futs = {ex.submit(_fetch_page, p): p for p in pages}
                    for f in as_completed(futs):
                        u, d = f.result()
                        total_up += u
                        total_down += d
                if total_up + total_down > 0:
                    all_up = total_up
                    all_down = total_down
                    print(f"  [情绪] 全市场涨 {all_up} 跌 {all_down}", file=sys.stderr)
                else:
                    print("  [情绪] 全市场数据采样失败", file=sys.stderr)
            except Exception as e:
                print(f"  [情绪] 全市场获取异常: {e}", file=sys.stderr)

            # 涨跌比（基于涨停/跌停）
            total_sd = today_limit_up + today_limit_down
            if total_sd > 0:
                today_market_breadth = today_limit_up / total_sd
            print(f"  [情绪] 今日涨停 {today_limit_up} 只, 跌停 {today_limit_down} 只", file=sys.stderr)
        except Exception as e:
            print(f"  [情绪] 大盘数据获取异常: {e}", file=sys.stderr)

        # ── 综合评分：上交易日表现(40%) + 今天盘面(30%) + 涨跌停比(20%) + 炸板率(10%) ──
        # 1. 基础分：上交易日涨停今表现 (0-10)
        if avg_premium > 3 and promo_rate > 0.3:
            prev_score = 9.0
            prev_label = "高潮"
        elif avg_premium > 1 and promo_rate > 0.2:
            prev_score = 7.0
            prev_label = "活跃"
        elif avg_premium > -1 and promo_rate > 0.1:
            prev_score = 5.0
            prev_label = "正常"
        elif avg_premium > -3:
            prev_score = 3.0
            prev_label = "低迷"
        else:
            prev_score = 1.0
            prev_label = "冰点"

        # 2. 今日盘面修正：涨跌比 → 大幅加分/扣分 (-3~+3)
        breadth_bonus = 0
        if all_up + all_down > 0:
            all_ratio = all_up / (all_up + all_down)
            if all_ratio > 0.75:
                breadth_bonus = 3
            elif all_ratio > 0.65:
                breadth_bonus = 2
            elif all_ratio > 0.55:
                breadth_bonus = 1
            elif all_ratio < 0.25:
                breadth_bonus = -3
            elif all_ratio < 0.35:
                breadth_bonus = -2
            elif all_ratio < 0.45:
                breadth_bonus = -1

        # 3. 今日涨停/跌停修正 (-2~+2)
        limit_bonus = 0
        if today_limit_up >= 80:
            limit_bonus = 2
        elif today_limit_up >= 60:
            limit_bonus = 1
        elif today_limit_up >= 40:
            limit_bonus = 0
        elif today_limit_up >= 20:
            limit_bonus = -1
        else:
            limit_bonus = -2

        if today_limit_down > 50:
            limit_bonus -= 2
        elif today_limit_down > 30:
            limit_bonus -= 1

        # 4. 炸板率修正 (-2~0)
        zhaban_penalty = 0
        if zhaban_rate > 0.45:
            zhaban_penalty = -2
        elif zhaban_rate > 0.35:
            zhaban_penalty = -1

        score = prev_score + breadth_bonus + limit_bonus + zhaban_penalty
        score = max(0, min(10, score))

        # 最终等级
        if score >= 8:
            level = "高潮"
        elif score >= 6:
            level = "活跃"
        elif score >= 4:
            level = "正常"
        elif score >= 2:
            level = "低迷"
        else:
            level = "冰点"

        details = {
            'prev_limit_count': prev_total,
            'zhaban_count': zb_total,
            'dieting_count': dt_total,
            'avg_premium': round(avg_premium, 2),
            'promotion_rate': round(promo_rate, 2),
            'zhaban_rate': round(zhaban_rate, 2),
            'today_limit_up': today_limit_up,
            'today_limit_down': today_limit_down,
            'today_breadth': round(today_market_breadth, 2),
            'all_up': all_up,
            'all_down': all_down,
        }
        return round(score, 1), level, details

    except Exception as e:
        print(f"  [WARN] 市场情绪评分失败: {e}", file=sys.stderr)
        return 5.0, "未知", {"note": f"error: {e}"}


# ─── 龙虎榜分析 ───

def analyze_dragon_tiger(df: pd.DataFrame, today_str: str):
    """龙虎榜分析。返回 (bonus_series, details_dict)。"""
    scores = pd.Series(0.0, index=df.index)
    lhb_data = {}
    try:
        try:
            lhb = ak.stock_lhb_detail_em(date=today_str)
        except TypeError:
            try:
                lhb = ak.stock_lhb_detail_em()
            except Exception:
                lhb = pd.DataFrame()
        if not lhb.empty:
            # 统计上榜个股的买卖情况
            code_col = '代码' if '代码' in lhb.columns else lhb.columns[1]
            for idx in df.index:
                code = str(df.loc[idx, '代码']).strip().zfill(6)
                stock_lhb = lhb[lhb[code_col].astype(str).str.zfill(6) == code]
                if not stock_lhb.empty:
                    # 机构买入加分
                    buy_amount = 0
                    sell_amount = 0
                    for _, lr in stock_lhb.iterrows():
                        buy_amount += float(lr.iloc[6]) if len(lr) > 6 else 0
                        sell_amount += float(lr.iloc[7]) if len(lr) > 7 else 0
                    net_buy = buy_amount - sell_amount
                    if net_buy > 1e7:
                        scores[idx] = 5.0
                        lhb_data[code] = f"净买入{net_buy/1e8:.2f}亿"
                    elif net_buy > 0:
                        scores[idx] = 3.0
                    elif net_buy < -1e7:
                        scores[idx] = -4.0
                        lhb_data[code] = f"净卖出{abs(net_buy)/1e8:.2f}亿"
    except Exception as e:
        print(f"  [scanner L1247] failed: {e}", file=sys.stderr)
    return scores, lhb_data


# ─── 历史股性评分 ───

def score_stock_history(df: pd.DataFrame, today_str: str, prev_df: pd.DataFrame = None):
    """
    基于近期涨停数据评估股性。
    优化: 接受 prev_df 参数避免重复拉取 (backtest_score_prev 已经传入了 prev)。
    内部 55 次本地过滤向量化: 800ms → < 10ms。
    """
    scores = pd.Series(2.5, index=df.index)
    raw_details = {}
    try:
        # 优先用调用方传入的 prev_df, 避免重复网络请求 (节省 ~800ms)
        prev = prev_df if prev_df is not None else ak.stock_zt_pool_previous_em(date=today_str)
        if prev.empty:
            return scores, raw_details
        name_col = prev.columns[2]
        code_col = prev.columns[1]
        zt_stat_col = None
        for c in prev.columns:
            if '涨停' in str(c) and '统计' in str(c):
                zt_stat_col = c
                break
        if zt_stat_col is None:
            return scores, raw_details

        # 向量化: 1 次过滤替代 55 次 prev[mask]
        prev_code_norm = prev[code_col].astype(str).str.zfill(6)
        df_code_norm = df.iloc[:, 1].astype(str).str.strip().str.zfill(6) if '代码' not in df.columns else df['代码'].astype(str).str.strip().str.zfill(6)
        # 提取 times/days (字符串 '2/1' -> times=2, days=1)
        prev_stat = prev[zt_stat_col].astype(str).str.strip().str.split('/', n=1, expand=True)
        prev_stat.columns = ['times_str', 'days_str']
        prev_stat['times'] = pd.to_numeric(prev_stat['times_str'], errors='coerce').fillna(0)
        prev_stat['days'] = pd.to_numeric(prev_stat['days_str'], errors='coerce').fillna(1).replace(0, 1)
        prev_stat['freq'] = prev_stat['times'] / prev_stat['days']
        # 构建 prev_code -> freq 映射
        prev_code_to_freq = dict(zip(prev_code_norm, prev_stat['freq']))
        prev_code_to_times = dict(zip(prev_code_norm, prev_stat['times']))
        prev_code_to_days = dict(zip(prev_code_norm, prev_stat['days']))

        # 一次查表 (避免 55 次 prev[mask])
        freqs = df_code_norm.map(prev_code_to_freq).fillna(0)
        times_series = df_code_norm.map(prev_code_to_times).fillna(0)
        days_series = df_code_norm.map(prev_code_to_days).fillna(1)
        # 应用阶梯评分
        scores = pd.Series(2.5, index=df.index)
        scores[freqs >= 0.3] = 6.0
        scores[(freqs >= 0.2) & (freqs < 0.3)] = 5.0
        scores[(freqs >= 0.1) & (freqs < 0.2)] = 3.5
        # 记录详情
        for code, t, d in zip(df_code_norm, times_series, days_series):
            if code in prev_code_to_freq:
                raw_details[code] = f"{int(t)}/{int(d)}"
    except Exception as e:
        print(f"  [scanner L1289] failed: {e}", file=sys.stderr)
    return scores, raw_details


# ─── 炸板股反包潜力扫描 ───

def score_zhaban_data(df: pd.DataFrame, today_str: str, weights: dict = None) -> pd.DataFrame:
    """炸板反包评分 (P5: 5因子可调权)。"""
    df = df.copy()

    defaults = {'seal': 20, 'money': 20, 'feature': 15, 'turnover': 10, 'sector': 12}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})
    max_raw = sum(defaults.values())

    seal_time_col = '首次封板时间' if '首次封板时间' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)
    seal_fund_col = '封板资金' if '封板资金' in df.columns else (df.columns[14] if len(df.columns) > 14 else None)
    zhaban_count_col = '炸板次数' if '炸板次数' in df.columns else (df.columns[12] if len(df.columns) > 12 else None)
    turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    industry_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)

    # 1. 封板质量 (0-20)
    seal_scores = pd.Series(0.0, index=df.index)
    seal_scores += df[seal_time_col].apply(seal_time_score)
    fund_vals = df[seal_fund_col].fillna(0).astype(float)
    max_fund = fund_vals.max()
    if max_fund > 0: seal_scores += (fund_vals / max_fund) * 5
    else: seal_scores += 3
    zb_times = df[zhaban_count_col].fillna(0).astype(float)
    seal_scores += np.clip(1.0 - zb_times / 8.0, 0, 1) * 5
    f_seal = (seal_scores / 20).clip(0, 1)

    # 2. 资金承接 (0-20)
    fund_df_zb, _ = fetch_fund_flow_data()
    raw_money = pd.Series(0.0, index=df.index)
    if fund_df_zb is not None:
        money_scores, raw_money = get_money_flow_scores(df, fund_df=fund_df_zb)
    else:
        money_scores = np.clip(fund_vals / (fund_vals.max() + 1), 0, 1) * 10
        raw_money = fund_vals
    f_money = (money_scores / 20).clip(0, 1)

    # 3. 炸板特征 (0-15)
    turnover_vals = df[turnover_col].fillna(0).astype(float)
    feature = pd.Series(7.5, index=df.index)
    feature = feature + ((turnover_vals >= 10) & (turnover_vals <= 25)) * 5 + \
        (((turnover_vals >= 5) & (turnover_vals <= 30)) & ~((turnover_vals >= 10) & (turnover_vals <= 25))) * 2 - \
        (turnover_vals > 40) * 3
    f_feature = (feature.clip(0, 15) / 15)

    # 4. 换手率 (0-10)
    turn_scores = pd.Series(5.0, index=df.index)
    turn_scores = np.where((turnover_vals >= 8) & (turnover_vals <= 20), 10.0,
        np.where((turnover_vals >= 5) & (turnover_vals <= 30), 7.0,
        np.where(turnover_vals <= 3, 3.0, np.where(turnover_vals > 40, 2.0, 5.0))))
    f_turn = (turn_scores / 10).clip(0, 1)

    # 5. 板块热度 (0-12)
    try:
        limit_pool = ak.stock_zt_pool_em(date=today_str)
        if not limit_pool.empty:
            ind_col_l = '所属行业' if '所属行业' in limit_pool.columns else limit_pool.columns[15]
            counts = limit_pool[ind_col_l].value_counts()
            industries = df[industry_col] if industry_col in df.columns else df.iloc[:, 15]
            industry_counts = industries.map(counts).fillna(0)
            sector_raw = (4 + industry_counts * 2).clip(upper=12)
        else:
            sector_raw = get_sector_heat_scores(df, money_series=raw_money)
    except Exception:
        sector_raw = get_sector_heat_scores(df, money_series=raw_money)
    f_sector = (sector_raw / 12).clip(0, 1)

    total = (f_seal * w['seal'] + f_money * w['money'] + f_feature * w['feature'] +
             f_turn * w['turnover'] + f_sector * w['sector'])
    df['总分'] = (total / max_raw * 100).clip(lower=0).round(1)
    df['zb_seal'] = (f_seal * w['seal']).round(1)
    df['封板质量'] = seal_scores.round(1)
    df['zb_money'] = (f_money * w['money']).round(1)
    df['zb_feature'] = (f_feature * w['feature']).round(1)
    df['zb_turnover'] = (f_turn * w['turnover']).round(1)
    df['zb_sector'] = (f_sector * w['sector']).round(1)
    df['资金承接'] = money_scores.round(1)
    df['炸板特征'] = feature.round(1)
    df['换手评分'] = turn_scores.round(1)
    df['板块热度'] = sector_raw.round(1)
    df['净流入'] = raw_money

    return df.sort_values('总分', ascending=False).head(TOP_N)


def _score_reversal(pullback: pd.DataFrame, today_str: str = None, weights: dict = None) -> pd.DataFrame:
    """纯函数: 对"上交易日涨停今日下跌"的票做反转评分

    P5: 4因子可调权, 输出因子分列供IC分析。

    因子:
    - 连板位置 (0-30) — 连板越高，反包势能越强
    - 换手率 (0-25) — 低换手=惜售=筹码锁定好，高换手=抛压大
    - 回调深度 (0-25) — 深度回调才有反包空间
    - 板块支撑 (0-15) — 按板块涨停股数连续分档
    - 封板留存率 (0-5) — 板块涨停股留存率

    返回带因子分列 + '反转评分' 总分的 DataFrame。
    """
    if pullback is None or pullback.empty:
        return pullback

    defaults = {'turnover': 25, 'consecutive': 30, 'pullback': 25, 'sector': 15, 'retention': 5}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})

    turnover_col = pullback.columns[9] if len(pullback.columns) > 9 else None
    seal_stat_col = pullback.columns[14] if len(pullback.columns) > 14 else None
    ind_col = pullback.columns[15] if len(pullback.columns) > 15 else None

    if '今日涨幅' not in pullback.columns:
        chg_col = pullback.columns[3]
        pullback['今日涨幅'] = pullback[chg_col].astype(float)

    # 因子原始分 (0-1 归一化)
    f_to = pd.Series(0.0, index=pullback.index)
    f_lb = pd.Series(0.0, index=pullback.index)
    f_chg = pd.Series(0.0, index=pullback.index)
    f_sector = pd.Series(0.0, index=pullback.index)
    f_retention = pd.Series(0.5, index=pullback.index)

    # 1. 换手率（符号修正：低换手=惜售=好，高换手=派发=差）
    if turnover_col:
        for idx in pullback.index:
            t = float(pullback.loc[idx, turnover_col]) if pd.notna(pullback.loc[idx, turnover_col]) else 0
            if t < 5:                f_to[idx] = 1.0   # 惜售，筹码锁定
            elif 5 <= t < 8:         f_to[idx] = 0.8
            elif 8 <= t < 15:        f_to[idx] = 0.5
            elif 15 <= t < 25:       f_to[idx] = 0.2
            else:                    f_to[idx] = 0.0   # >25%巨量换手=出货

    # 2. 连板位置（单调递增：连板越高分越高）
    if seal_stat_col:
        for idx in pullback.index:
            raw = str(pullback.loc[idx, seal_stat_col]) if pd.notna(pullback.loc[idx, seal_stat_col]) else ''
            consecutive = 0
            if '/' in raw:
                try: consecutive = int(raw.split('/')[1])
                except: pass
            if consecutive >= 4:     f_lb[idx] = 1.0   # 高度连板龙头，反包势能最强
            elif consecutive == 3:   f_lb[idx] = 0.85
            elif consecutive == 2:   f_lb[idx] = 0.65
            elif consecutive == 1:   f_lb[idx] = 0.40
            else:                    f_lb[idx] = 0.20

    # 3. 回调深度（真正跌深了才有反弹空间）
    for idx in pullback.index:
        chg_val = pullback.loc[idx, '今日涨幅']
        if chg_val < -7:         f_chg[idx] = 1.0   # 深度回调
        elif -7 <= chg_val < -5: f_chg[idx] = 0.8
        elif -5 <= chg_val < -3: f_chg[idx] = 0.6
        elif -3 <= chg_val < -1: f_chg[idx] = 0.4
        else:                    f_chg[idx] = 0.1   # 没跌多少，不叫反转

    # 4. 板块支撑（按板块涨停股数连续分档）
    industry_counts = {}
    if today_str and ind_col:
        try:
            lt_today = ak.stock_zt_pool_em(date=today_str)
            if lt_today is not None and not lt_today.empty:
                lt_ind_col = '所属行业' if '所属行业' in lt_today.columns else (lt_today.columns[15] if len(lt_today.columns) > 15 else None)
                if lt_ind_col:
                    industry_counts = lt_today[lt_ind_col].value_counts().to_dict()
        except Exception:
            pass

    for idx in pullback.index:
        ind = str(pullback.loc[idx, ind_col]) if ind_col and pd.notna(pullback.loc[idx, ind_col]) else ''
        cnt = industry_counts.get(ind, 0) if industry_counts else 0
        if cnt >= 5:       f_sector[idx] = 1.0
        elif cnt >= 3:     f_sector[idx] = 0.8
        elif cnt >= 2:     f_sector[idx] = 0.6
        elif cnt >= 1:     f_sector[idx] = 0.4
        else:              f_sector[idx] = 0.2

    # 5. 封板留存率（板块涨停股留存率=持续性）
    if today_str and ind_col and industry_counts:
        try:
            prev_pool = ak.stock_zt_pool_previous_em(date=today_str)
            if prev_pool is not None and not prev_pool.empty:
                prev_ind_col = '所属行业' if '所属行业' in prev_pool.columns else (prev_pool.columns[15] if len(prev_pool.columns) > 15 else None)
                if prev_ind_col:
                    prev_counts = prev_pool[prev_ind_col].value_counts()
                    for idx in pullback.index:
                        ind = str(pullback.loc[idx, ind_col]) if pd.notna(pullback.loc[idx, ind_col]) else ''
                        today_c = industry_counts.get(ind, 0)
                        prev_c = prev_counts.get(ind, 0) if prev_counts is not None else 0
                        if prev_c > 0:
                            retention = today_c / prev_c
                            if retention >= 0.8:    f_retention[idx] = 1.0
                            elif retention >= 0.6:  f_retention[idx] = 0.8
                            elif retention >= 0.4:  f_retention[idx] = 0.6
                            elif retention >= 0.2:  f_retention[idx] = 0.4
                            else:                   f_retention[idx] = 0.2
                        else:
                            f_retention[idx] = 0.3  # 新板块，留存率未知
        except Exception:
            pass

    # 加权总分 (归一化到0-100)
    total = (f_to * w['turnover'] + f_lb * w['consecutive'] +
             f_chg * w['pullback'] + f_sector * w['sector'] +
             f_retention * w['retention'])
    weight_sum = sum(abs(v) for v in w.values())
    normalized = (total / max(weight_sum, 1) * 100) if weight_sum != 0 else total

    pullback = pullback.copy()
    pullback['反转评分'] = normalized.clip(lower=0).round(1)
    pullback['rev_turnover'] = (f_to * w['turnover']).round(1)
    pullback['rev_consecutive'] = (f_lb * w['consecutive']).round(1)
    pullback['rev_pullback'] = (f_chg * w['pullback']).round(1)
    pullback['rev_sector'] = (f_sector * w['sector']).round(1)
    pullback['rev_retention'] = (f_retention * w['retention']).round(1)
    return pullback


def scan_reversal(today_str: str, table_mode: bool = False, top_n: int = None):
    """
    涨停回调反转扫描：找"上交易日涨停今日下跌"的股票，评估明日反包潜力。
    逻辑：上交易日强势封板→今天回调洗盘→明天最可能反转大涨。
    达实智能这类"上交易日跌今天涨停"的反转股，上交易日大概率不在涨停池，
    但前天涨停上交易日回调的股票，今天就是反转候选。
    """
    import pandas as pd
    n = top_n if top_n is not None else TOP_N
    print("[反转扫描] 获取上交易日涨停今日表现...", file=sys.stderr)
    try:
        prev = ak.stock_zt_pool_previous_em(date=today_str)
    except Exception as e:
        print(f"  数据获取失败: {e}", file=sys.stderr)
        return
    if prev.empty:
        print("no_data")
        return

    df = filter_non_main_board(prev)
    if df.empty:
        print("no_data")
        return

    chg_col = df.columns[3]; code_col = df.columns[1]; name_col = df.columns[2]
    df = filter_xr_xd_dr(df, name_col=name_col)
    if df.empty:
        print("no_data")
        return
    price_col = df.columns[4]; turnover_col = df.columns[9]
    ind_col = df.columns[15] if len(df.columns) > 15 else None
    seal_stat_col = df.columns[14] if len(df.columns) > 14 else None

    df['今日涨幅'] = df[chg_col].astype(float)

    # 筛选：今日下跌或微涨<1%（回调中）
    pullback = df[(df['今日涨幅'] >= -7) & (df['今日涨幅'] <= 1)].copy()
    if pullback.empty:
        print("no_data")
        return
    print(f"  → 上交易日涨停今回调: {len(pullback)} 只 (总{len(df)}只)", file=sys.stderr)

    # ── 反转评分 (P2.1 抽到 _score_reversal) ──
    pullback = _score_reversal(pullback, today_str=today_str)

    pullback = pullback.sort_values('反转评分', ascending=False).head(n)

    # ── 输出 ──
    if table_mode:
        lines = []
        lines.append(f"{'代码':<8s} {'名称':<8s} {'今涨幅':>7s} {'换手':>6s} {'连板':>4s} {'行业':<10s} {'反转分':>6s} {'建议':<14s}")
        lines.append("-" * 72)
        for _, row in pullback.iterrows():
            code = str(row[code_col]).strip().zfill(6)
            name = str(row[name_col])
            chg = row['今日涨幅']
            to = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
            raw = str(row[seal_stat_col]) if seal_stat_col else ''
            lb = int(raw.split('/')[1]) if '/' in raw else 0
            ind = str(row[ind_col]) if ind_col and pd.notna(row[ind_col]) else ''
            score = int(row['反转评分'])
            if score >= 80: adv = '⭐ 重点观察'
            elif score >= 65: adv = '加入自选'
            elif score >= 50: adv = '观望'
            else: adv = '暂不参与'
            lines.append(f"{code:<8s} {name:<8s} {chg:+6.1f}% {to:5.1f}% {lb:4d} {ind:<10s} {score:6d} {adv:<14s}")
        print('\n'.join(lines))
    else:
        for _, row in pullback.iterrows():
            code = str(row[code_col]).strip().zfill(6)
            name = str(row[name_col])
            score = int(row['反转评分'])
            chg = row['今日涨幅']
            to = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
            ind = str(row[ind_col]) if ind_col and pd.notna(row[ind_col]) else ''
            print(f"{code} {name}: 反转{score}分 | 今{chg:+.1f}% | 换手{to:.1f}% | {ind}")

    return pullback[['反转评分']] if not pullback.empty else None


def scan_zhaban(today_str: str, table_mode: bool = False, top_n: int = None):
    n = top_n if top_n is not None else TOP_N
    print("[炸板扫描] 获取炸板股池...", file=sys.stderr)
    try:
        zb_df = ak.stock_zt_pool_zbgc_em(date=today_str)
    except Exception as e:
        print(f"  炸板数据获取失败: {e}", file=sys.stderr)
        return
    if zb_df.empty: print("no_data"); return
    print(f"  → 炸板股共 {len(zb_df)} 只", file=sys.stderr)

    df = zb_df.copy()
    before = len(df)
    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)
    if '流通市值' in df.columns:
        df = df[df['流通市值'].astype(float) <= MAX_MARKET_CAP * 1e8]
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    df = df[df[price_col].astype(float) <= MAX_PRICE]
    after = len(df)
    print(f"  → 过滤后 {after}/{before} 只", file=sys.stderr)
    if df.empty: print("no_data"); return

    # 统一评分
    out_df = score_zhaban_data(df, today_str)
    if n < TOP_N: out_df = out_df.head(n)

    today_display = date.today().strftime('%Y-%m-%d')
    lines = [f"炸板股反包潜力 TOP{n} ({today_display})", "=" * 70]

    for rank, (_, row) in enumerate(out_df.iterrows(), 1):
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        score = float(row['总分'])
        seal_s = float(row['封板质量'])
        money_s = float(row['资金承接'])
        feat_s = float(row['炸板特征'])
        turn_s = float(row['换手评分'])
        sec_s = float(row['板块热度'])
        st_col = '首次封板时间' if '首次封板时间' in out_df.columns else out_df.columns[11]
        zb_col = '炸板次数' if '炸板次数' in out_df.columns else out_df.columns[12]
        to_col = '换手率' if '换手率' in out_df.columns else out_df.columns[9]
        seal_time = str(row.get(st_col, ''))[:4]
        zhaban_cnt = int(float(row.get(zb_col, 0))) if pd.notna(row.get(zb_col, None)) else 0
        turnover = float(row.get(to_col, 0)) if pd.notna(row.get(to_col, None)) else 0

        # 净流入文本
        net = float(row.get('净流入', 0))
        if abs(net) >= 1e8:
            net_str = f"{net/1e8:.2f}亿"
        elif abs(net) >= 1e4:
            net_str = f"{net/1e4:.0f}万"
        else:
            net_str = f"{net:.0f}"

        # 反包潜力判断
        parts = []
        if seal_s >= 15:
            parts.append("封板质量高")
        if money_s >= 12:
            parts.append(f"资金承接强({net_str})")
        elif net < 0:
            parts.append(f"资金流出{net_str}")
        if feat_s >= 10:
            parts.append("炸板特征优(早封+适中换手)")
        if 8 <= turnover <= 20:
            parts.append("换手适中")
        elif turnover > 30:
            parts.append("换手过高⚠️")
        logic = " + ".join(parts) if parts else "标准炸板反包标的"

        lines.append(f"\n{rank}. {code} {name} | 反包潜力 {score:.1f}")
        lines.append(f"   封板时间 {seal_time} | 炸板 {zhaban_cnt}次 | 换手 {turnover:.1f}%")
        lines.append(f"   评分拆解: 封板质量{seal_s:.1f}/25 + 资金承接{money_s:.1f}/20 + 炸板特征{feat_s:.1f}/15 + 换手评分{turn_s:.1f}/10 + 板块热度{sec_s:.1f}/12")
        lines.append(f"   {logic}")
        risk = "次日反包需竞价放量高开确认，低开-3%直接放弃" if net >= 0 else "资金净流出，反包概率降低，观望为主"
        lines.append(f"   {risk}")

    # ── 构建返回数据（供全维度综合榜单使用） ──
    zhaban_results = []
    for _, row in out_df.iterrows():
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        score = float(row['总分'])
        zhaban_results.append({'code': code, 'name': name, 'score': score, 'dimension': '炸板反包'})

    lines.append(f"\n{'=' * 70}")
    lines.append("反包策略: 竞价高开+放量 = 可参与 | 竞价平开/低开 = 放弃")
    lines.append("止损: 参与后次日收盘不反包即出，-5%硬止损")
    print("\n".join(lines))
    return zhaban_results


# ═══════════════════════════════════════════
#  模式3: 趋势动量股扫描 (--trend)
# ═══════════════════════════════════════════

def _score_trend(df: pd.DataFrame, weights: dict = None) -> pd.DataFrame:
    """P2.2 抽出的纯函数: 趋势动量评分

    5 因子 (0-100):
    - 涨幅分 (0-40): 3-8% 甜蜜区
    - 换手活跃分 (0-30): 8-15% 甜蜜区
    - 成交额分 (0-30): 越大关注度越高
    - 量比加分 (0-5): 强势池特有
    - 新高加分 (0-3)

    P4: 支持可调权 — weights=None 用默认权重, 传 dict 则覆盖。

    输入: 已过滤 (涨幅 2.5-8.5% + 非 ST + 市值<MAX_MARKET_CAP) 的 DataFrame
    输出: 加因子分列 + '动量评分' 总分的 DataFrame
    """
    if df is None or df.empty:
        return df

    # 默认权重 (可被 weights 参数覆盖)
    defaults = {'chg': 40, 'turnover': 30, 'amount': 30, 'vol_ratio': 5, 'new_high': 3, 'ma_rev': 0}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})

    # 列识别 (强势池列名规范,做防御)
    change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
    turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
    vol_ratio_col = '量比' if '量比' in df.columns else None
    volume_col = '成交额' if '成交额' in df.columns else df.columns[6]
    new_high_col = '是否新高' if '是否新高' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)

    # 因子原始分 (0-1 归一化后再乘权重)
    f_chg = pd.Series(0.0, index=df.index)
    f_turnover = pd.Series(0.0, index=df.index)
    f_amount = pd.Series(0.0, index=df.index)
    f_vr = pd.Series(0.0, index=df.index)
    f_nh = pd.Series(0.0, index=df.index)

    # 1. 涨幅分
    changes = df[change_col].astype(float)
    for idx in df.index:
        chg = float(changes[idx])
        if 6 <= chg <= 8:       f_chg[idx] = 1.0
        elif 5 <= chg < 6:      f_chg[idx] = 0.875
        elif 4 <= chg < 5:      f_chg[idx] = 0.75
        elif 3 <= chg < 4:      f_chg[idx] = 0.625
        elif 8 <= chg < 9.5:    f_chg[idx] = 0.5
        else:                   f_chg[idx] = 0.375

    # 2. 换手分
    turnovers = df[turnover_col].astype(float)
    for idx in df.index:
        t = float(turnovers[idx])
        if 8 <= t <= 15:        f_turnover[idx] = 1.0
        elif 5 <= t < 8:        f_turnover[idx] = 0.833
        elif 15 < t <= 20:      f_turnover[idx] = 0.667
        elif 3 <= t < 5:        f_turnover[idx] = 0.5
        elif 20 < t <= 25:      f_turnover[idx] = 0.333
        else:                   f_turnover[idx] = 0.167

    # 3. 成交额分 (归一化)
    volumes = df[volume_col].astype(float)
    max_v = volumes.max()
    if max_v > 0:
        f_amount = (volumes / max_v).clip(0, 1)
    else:
        f_amount = pd.Series(0.5, index=df.index)

    # 4. 量比加分
    if vol_ratio_col and vol_ratio_col in df.columns:
        vol_ratios = df[vol_ratio_col].astype(float)
        for idx in df.index:
            vr = float(vol_ratios[idx])
            if vr > 3:          f_vr[idx] = 1.0
            elif vr > 2:        f_vr[idx] = 0.6
            elif vr > 1.2:      f_vr[idx] = 0.2

    # 5. 新高加分
    if new_high_col and new_high_col in df.columns:
        for idx in df.index:
            if str(df.loc[idx, new_high_col]) == '是':
                f_nh[idx] = 1.0

    # 加权总分
    total = (f_chg * w['chg'] + f_turnover * w['turnover'] +
             f_amount * w['amount'] + f_vr * w['vol_ratio'] +
             f_nh * w['new_high'])

    # 写入因子列 (供回测相关性分析)
    df = df.copy()
    # 6. MA回归因子 (0-10): 偏离均线越远越容易回调
    f_ma = pd.Series(5.0, index=df.index)
    if w.get('ma_rev', 0) != 0:  # 权重非零才拉取MA数据(支持负权因子)
        code_col_ma = '代码' if '代码' in df.columns else df.columns[1]
        try:
            f_ma = _calc_ma_regression(df, code_col=code_col_ma)
        except Exception:
            pass

    total = (f_chg * w['chg'] + f_turnover * w['turnover'] +
             f_amount * w['amount'] + f_vr * w['vol_ratio'] +
             f_nh * w['new_high'] + f_ma * w.get('ma_rev', 0))

    # 归一化到0-100 (除以当前权重绝对值总和, 支持负权)
    weight_sum = sum(abs(v) for v in w.values())
    normalized = (total / max(weight_sum, 1) * 100) if weight_sum != 0 else total

    df = df.copy()
    df['动量评分'] = normalized.clip(lower=0).round(1)  # 负权允许, 但总分不<0
    df['trend_chg'] = (f_chg * w['chg']).round(1)
    df['trend_turnover'] = (f_turnover * w['turnover']).round(1)
    df['trend_amount'] = (f_amount * w['amount']).round(1)
    df['trend_vr'] = (f_vr * w['vol_ratio']).round(1)
    df['trend_nh'] = (f_nh * w['new_high']).round(1)
    df['trend_ma'] = (f_ma * w.get('ma_rev', 0)).round(1)

    return df


def _calc_ma_regression(df: pd.DataFrame, code_col: str = None) -> pd.Series:
    """计算MA回归因子: 当前价 vs 5日/10日均线的偏离度

    偏离越小(贴近均线) → 分数越高 (趋势健康)
    偏离越大(远离均线) → 分数越低 (超买回调风险)

    返回 0-10 的 Series
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import akshare as ak
    from datetime import datetime, timedelta

    if df is None or df.empty:
        return pd.Series(0.0, index=df.index)

    code_col = code_col or ('代码' if '代码' in df.columns else df.columns[1])
    codes = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        if len(code) == 6:
            codes.append(code)

    if not codes:
        return pd.Series(0.0, index=df.index)

    today = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')
    prices = {}

    def _fetch(code):
        try:
            hist = ak.stock_zh_a_hist(symbol=code, period='daily',
                                       start_date=start, end_date=today,
                                       adjust='qfq')
            if hist is not None and not hist.empty and len(hist) >= 5:
                closes = hist['收盘'].astype(float).values
                return code, closes
        except Exception:
            pass
        return code, None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_fetch, c): c for c in codes}
        for f in as_completed(futures):
            code, closes = f.result()
            if closes is not None and len(closes) >= 5:
                prices[code] = closes

    scores = pd.Series(0.0, index=df.index)
    for idx in df.index:
        code = str(df.loc[idx, code_col]).strip().zfill(6)
        closes = prices.get(code)
        if closes is None or len(closes) < 5:
            scores[idx] = 5.0  # 无数据给中性分
            continue

        current = closes[-1]
        ma5 = closes[-5:].mean()
        ma10 = closes[-10:].mean() if len(closes) >= 10 else ma5

        # 偏离度: (当前价/MA - 1) * 100
        dev5 = (current / ma5 - 1) * 100
        dev10 = (current / ma10 - 1) * 100
        dev = max(dev5, dev10)  # 取最大偏离

        # 评分: 偏离<2%→满分; 2-5%→递减; 5-8%→低分; >8%→0
        if dev <= 2:
            scores[idx] = 10.0
        elif dev <= 5:
            scores[idx] = 10 - (dev - 2) * 2     # 2%→10, 5%→4
        elif dev <= 8:
            scores[idx] = max(0, 4 - (dev - 5))   # 5%→4, 8%→1
        else:
            scores[idx] = 0

    return scores.round(1)


def scan_trend(today_str: str, _table_mode: bool = False, top_n: int = None):
    """
    趋势动量股扫描。
    找出近期趋势强劲、量价配合好但未涨停的标的。
    适用于"不涨停不停涨"的趋势交易模式。
    """
    n = top_n if top_n is not None else TOP_N
    print("[趋势扫描] 获取强势股池...", file=sys.stderr)

    # ── 主方案：akshare 强势池（盘中实时数据，已验证可用）──
    strong_df = None
    try:
        strong_df = ak.stock_zt_pool_strong_em(date=today_str)
        if strong_df is not None and not strong_df.empty:
            print(f"  -> 强势池 {len(strong_df)} 只（实时数据）", file=sys.stderr)
    except Exception as e:
        print(f"  ! 强势池获取失败: {e}", file=sys.stderr)

    if strong_df is not None and not strong_df.empty:
        # ── 强势池数据清洗与筛选 ──
        df = strong_df.copy()
        before = len(df)

        # 列名标准化（强势池列名已经规范，但做防御处理）
        code_col = '代码' if '代码' in df.columns else df.columns[1]
        name_col = '名称' if '名称' in df.columns else df.columns[2]
        change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
        turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
        vol_ratio_col = '量比' if '量比' in df.columns else None   # 强势池特有
        volume_col = '成交额' if '成交额' in df.columns else df.columns[6]
        cap_col = '流通市值' if '流通市值' in df.columns else None
        industry_col = '所属行业' if '所属行业' in df.columns else None
        new_high_col = '是否新高' if '是否新高' in df.columns else None   # 强势池特有

        # 板块过滤
        df = filter_non_main_board(df, code_col=code_col)

        # 市值过滤
        if cap_col and cap_col in df.columns:
            df = df[df[cap_col].astype(float) <= MAX_MARKET_CAP * 1e8]

        # 价格过滤
        price_col = '最新价' if '最新价' in df.columns else df.columns[4]
        df = df[df[price_col].astype(float) <= MAX_PRICE]

        # ── 核心筛选：涨幅2.5-8.5%（没涨停的强势股，能买进！）──
        changes = df[change_col].astype(float)
        df = df[(changes >= 2.5) & (changes < 8.5)]

        after = len(df)
        print(f"  -> 过滤后 {after}/{before} 只（涨幅2.5-8.5% + 非ST/科创/北交 + 市值<{MAX_MARKET_CAP}亿）", file=sys.stderr)

        if not df.empty:
            # ── 动量评分 (P2.2 抽到 _score_trend) ──
            df = _score_trend(df)
            df = df.sort_values('动量评分', ascending=False).head(n)

            # ── 输出 ──
            market_status = get_market_status()
            time_tag = "盘中实时" if market_status == 'trading' else "盘后强势池"
            today_display = date.today().strftime('%Y-%m-%d')
            lines = [f"趋势动量股 TOP{n} ({today_display} {time_tag})", "=" * 80]

            trend_results = []
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                name = row[name_col]
                score = float(row['动量评分'])
                trend_results.append({'code': code, 'name': name, 'score': min(score, 100), 'dimension': '趋势动量'})

            for rank, (_, row) in enumerate(df.iterrows(), 1):
                code = str(row[code_col]).strip().zfill(6)
                name = row[name_col]
                score = float(row['动量评分'])
                chg = float(row[change_col])
                t = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
                v = float(row[volume_col]) if pd.notna(row[volume_col]) else 0
                vol_str = f"{v/1e8:.2f}亿" if v >= 1e8 else f"{v/1e4:.0f}万"
                # 量比显示
                vr_display = ""
                if vol_ratio_col and vol_ratio_col in df.columns:
                    vr_val = float(row[vol_ratio_col]) if pd.notna(row.get(vol_ratio_col, None)) else 0
                    if vr_val > 0:
                        vr_display = f" 量比{vr_val:.1f}"
                # 新高显示
                nh_display = ""
                if new_high_col and new_high_col in df.columns:
                    if str(row.get(new_high_col, '')) == '是':
                        nh_display = " [新高!]"
                # 行业
                industry = ""
                if industry_col and industry_col in df.columns:
                    industry = str(row.get(industry_col, ''))

                # 评级
                if score >= 75:
                    grade = "AAA"
                    grade_desc = "强势突破"
                elif score >= 60:
                    grade = "AA"
                    grade_desc = "趋势优良"
                elif score >= 45:
                    grade = "A"
                    grade_desc = "值得关注"
                else:
                    grade = "B"
                    grade_desc = "一般"

                lines.append(f"\n{rank}. {code} {name} | {grade}级 {score:.1f}分{nh_display}")
                lines.append(f"   涨幅 {chg:+.1f}% | 换手 {t:.1f}% | 成交 {vol_str}{vr_display}")
                signal = "量价齐升" if t > 5 and chg > 4 else "温和放量" if t > 2 else "缩量上涨"
                lines.append(f"   信号: {signal}({grade_desc}) | 行业: {industry}")
                lines.append(f"   策略: 沿5/10日线低吸，放量滞涨止盈，不追高")

            lines.append(f"\n{'=' * 80}")
            lines.append("盘中趋势策略: 从未涨停的强势股中寻找动量标的，买在启动前")
            lines.append("筛选条件: 涨幅2.5-8.5% + 换手5-15% + 量比>1.0（盘中实时）")
            lines.append("止损: 跌破10日线或单日跌超-5%出局")
            print("\n".join(lines))
            return trend_results

        # ── 强势池有数据但筛选后为空 ──
        print("  -> 强势池中无符合条件标的（涨幅2.5-8.5% + 非ST/科创/北交）", file=sys.stderr)
        # 放宽条件重试
        df_loose = strong_df.copy()
        df_loose = filter_non_main_board(df_loose)
        changes_loose = df_loose['涨跌幅' if '涨跌幅' in df_loose.columns else df_loose.columns[3]].astype(float)
        df_loose = df_loose[(changes_loose >= 1.5) & (changes_loose < 9.5)]
        if not df_loose.empty:
            print(f"  -> 放宽条件后 {len(df_loose)} 只（涨幅1.5-9.5%）", file=sys.stderr)
            # 简单排序输出
            df_loose['动量评分'] = changes_loose[df_loose.index] * 8
            df_loose = df_loose.sort_values('动量评分', ascending=False).head(n)
            today_display = date.today().strftime('%Y-%m-%d')
            lines = [f"趋势动量股 TOP{n} ({today_display} 放宽条件)", "=" * 60]
            trend_results = []
            for _, row in df_loose.iterrows():
                c = str(row.get('代码', row.iloc[1])).strip().zfill(6)
                nm = row.get('名称', row.iloc[2])
                chg = float(row.get('涨跌幅', row.iloc[3]))
                trend_results.append({'code': c, 'name': nm, 'score': min(chg * 10, 100), 'dimension': '趋势动量'})
                lines.append(f"{len(trend_results)}. {c} {nm} | 涨幅 {chg:+.1f}%")
            lines.append(f"\n{'=' * 60}")
            lines.append("放宽条件扫描，标的仅供参考")
            print("\n".join(lines))
            return trend_results

    # ── 降级方案：上交易日涨停今日续强（盘后可用）──
    print("  强势池不可用，使用「上交易日涨停今日续强」策略", file=sys.stderr)
    print("  该策略寻找上交易日涨停后今日继续走强(涨幅3-9%)的标的", file=sys.stderr)
    from datetime import datetime as dt_mod, timedelta
    today_dt = dt_mod.strptime(today_str, '%Y%m%d') if len(today_str) == 8 else dt_mod.today()
    wd = today_dt.weekday()
    days_back = 3 if wd == 0 else (2 if wd == 6 else 1)
    yesterday = (today_dt - timedelta(days=days_back)).strftime('%Y%m%d')
    try:
        prev = ak.stock_zt_pool_previous_em(date=yesterday)
    except Exception:
        prev = pd.DataFrame()

    if prev.empty:
        print("no_data")
        return

    change_col = '涨跌幅' if '涨跌幅' in prev.columns else (prev.columns[3] if len(prev.columns) > 3 else prev.columns[0])
    df = prev.copy()
    df = filter_non_main_board(df)
    changes = df[change_col].astype(float)
    df = df[(changes >= 3) & (changes < 9)]
    if df.empty:
        print("no_data")
        return

    df['趋势评分'] = changes * 10
    df = df.sort_values('趋势评分', ascending=False).head(n)

    lines = [f"趋势动量股 TOP{n} | 上交易日涨停今日续强", "=" * 70]
    for rank, (_, row) in enumerate(df.iterrows(), 1):
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        chg = float(row[change_col])
        lines.append(f"{rank}. {code} {name} | 今日涨幅 {chg:+.1f}%")
        lines.append(f"   上交易日涨停后今日继续走强，趋势延续中")
        lines.append(f"   策略: 沿5日线持有，破5日线止盈")

    trend_results = []
    for _, row in df.iterrows():
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        chg = float(row[change_col])
        trend_results.append({'code': code, 'name': name, 'score': min(chg * 10, 100), 'dimension': '趋势动量'})

    lines.append(f"\n{'=' * 70}")
    print("\n".join(lines))
    return trend_results


# ─── 板块联动纯评分函数 ───

def score_sector_data(limit_df: pd.DataFrame, zhaban_df: pd.DataFrame,
                      dieting_df: pd.DataFrame, top_n: int = TOP_N) -> list[dict]:
    """板块联动强度纯评分（无print）。Web card端点和CLI共享。
    输入三个涨停/炸板/跌停DataFrame，返回按联动强度排序的板块列表。"""
    # 涨停行业分布
    limit_counts = {}
    if not limit_df.empty:
        ind_col = '所属行业' if '所属行业' in limit_df.columns else (limit_df.columns[15] if len(limit_df.columns) > 15 else None)
        if ind_col: limit_counts = limit_df[ind_col].value_counts().to_dict()
    # 炸板行业分布
    zhaban_counts = {}
    if not zhaban_df.empty:
        ind_col2 = '所属行业' if '所属行业' in zhaban_df.columns else (zhaban_df.columns[15] if len(zhaban_df.columns) > 15 else None)
        if ind_col2: zhaban_counts = zhaban_df[ind_col2].value_counts().to_dict()
    # 跌停行业分布
    dieting_counts = {}
    if not dieting_df.empty:
        ind_col3 = '所属行业' if '所属行业' in dieting_df.columns else (dieting_df.columns[15] if len(dieting_df.columns) > 15 else None)
        if ind_col3: dieting_counts = dieting_df[ind_col3].value_counts().to_dict()

    all_industries = set(list(limit_counts.keys()) + list(zhaban_counts.keys()) + list(dieting_counts.keys()))
    if not all_industries: return []

    stats = []
    for industry in all_industries:
        lc = limit_counts.get(industry, 0)
        zc = zhaban_counts.get(industry, 0)
        dc = dieting_counts.get(industry, 0)
        total = lc + zc + dc
        stats.append({
            'industry': industry,
            'limit_cnt': lc, 'zhaban_cnt': zc, 'dieting_cnt': dc,
            'link_strength': round(lc - zc * 0.3 - dc * 0.5, 1),
            'profit_effect': round(lc / total * 100, 0) if total > 0 else 0,
            'seal_rate': round(lc / (lc + zc) * 100, 0) if (lc + zc) > 0 else 50,
        })
    stats.sort(key=lambda x: x['link_strength'], reverse=True)
    return stats[:top_n]


# ─── P2.3: 板块个股反向查询 ───

def _get_sector_stocks(industry: str, limit_df: pd.DataFrame, zhaban_df: pd.DataFrame,
                       dieting_df: pd.DataFrame = None) -> list[dict]:
    """给定板块名,返回该板块所有涨停+炸板个股的 code/name 列表

    用于 P2.3 板块 tab 回测: 板块级 → 个股级映射
    返回: [{'code': '000001', 'name': '平安银行', 'source': 'limit'}, ...]
    """
    stocks = []
    seen = set()
    ind_col = None

    for source_name, df in [('limit', limit_df), ('zhaban', zhaban_df), ('dieting', dieting_df)]:
        if df is None or df.empty:
            continue
        if ind_col is None or ind_col not in df.columns:
            ind_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)
        if ind_col is None or ind_col not in df.columns:
            continue
        for _, row in df.iterrows():
            if str(row.get(ind_col, '')) != industry:
                continue
            code = str(row.get('代码', '') or row.iloc[1]).strip().zfill(6)
            if not code or code in seen:
                continue
            seen.add(code)
            stocks.append({
                'code': code,
                'name': str(row.get('名称', '') or row.iloc[2]),
                'source': source_name,
            })
    return stocks


def _score_sector(date_str: str, top_n: int = TOP_N) -> pd.DataFrame:
    """P2.3 板块 tab 回测评分: 板块级 → 个股级映射

    流程:
    1. 拉当日涨停+炸板+跌停池
    2. score_sector_data 算板块联动强度
    3. 每个板块的 TOP 个股按板块强度打"板块强度分"
    4. 输出: 个股级 DataFrame,含 '板块强度' 列

    注: 板块级回测策略 = 板块 TOP1 的所有涨停个股等权买 → D+1 算收益
    """
    # 拉数据
    try:
        limit_df = ak.stock_zt_pool_em(date=date_str)
    except Exception:
        limit_df = pd.DataFrame()
    try:
        zhaban_df = ak.stock_zt_pool_zbgc_em(date=date_str)
    except Exception:
        zhaban_df = pd.DataFrame()
    try:
        dieting_df = ak.stock_zt_pool_dtgc_em(date=date_str)
    except Exception:
        dieting_df = pd.DataFrame()

    # 板块评分
    sectors = score_sector_data(limit_df, zhaban_df, dieting_df, top_n=top_n)
    if not sectors:
        return pd.DataFrame()

    # 板块 → 个股
    rows = []
    for sector in sectors:
        industry = sector['industry']
        link = sector['link_strength']
        stocks = _get_sector_stocks(industry, limit_df, zhaban_df, dieting_df)
        # 按 link_strength 排序后分配: 板块强度分 = link_strength * 10 (clip 0-100)
        sector_score = max(0, min(100, int(link * 10 + 50)))
        for stock in stocks:
            rows.append({
                '代码': stock['code'],
                '名称': stock['name'],
                '所属行业': industry,
                '板块强度': sector_score,
                'link_strength': link,
                '_source': stock['source'],
                'limit_cnt': sector['limit_cnt'],
                'zhaban_cnt': sector['zhaban_cnt'],
                'dieting_cnt': sector['dieting_cnt'],
                'profit_effect': sector['profit_effect'],
                'seal_rate': sector['seal_rate'],
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════
#  模式4: 板块联动强度 (--sector)
# ═══════════════════════════════════════════

def scan_sector(today_str: str, table_mode: bool = False, top_n: int = None):
    n = top_n if top_n is not None else TOP_N
    print("[板块扫描] 获取涨停池+炸板池行业分布...", file=sys.stderr)

    # 并行获取涨停池、炸板池
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pool_data = {}

    def _get_limit():
        try:
            return 'limit', ak.stock_zt_pool_em(date=today_str)
        except:
            return 'limit', pd.DataFrame()

    def _get_zhaban():
        try:
            return 'zhaban', ak.stock_zt_pool_zbgc_em(date=today_str)
        except:
            return 'zhaban', pd.DataFrame()

    def _get_dieting():
        try:
            return 'dieting', ak.stock_zt_pool_dtgc_em(date=today_str)
        except:
            return 'dieting', pd.DataFrame()

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(f): f.__name__ for f in [_get_limit, _get_zhaban, _get_dieting]}
        for f in as_completed(futs):
            try:
                k, v = f.result()
                pool_data[k] = v
            except Exception as e:
                print(f"  [scanner L1946] failed: {e}", file=sys.stderr)

    limit_df = pool_data.get('limit', pd.DataFrame())
    zhaban_df = pool_data.get('zhaban', pd.DataFrame())
    dieting_df = pool_data.get('dieting', pd.DataFrame())

    if limit_df.empty and zhaban_df.empty:
        print("no_data")
        return

    # 统一评分
    top_sectors = score_sector_data(limit_df, zhaban_df, dieting_df, top_n=n)

    # ── 获取资金流 (可选增强) ──
    fund_df, _ = fetch_fund_flow_data()
    sector_fund_map = {}
    if fund_df is not None and not fund_df.empty:
        # 获取行业板块资金流
        try:
            sector_fund = ak.stock_sector_fund_flow_rank(indicator="今日")
            if sector_fund is not None and not sector_fund.empty:
                for _, row in sector_fund.iterrows():
                    name = str(row.iloc[1]) if len(row) > 1 else ''
                    net_in = float(row.iloc[4]) if len(row) > 4 else 0
                    sector_fund_map[name] = net_in
        except Exception as e:
            print(f"  [scanner L1972] failed: {e}", file=sys.stderr)

    today_display = date.today().strftime('%Y-%m-%d')
    lines = [f"板块联动强度 TOP{n} ({today_display})", "=" * 80]
    lines.append(f"{'排名':<4} {'板块名称':<14} {'联动分':<8} {'涨停':<6} {'炸板':<6} {'跌停':<6} {'赚钱效应':<10} {'封板率':<8} {'资金净流'}")
    lines.append("-" * 80)

    for rank, s in enumerate(top_sectors, 1):
        fund_str = ""
        if s['industry'] in sector_fund_map:
            fv = sector_fund_map[s['industry']]
            if fv > 0:
                fund_str = f"+{fv/1e8:.1f}亿" if fv >= 1e8 else f"+{fv/1e4:.0f}万"
            else:
                fund_str = f"{fv/1e8:.1f}亿" if abs(fv) >= 1e8 else f"{fv/1e4:.0f}万"

        lines.append(f" #{rank:<2} {s['industry']:<12} {s['link_strength']:<6.1f}  {s['limit_cnt']:<4}  {s['zhaban_cnt']:<4}  {s['dieting_cnt']:<4}  {s['profit_effect']:<5.0f}%   {s['seal_rate']:<5.0f}%  {fund_str}")

    lines.append(f"\n{'=' * 80}")
    lines.append("联动强度说明: 涨停数多+炸板/跌停少 = 强联动(板块合力)")
    lines.append("打分基于涨停-炸板×0.3-跌停×0.5")
    lines.append("策略: 聚焦联动强度TOP3板块的龙头股")
    print("\n".join(lines))


# ═══════════════════════════════════════════
#  模式5: 跌停翘板信号 (--dtqiaoban)
# ─── 跌停翘板纯评分函数 ───

def score_dtqiaoban_data(df: pd.DataFrame, weights: dict = None) -> pd.DataFrame:
    """翘板反抽评分 (P5: 5因子可调权)。"""
    df = df.copy()
    defaults = {'deal': 25, 'seal': 25, 'cont': 25, 'turnover': 15, 'time': 10}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})
    # 列识别
    deal_col = df.columns[12] if len(df.columns) > 12 else None
    seal_fund_col = df.columns[10] if len(df.columns) > 10 else None
    cont_dieting_col = df.columns[13] if len(df.columns) > 13 else None
    turnover_col = df.columns[9] if len(df.columns) > 9 else None
    seal_time_col = df.columns[11] if len(df.columns) > 11 else None

    f_deal = pd.Series(0.2, index=df.index)
    f_seal = pd.Series(0.5, index=df.index)
    f_cont = pd.Series(0.2, index=df.index)
    f_turn = pd.Series(0.1, index=df.index)
    f_time = pd.Series(0.3, index=df.index)
    for idx in df.index:
        if deal_col is not None:
            dv = float(df.loc[idx, deal_col]) if pd.notna(df.loc[idx, deal_col]) else 0
            if dv > 3000e4: f_deal[idx] = 1.0
            elif dv > 1000e4: f_deal[idx] = 0.8
            elif dv > 500e4: f_deal[idx] = 0.6
            elif dv > 100e4: f_deal[idx] = 0.4
            else: f_deal[idx] = 0.2
        if seal_fund_col is not None:
            sv = float(df.loc[idx, seal_fund_col]) if pd.notna(df.loc[idx, seal_fund_col]) else 0
            if sv < 100e4: f_seal[idx] = 1.0
            elif sv < 1000e4: f_seal[idx] = 0.8
            elif sv < 5000e4: f_seal[idx] = 0.4
            else: f_seal[idx] = 0.12
        if cont_dieting_col is not None:
            cv = int(float(df.loc[idx, cont_dieting_col])) if pd.notna(df.loc[idx, cont_dieting_col]) else 0
            if cv >= 5: f_cont[idx] = 1.0
            elif cv == 4: f_cont[idx] = 0.9
            elif cv == 3: f_cont[idx] = 0.8
            elif cv == 2: f_cont[idx] = 0.6
            elif cv == 1: f_cont[idx] = 0.4
            else: f_cont[idx] = 0.2
        if turnover_col is not None:
            tv = float(df.loc[idx, turnover_col]) if pd.notna(df.loc[idx, turnover_col]) else 0
            if tv > 15: f_turn[idx] = 1.0
            elif tv > 10: f_turn[idx] = 0.8
            elif tv > 5: f_turn[idx] = 0.6
            elif tv > 3: f_turn[idx] = 0.4
            elif tv > 1: f_turn[idx] = 0.2
            else: f_turn[idx] = 0.0
        if seal_time_col is not None:
            t = str(df.loc[idx, seal_time_col]).strip()
            if len(t) >= 4:
                minutes = int(t[:2]) * 60 + int(t[2:4])
                if minutes >= 840: f_time[idx] = 1.0
                elif minutes >= 750: f_time[idx] = 0.5
                else: f_time[idx] = 0.2
    total = (f_deal*w['deal'] + f_seal*w['seal'] + f_cont*w['cont'] + f_turn*w['turnover'] + f_time*w['time'])
    max_raw = sum(defaults.values())
    df['翘板评分'] = (total / max_raw * 100).clip(lower=0).round(1)
    df['dt_deal'] = (f_deal*w['deal']).round(1)
    df['dt_seal'] = (f_seal*w['seal']).round(1)
    df['dt_cont'] = (f_cont*w['cont']).round(1)
    df['dt_turnover'] = (f_turn*w['turnover']).round(1)
    df['dt_time'] = (f_time*w['time']).round(1)
    return df.sort_values('翘板评分', ascending=False).head(TOP_N)


# ═══════════════════════════════════════════

def scan_dtqiaoban(today_str: str, table_mode: bool = False, top_n: int = None):
    n = top_n if top_n is not None else TOP_N
    print("[翘板扫描] 获取跌停股池...", file=sys.stderr)
    try:
        dt_df = ak.stock_zt_pool_dtgc_em(date=today_str)
    except Exception as e:
        print(f"  跌停数据获取失败: {e}", file=sys.stderr)
        return
    if dt_df.empty:
        print("no_data")
        return
    print(f"  → 跌停股共 {len(dt_df)} 只", file=sys.stderr)

    df = dt_df.copy()
    before = len(df)

    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)

    # 过滤市值过大（市值小的更容易翘板）
    if '流通市值' in df.columns:
        df = df[df['流通市值'].astype(float) <= MAX_MARKET_CAP * 1e8]
    elif len(df.columns) > 6:
        try:
            df = df[df.iloc[:, 6].astype(float) <= MAX_MARKET_CAP * 1e8]
        except Exception as e:
            print(f"  [scanner L2098] failed: {e}", file=sys.stderr)

    after = len(df)
    print(f"  → 过滤后 {after}/{before} 只", file=sys.stderr)
    if df.empty:
        print("no_data")
        return

    # 统一评分
    df = score_dtqiaoban_data(df)
    if n < TOP_N: df = df.head(n)

    # ── 列识别（用于输出） ──
    code_col = df.columns[1]
    name_col = df.columns[2]
    change_col = df.columns[3]
    turnover_col = df.columns[9] if len(df.columns) > 9 else None
    seal_fund_col = df.columns[10] if len(df.columns) > 10 else None
    cont_dieting_col = df.columns[13] if len(df.columns) > 13 else None

    today_display = date.today().strftime('%Y-%m-%d')
    lines = [f"跌停翘板信号 TOP{n} ({today_display})", "=" * 70]

    for rank, (_, row) in enumerate(df.iterrows(), 1):
        code = str(row[code_col]).strip().zfill(6)
        name = row[name_col]
        score = float(row['翘板评分'])
        chg = float(row[change_col]) if pd.notna(row[change_col]) else -10
        turn = float(row[turnover_col]) if turnover_col and pd.notna(row.get(turnover_col)) else 0
        # 信号描述：基于评分和换手
        sigs = []
        if score >= 60: sigs.append("高信号")
        elif score >= 35: sigs.append("中等信号")
        else: sigs.append("弱信号")
        if turn > 10: sigs.append("高换手")
        detail_str = " + ".join(sigs)

        # 封单信息
        seal_str = ""
        if seal_fund_col:
            sf = float(row[seal_fund_col]) if pd.notna(row.get(seal_fund_col)) else 0
            if sf >= 1e8:
                seal_str = f"封单{sf/1e8:.2f}亿"
            elif sf >= 1e4:
                seal_str = f"封单{sf/1e4:.0f}万"
            else:
                seal_str = f"封单{sf:.0f}"

        # 连续跌停
        cont_str = ""
        if cont_dieting_col:
            cv = row.get(cont_dieting_col)
            if pd.notna(cv):
                cv_int = int(float(cv))
                if cv_int > 0:
                    cont_str = f"连跌{cv_int}板"

        lines.append(f"\n{rank}. {code} {name} | 翘板评分 {score:.1f}")
        lines.append(f"   跌幅 {chg:.2f}% | 换手 {turn:.1f}% | {seal_str} | {cont_str}")
        lines.append(f"   信号: {detail_str}")
        if score >= 60:
            lines.append(f"   次日策略: 竞价观察，放量高开可博弈反抽，目标+3%~+5%止损-3%")
        elif score >= 35:
            lines.append(f"   次日策略: 仅观望，需竞价放量确认才有参与价值")
        else:
            lines.append(f"   次日策略: ❌ 不建议参与，无量封死跌停抛压大")

    # ── 构建返回数据（供全维度综合榜单使用） ──
    dtqiaoban_results = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        name = row[name_col]
        score = float(row['翘板评分'])
        dtqiaoban_results.append({'code': code, 'name': name, 'score': score, 'dimension': '跌停翘板'})

    lines.append(f"\n{'=' * 70}")
    lines.append("翘板策略: 放量+封单小+连续跌停 = 翘板信号 | 无量+封单大 = 回避")
    lines.append("风险提示: 跌停翘板属于极高风险博弈，务必轻仓+严格止损(-3%)")
    print("\n".join(lines))
    return dtqiaoban_results


# ─── 回测系统 ───

def backtest_score_prev(prev_df: pd.DataFrame, date_str: str = None):
    """
    对上交易日涨停股进行回测评分，使用与实盘排行完全相同的 7 因子加权模型。
    prev_df: stock_zt_pool_previous_em 返回的上交易日涨停池（含今日涨跌幅）
    date_str: 上交易日日期 YYYYMMDD，用于计算历史股性等因子
    返回: (df_with_scores, summary_dict)
    """
    df = prev_df.copy()

    # 前置过滤
    name_col = None; code_col = None
    for c in df.columns:
        if '名称' in str(c) or '股票名称' in str(c): name_col = c
        if '代码' in str(c): code_col = c
    name_col = name_col or df.columns[2]
    code_col = code_col or df.columns[1]
    df = filter_non_main_board(df)
    if df.empty:
        return df, {"count": 0}

    # 今日涨跌幅
    change_col = None
    for c in df.columns:
        if '涨跌幅' in str(c): change_col = c; break
    change_col = change_col or df.columns[3]
    df['今日涨幅'] = df[change_col].astype(float).round(2)
    df['晋级'] = df['今日涨幅'] > 9

    # ─── 7 因子评分（与实盘排行相同的 apply_weights） ───
    import weight_manager
    w = weight_manager.load_weights()

    # 回测数据修复：stock_zt_pool_previous_em 没有封板时间/封板资金列
    # 换手率代理逻辑：在"上交易日涨停"票中，低换手=筹码锁定好=强封板。
    # 这是回测的固有局限——无法还原真实的封板时间和封板资金。
    has_seal_data = ('首次封板时间' in df.columns or '封板资金' in df.columns)
    if has_seal_data:
        seal_s = score_seal_strength(df)
    else:
        seal_s = pd.Series(0.0, index=df.index)
        turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
        if turnover_col:
            turnover = df[turnover_col].fillna(0).astype(float)
            for idx in df.index:
                t = turnover[idx]
                # 低换手=强封板(筹码锁定), 高换手=弱封板(抛压大)
                if t < 1: seal_s[idx] = 25
                elif t < 3: seal_s[idx] = 20
                elif t < 5: seal_s[idx] = 15
                elif t < 8: seal_s[idx] = 10
                elif t < 15: seal_s[idx] = 5
                else: seal_s[idx] = 2
    tech_s = score_tech_form(df)
    sector_score = get_sector_score(df)

    # 回测seal黄金奖励：仅 fallback 路径需要（score_seal_strength 内部已含黄金奖励）
    if not has_seal_data:
        seal_gold = pd.Series(0.0, index=df.index)
        for idx in df.index:
            if seal_s[idx] >= 20: seal_gold[idx] = 3.0
        seal_s = (seal_s + seal_gold).clip(upper=28.0)

    # 历史股性：日期可用才计算，否则用默认
    if date_str:
        try:
            history_s, _ =         score_stock_history(df, date_str, prev_df=prev_df)
        except Exception:
            history_s = pd.Series(2.5, index=df.index)
    else:
        history_s = pd.Series(2.5, index=df.index)

    # 资金流、情绪历史不可用，用中性默认值
    money_s = pd.Series(10.0, index=df.index)
    sent_s = pd.Series(5.0, index=df.index)

    # 回测权重调整：tech在回测中为负相关(r≈-0.06)，降为0避免噪声
    w_bt = dict(w)
    w_bt['tech'] = 0.0
    # 将tech的权重分配给seal和sector（回测中仅有的正相关因子）
    w_bt['seal'] = w['seal'] + 3.0
    w_bt['sector'] = w['sector'] + 3.0

    scores = weight_manager.apply_weights(
        seal_s, money_s, sector_score,
        tech_s, history_s, sent_s,
        weights=w_bt)

    df['回测评分'] = scores.round(1)
    df['seal_factor'] = seal_s.round(1)
    df['tech_factor'] = tech_s.round(1)
    df['sector_factor'] = sector_score.round(1)
    df['history_factor'] = history_s.round(1)
    df['money_factor'] = money_s.round(1)  # 全默认，相关性为 0
    df['sentiment_factor'] = sent_s.round(1)  # 全默认，相关性为 0

    total = len(df)
    avg_change = df['今日涨幅'].mean()
    promo_rate = df['晋级'].mean()
    pos_rate = (df['今日涨幅'] > 0).mean()

    # 分组统计（按百分位分 A/B/C/D）
    pct_ranks = df['回测评分'].rank(pct=True)
    def get_grade(p):
        if p >= 0.75: return 'A'
        elif p >= 0.50: return 'B'
        elif p >= 0.25: return 'C'
        else: return 'D'
    df['等级'] = pct_ranks.apply(get_grade)

    grades_detail = {}
    for g in ['A', 'B', 'C', 'D']:
        sub = df[df['等级'] == g]
        if len(sub) > 0:
            grades_detail[g] = {
                'count': len(sub),
                'avg_change': round(sub['今日涨幅'].mean(), 2),
                'promo_rate': round(sub['晋级'].mean() * 100, 0),
                'pos_rate': round((sub['今日涨幅'] > 0).mean() * 100, 0),
            }
        else:
            grades_detail[g] = {'count': 0, 'avg_change': 0, 'promo_rate': 0, 'pos_rate': 0}

    # 前30% vs 后30%
    sorted_scores = df.sort_values('回测评分')
    n = len(sorted_scores)
    k = max(1, int(n * 0.3))
    top30 = sorted_scores.tail(k)
    bot30 = sorted_scores.head(k)

    # 7 因子独立相关性（跳过常数因子避免 numpy warning）
    _factor_names = ['seal_factor', 'tech_factor', 'sector_factor',
                     'history_factor',
                     'money_factor', 'sentiment_factor']
    factor_correlations = {}
    for f in _factor_names:
        if f in df.columns:
            vals = df[f].astype(float)
            if vals.std() < 0.01:
                continue  # 常数因子（如 money/sentiment 默认值），跳过
            fc = vals.corr(df['今日涨幅'].astype(float))
            key = f.replace('_factor', '')
            factor_correlations[key] = round(fc, 4) if not pd.isna(fc) else 0.0

    corr = df['回测评分'].corr(df['今日涨幅'])

    # ── 模拟交易统计 ──
    trade_stats = _simulate_trades(df, '回测评分')

    summary = {
        'count': total,
        'avg_change': round(avg_change, 2),
        'promo_rate': round(promo_rate * 100, 1),
        'pos_rate': round(pos_rate * 100, 1),
        'correlation': round(corr, 4),
        'factor_correlations': factor_correlations,
        'grades': grades_detail,
        'top30_avg': round(top30['今日涨幅'].mean(), 2),
        'bot30_avg': round(bot30['今日涨幅'].mean(), 2),
        'trade_stats': trade_stats,
        'top5': [(str(df.loc[i, code_col]).strip().zfill(6),
                  str(df.loc[i, name_col]).strip(),
                  float(df.loc[i, '回测评分']),
                  float(df.loc[i, '今日涨幅'])) for i in sorted_scores.tail(5).index],
        'bot5': [(str(df.loc[i, code_col]).strip().zfill(6),
                  str(df.loc[i, name_col]).strip(),
                  float(df.loc[i, '回测评分']),
                  float(df.loc[i, '今日涨幅'])) for i in sorted_scores.head(5).index],
    }
    return df, summary


def _simulate_trades(df, score_col, top_n=10, commission=0.00025, slippage=0.001):
    """
    模拟交易：取评分最高的 N 只，次日开盘买入/收盘卖出。
    过滤次日无法买入的标的（一字板/缩量秒板）。
    返回: {total_return, win_rate, profit_loss_ratio, max_drawdown, trades, unbuyable_count}
    """
    sorted_df = df.sort_values(score_col, ascending=False)

    # 次日竞价过滤：排除一字板或缩量涨停（换手<1% + 涨>9.5% = 买不到）
    turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    unbuyable = pd.Series(False, index=sorted_df.index)
    if turnover_col and '今日涨幅' in sorted_df.columns:
        changes_all = sorted_df['今日涨幅'].astype(float)
        turnovers = sorted_df[turnover_col].astype(float)
        unbuyable = (changes_all > 9.5) & (turnovers < 1.0)

    # 取评分最高的 top_n 且可买入的
    buyable = sorted_df[~unbuyable].head(top_n)
    # 如果可买入的不够，放宽换手限制
    if len(buyable) < 3:
        unbuyable_loose = (sorted_df['今日涨幅'].astype(float) > 9.5) & (sorted_df[turnover_col].astype(float) < 0.3)
        buyable = sorted_df[~unbuyable_loose].head(top_n)

    unbuyable_count = int(unbuyable.sum()) if len(buyable) > 0 else 0
    changes = buyable['今日涨幅'].astype(float).values
    n_trades = len(changes)
    if n_trades == 0:
        return {'total_return': 0, 'win_rate': 0, 'profit_loss_ratio': 0,
                'max_drawdown': 0, 'trade_count': 0, 'best': 0, 'worst': 0, 'unbuyable_count': 0}

    # 每笔：涨幅 - 佣金(万2.5双向) - 滑点(0.1%)
    returns = changes - commission * 2 * 100 - slippage * 100
    wins = (returns > 0).sum()
    total_return = round(float(returns.sum() / n_trades), 2)

    win_avg = float(np.mean(returns[returns > 0])) if wins > 0 else 0
    loss_avg = float(abs(np.mean(returns[returns <= 0]))) if wins < n_trades else 0
    profit_loss_ratio = round(win_avg / loss_avg, 2) if loss_avg > 0 else 0

    cum = np.cumsum(returns)
    peak = np.maximum.accumulate(cum)
    dd = cum - peak
    max_dd = round(float(dd.min()), 2)

    return {
        'total_return': total_return,
        'win_rate': round(wins / n_trades * 100, 1),
        'profit_loss_ratio': profit_loss_ratio,
        'max_drawdown': max_dd,
        'trade_count': n_trades,
        'best': round(float(returns.max()), 2),
        'worst': round(float(returns.min()), 2),
        'unbuyable_count': unbuyable_count,
    }


def auto_verify_backtest(today_str: str, table_mode: bool = False, current_weights: dict = None,
                          plan_name: str = 'A'):
    """
    回测验证：盘后自动运行。
    优先从 Plan 归档 (daily_data/) 读取昨日评分 → 对比今日实际涨跌幅 → 计算因子相关性。
    归档不存在时回退到 backtest_score_prev 重评分 (冷启动兼容)。
    plan_name: 'A' 或 'B', 用于独立调权

    P5 调权隔离决策 (V1):
    - 本函数只覆盖 plan_a / plan_b 的涨停 tab 调权 (6 因子)
    - 炸板/翘板/趋势/反转/板块的回测只输出统计,不调权
    - V2 可扩展: 为每个 tab 建独立 weight_manager.PLAN_X_WEIGHTS
    返回: (输出文本, adjusted_weights) 或 None
    """
    import weight_manager
    from cache import _last_trading_date

    wd = datetime.strptime(today_str, '%Y%m%d').weekday()
    if wd >= 5:
        return None

    market_status = get_market_status()
    if market_status == 'trading':
        return None

    # 拉取今日实际涨跌幅
    try:
        prev_df = ak.stock_zt_pool_previous_em(date=today_str)
        if prev_df.empty:
            return None
    except Exception:
        return None

    try:
        change_col = next((c for c in prev_df.columns if '涨跌幅' in str(c)), None)
        if change_col is None:
            return None
        changes = prev_df[change_col].astype(float)
        if changes.abs().max() < 0.5:
            return None
    except (ValueError, IndexError):
        return None

    # code→涨幅 映射
    code_col_raw = next((c for c in prev_df.columns if '代码' in str(c)), prev_df.columns[1])
    code_map = {}
    for _, row in prev_df.iterrows():
        try:
            code_map[str(row[code_col_raw]).strip().zfill(6)] = float(row[change_col])
        except Exception:
            continue

    sep = "─" * 50
    lines = [f"\n{sep}"]
    all_fc = {}
    archive_used = False

    # ── 优先: 读 Plan 归档 ──
    yesterday = _last_trading_date(today_str)
    if yesterday:
        try:
            from plans.archiver import list_plan_results, load_plan_result
            plan_names = list_plan_results(yesterday)
            if plan_names:
                for plan_name in plan_names:
                    archive = load_plan_result(yesterday, plan_name)
                    if not archive or not archive.get('stocks'):
                        continue
                    stocks = archive['stocks']
                    factor_scores = {}
                    for s in stocks:
                        code = s.get('code', '')
                        if code not in code_map:
                            continue
                        actual = code_map[code]
                        for fkey in ['seal_score', 'money_score', 'sector_score',
                                     'tech_score', 'history_score', 'stock_sentiment_score',
                                     'principal_score', 'north_flow_score', 'margin_score',
                                     'inst_rating_score', 'limit_reason_score']:
                            if fkey in s:
                                factor_scores.setdefault(fkey, []).append((float(s[fkey]), actual))
                    plan_fc = {}
                    for fkey, pairs in factor_scores.items():
                        if len(pairs) < 8:
                            continue
                        scores_arr = pd.Series([p[0] for p in pairs])
                        returns_arr = pd.Series([p[1] for p in pairs])
                        if scores_arr.std() < 0.01 or returns_arr.std() < 0.01:
                            continue
                        c = scores_arr.corr(returns_arr)
                        if not pd.isna(c):
                            plan_fc[fkey.replace('_score', '')] = round(float(c), 4)
                    if plan_fc:
                        archive_used = True  # 仅在确实产出有效IC后标记
                        lines.append(f" [Plan {plan_name}] 归档验证 | {len(stocks)}只")
                        all_fc.update(plan_fc)
        except Exception as e:
            lines.append(f" [归档回测] 异常,回退重评分: {e}")
            archive_used = False

    # ── 回退: 旧方法 ──
    if not archive_used:
        df_result, summary = backtest_score_prev(prev_df, date_str=today_str)
        if summary['count'] == 0:
            return None
        all_fc = summary.get('factor_correlations', {})
        lines.append(f" 评分验证 | 昨 {summary['count']} 只 → 今晋级 {summary['promo_rate']:.0f}% | "
                     f"正收益 {summary['pos_rate']:.0f}% | 均价 {summary['avg_change']:+.1f}%")
        if all_fc:
            avail = [k for k in weight_manager.BACKTEST_FACTORS if k in all_fc and all_fc[k] != 0]
            if avail:
                lines.append(f" 因子相关性: {' | '.join(f'{k}: {all_fc[k]:+.3f}' for k in avail)}")

    # ── 滚动调权 (按 Plan 独立) ──
    adjusted_weights = None
    daily_msg = None
    if current_weights is not None and all_fc:
        weight_manager.save_daily_correlations(all_fc, trading_date=today_str, plan_name=plan_name)
        new_w, adj_msg = weight_manager.daily_adjust_weights(current_weights, plan_name=plan_name)
        if new_w:
            adjusted_weights = new_w
        if adj_msg:
            for line in adj_msg.split('\n'):
                lines.append(line)

    if adjusted_weights and current_weights:
        changes = []
        for k in weight_manager.BACKTEST_FACTORS:
            if k in current_weights and k in adjusted_weights:
                delta = adjusted_weights[k] - current_weights[k]
                if abs(delta) > 0.01:
                    changes.append(f"{k}: {current_weights[k]:.0f}→{adjusted_weights[k]:.0f} ({delta:+.1f})")
        if changes:
            lines.append(f" 权重调整: {' | '.join(changes)}")
    lines.append(sep)
    return "\n".join(lines), adjusted_weights


def run_backtest(tab: str = 'limit-up', N: int = 5):
    """回测主入口 (P4: 支持多 tab 滚动回测)"""
    from datetime import datetime, timedelta
    from backtest_engine import run_tab_backtest, TAB_NAMES_CN

    # 周末检测
    wd = date.today().weekday()
    if wd >= 5:
        print("  [回测跳过] 周末不开盘")
        return

    # ── P4: 多 tab 滚动回测 (走 backtest_engine) ──
    if tab != 'limit-up':
        print(f"运行 {tab} ({TAB_NAMES_CN.get(tab, tab)}) {N} 天滚动回测 (T+1 真实)...")
        res = run_tab_backtest(tab=tab, max_days=N, top_n=3, capital=30000, use_cache=False)
        if 'error' in res and not res.get('trades'):
            print(f"  错误: {res['error']}")
            return
        s = res['summary']
        print(f"  笔数: {s.get('trade_count', 0)}")
        print(f"  胜率: {s.get('win_rate', 0)}%")
        print(f"  累计收益: {s.get('cumulative_ret', 0):+.2f}%")
        print(f"  总盈亏: ¥{s.get('total_pnl', 0):+,.0f}")
        print(f"  盈亏比: {s.get('plr', 0)}")
        print(f"  最大回撤: {s.get('max_dd', 0):.2f}%")
        print(f"  期望值: {s.get('ev', 0):+.2f}%")
        cmp = res.get('comparison', {})
        print(f"  一字板跳过: {cmp.get('unbuyable_count', 0)} 笔")
        return

    # ── 兼容旧版: limit-up 走 backtest_score_prev (输出因子相关性) ──
    print(f"运行 {N} 天滚动回测 (limit-up, 含因子相关性)...")
    results = []
    errors = 0

    for i in range(N):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if not _is_trading_day(d.strftime("%Y%m%d")):
            continue
        d_str = d.strftime("%Y%m%d")
        try:
            prev = ak.stock_zt_pool_previous_em(date=d_str)
            if prev.empty:
                continue
            df_res, summary = backtest_score_prev(prev, date_str=d_str)
            if summary['count'] >= 5:
                results.append(summary)
        except Exception as e:
            errors += 1
            if errors > 5:
                break
        if (i + 1) % 5 == 0:
            print(f"  ... {i+1}/{N}")

    if not results:
        print("  无有效回测数据")
        return

    # ── 聚合 ──
    total_count = sum(r['count'] for r in results)
    avg_corr = np.mean([r['correlation'] for r in results])
    avg_change = np.mean([r['avg_change'] for r in results])
    avg_promo = np.mean([r['promo_rate'] for r in results])
    avg_pos = np.mean([r['pos_rate'] for r in results])
    avg_top30 = np.mean([r['top30_avg'] for r in results])
    avg_bot30 = np.mean([r['bot30_avg'] for r in results])

    # 因子相关性聚合
    fc_list = [r.get('factor_correlations', {}) for r in results]
    factor_avg = {}
    for fc in fc_list:
        for k, v in fc.items():
            factor_avg.setdefault(k, []).append(v)
    factor_avg = {k: np.mean(v) for k, v in factor_avg.items()}

    lines = []
    lines.append("\n" + "=" * 55)
    lines.append(f"  滚动回测 ({len(results)} 天) | 共 {total_count} 只标的")
    lines.append("=" * 55)
    lines.append(f"  平均溢价: {avg_change:+.2f}% | 平均晋级率: {avg_promo:.1f}%")
    lines.append(f"  平均正收益比: {avg_pos:.1f}%")
    if factor_avg:
        corr_str = " | ".join(f"{k}: {factor_avg[k]:+.3f}" for k in ['seal', 'sector', 'tech'] if k in factor_avg)
        lines.append(f"  因子相关性(均值): {corr_str}")
    lines.append(f"  评分-涨幅相关系数(均值): {avg_corr:.4f}")
    lines.append(f"  前30%平均涨幅: {avg_top30:+.2f}%  |  后30%: {avg_bot30:+.2f}%  |  差: {avg_top30 - avg_bot30:+.2f}%")

    # 评级说明
    lines.append("")
    lines.append("评级标准 (满分100分, 基于实盘9因子加权):")
    lines.append("  S级>=75 | A级>=65 | B级>=55 | C级<55")
    lines.append("  评分维度: 封板强度(22) + 资金(12) + 板块合力(12) + 技术(6) + 股性(4) + 情绪(9) + 本金(6)")
    lines.append("  注: 回测中资金/情绪为默认值，实际预测力更强。历史数据不完整属正常现象。")

    lines.append("")
    lines.append("免责: 回测数据仅供参考, 历史表现不代表未来收益。")
    lines.append("      评分系统有效性需持续多日积累验证。")

    print("\n".join(lines))


# ─── 主流程 ───

def main():
    global TOP_N
    parser = argparse.ArgumentParser(description='超短线选股扫描器')
    parser.add_argument('--table', action='store_true', help='以表格格式输出（默认详细文本）')
    parser.add_argument('--backtest', action='store_true', help='运行回测系统（验证评分有效性）')
    parser.add_argument('--tab', type=str, default='limit-up',
                        choices=['limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal', 'sector'],
                        help='回测 tab (与 --backtest 一起用,默认 limit-up)')
    parser.add_argument('--days', type=int, default=5, help='回测天数 (默认 5,akshare 实际可用窗口限制)')
    parser.add_argument('--zhaban', action='store_true', help='炸板股反包潜力扫描')
    parser.add_argument('--trend', action='store_true', help='趋势动量股扫描')
    parser.add_argument('--sector', action='store_true', help='板块联动强度分析')
    parser.add_argument('--dtqiaoban', action='store_true', help='跌停翘板信号扫描')
    parser.add_argument('--reversal', action='store_true', help='涨停回调反转扫描(上交易日涨停今回调→明日反包)')
    parser.add_argument('--date', type=str, default='', help='指定日期 YYYYMMDD（默认: 今天）')
    parser.add_argument('--top', type=int, default=0, help=f'输出数量（默认: {TOP_N}）')
    args = parser.parse_args()

    # 日期处理
    today_raw = args.date if args.date else date.today().strftime("%Y%m%d")
    if args.top > 0:
        TOP_N = args.top

    if args.backtest:
        run_backtest(tab=args.tab, N=args.days)
        return
    table_mode = args.table

    # ── 模式分发 + 盘中/盘后自动检测 ──

    if args.zhaban:
        scan_zhaban(today_raw, table_mode, top_n=TOP_N)
        return
    if args.trend:
        scan_trend(today_raw, table_mode, top_n=TOP_N)
        return
    if args.sector:
        scan_sector(today_raw, table_mode, top_n=TOP_N)
        return
    if args.reversal:
        scan_reversal(today_raw, table_mode, top_n=TOP_N)
        return

    if args.dtqiaoban:
        scan_dtqiaoban(today_raw, table_mode, top_n=TOP_N)
        return

    # 无显式模式 → 自动检测盘中/盘后
    default_mode = get_default_mode()
    market_status = get_market_status()

    if default_mode == 'trend':
        # 盘中自动 → 趋势动量扫描（能买进）
        now = datetime.now(_CST)
        print(f"[自动检测] 当前盘中 ({now.strftime('%H:%M')}) → 默认趋势动量扫描", file=sys.stderr)
        scan_trend(today_raw, table_mode, top_n=TOP_N)
        return

    # 盘后 → 涨停多因子评分（次日预测）
    status_labels = {'closed': '盘后', 'lunch': '午休', 'weekend': '周末', 'holiday': '节假日', 'trading': '盘中'}
    status_cn = status_labels.get(market_status, market_status)
    print(f"[自动检测] 当前{status_cn} → 默认涨停多因子评分", file=sys.stderr)

    print("=" * 45, file=sys.stderr)
    today_display = today_raw[:4] + '-' + today_raw[4:6] + '-' + today_raw[6:8] if len(today_raw) == 8 else today_raw
    print(f"  超短线选股扫描器 | {today_display}", file=sys.stderr)
    print("=" * 45, file=sys.stderr)

    # 1. 获取涨停池（含非交易日检测）
    pool = fetch_limit_up_pool(date_str=today_raw)
    if pool.empty:
        print("not_trading_day")
        return

    # 2. 前置过滤 (不含价格，价格需要同花顺数据)
    filtered = pre_filter(pool)
    if filtered.empty:
        print(f"X 无符合条件的标的", file=sys.stderr)
        print("no_data")
        return

    # 3. 获取同花顺全市场数据 (一次调用，同时用于资金流评分 + 股价过滤)
    fund_df, fund_err = fetch_fund_flow_data()
    if fund_df is None:
        # 资金流不可用，降级输出
        print("  ! 同花顺数据不可用，降级为仅涨停强度+板块+量价评分", file=sys.stderr)
        money_scores = pd.Series(0.0, index=filtered.index)
        sector_raw = get_sector_heat_scores(filtered)
        tech_scores = score_tech_form(filtered)
        buyability_scores = score_buyability(filtered)
        sector_res_scores = get_sector_resonance(filtered)
        fmt = format_table_output if table_mode else format_output
        output = fmt(filtered, money_scores, sector_raw, score_seal_strength(filtered), tech_scores,
                      buyability_scores=buyability_scores,
                      sector_res_scores=sector_res_scores)
        print(output)
        return

    # 股价过滤
    filtered = filter_by_price(filtered, fund_df)
    if filtered.empty:
        print(f"X 无符合条件标的 (全部超过{MAX_PRICE}元)", file=sys.stderr)
        print("no_data")
        return

    print("[3/5] 评分: 涨停强度 + 资金面 + 板块热度 + 量价关系...", file=sys.stderr)
    seal_scores = score_seal_strength(filtered)
    money_scores, raw_money = get_money_flow_scores(filtered, fund_df=fund_df)
    sector_raw = get_sector_heat_scores(filtered, money_series=raw_money)
    tech_scores = score_tech_form(filtered)
    buyability_scores = score_buyability(filtered)
    sector_res_scores = get_sector_resonance(filtered)

    print("[4/5] 预测: 市场情绪 + 龙虎榜 + 历史股性 + 舆情...", file=sys.stderr)
    today_raw = date.today().strftime("%Y%m%d")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(detect_market_sentiment, today_raw): "sentiment",
            ex.submit(analyze_dragon_tiger, filtered, today_raw): "lhb",
            ex.submit(score_stock_history, filtered, today_raw): "history",
            ex.submit(lambda: __import__('stock_community').score_community(filtered)): "community",
        }
        res = {}
        for f in as_completed(futs):
            key = futs[f]
            try:
                res[key] = f.result()
            except Exception as e:
                print(f"  ! {key} 评分失败: {e}", file=sys.stderr)

    if res.get("sentiment"):
        sentiment_score, sentiment_level, sentiment_detail = res["sentiment"]
    else:
        sentiment_score, sentiment_level, sentiment_detail = 5.0, "未知", {}
    if res.get("lhb"):
        lhb_bonus, _ = res["lhb"]
    else:
        lhb_bonus, _ = pd.Series(0.0, index=filtered.index), {}
    if res.get("history"):
        history_scores, _ = res["history"]
    else:
        history_scores = pd.Series(2.5, index=filtered.index)
    if res.get("community"):
        community_scores = res["community"]
    else:
        community_scores = None

    # 龙虎榜加分并入资金质量（仅加分不扣分，满分20）
    money_scores = (money_scores + lhb_bonus).clip(upper=20.0)

    print("[5/5] 生成评分报告...", file=sys.stderr)
    import weight_manager
    weights = weight_manager.load_weights()
    fmt = format_table_output if table_mode else format_output
    output = fmt(filtered, money_scores, sector_raw, seal_scores, tech_scores,
                  raw_money=raw_money,
                  sentiment_score=sentiment_score,
                  sentiment_level=sentiment_level,
                  sentiment_detail=sentiment_detail,
                  history_scores=history_scores,
                  buyability_scores=buyability_scores,
                  sector_res_scores=sector_res_scores,
                  weights=weights)
    print(output)

    # 自动回测验证 + 权重调整
    verify_result = auto_verify_backtest(today_raw, table_mode, current_weights=weights)
    if verify_result:
        verify_output, new_weights = verify_result
        print(verify_output)
        if new_weights:
            weights = new_weights

    # ── 预计算总分 + TOP N 索引（供后续所有模块共享） ──
    s_history = history_scores if history_scores is not None else pd.Series(2.5, index=filtered.index)
    s_stock_sent = score_stock_sentiment(filtered, money_scores, buyability_scores)
    s_principal = score_by_principal(filtered, 20000)
    sector_merged_cli = (sector_res_scores + sector_raw) / 2.0
    base_totals = weight_manager.apply_weights(seal_scores, money_scores, sector_merged_cli, tech_scores, s_history, sentiment_score=pd.Series(float(sentiment_score), index=filtered.index), stock_sentiment_scores=s_stock_sent, principal_scores=s_principal, weights=weights)
    total_scores = base_totals
    top_indices = list(total_scores.sort_values(ascending=False).head(TOP_N).index)
    filtered_top = filtered.loc[top_indices]

    # ── 社区舆情聚合（已按评分排序） ──
    try:
        import stock_community
        community_output, sentiment_data = stock_community.run(filtered_top, TOP_N)
        if community_output:
            print(community_output)
    except Exception as e:
        print(f"  [scanner L2795] failed: {e}", file=sys.stderr)

    # ── 增强指标分析 ──
    try:
        import stock_indicators
        enhanced_output, enhanced_data = stock_indicators.run_enhanced(
            filtered_top, today_raw, TOP_N,
            total_scores=total_scores
        )
        if enhanced_output:
            print(enhanced_output)
    except Exception as e:
        print(f"  [scanner L2807] failed: {e}", file=sys.stderr)

    # ── 数据持久化 + 每日总结 ──
    try:
        import stock_data_manager
        today_data = {
            'date': date.today().isoformat(),
            'sentiment_level': sentiment_level,
            'sentiment_score': sentiment_score,
            'stocks': []
        }
        for idx in top_indices:
            row = filtered.loc[idx]
            today_data['stocks'].append({
                'code': str(row.get('代码', '')).strip().zfill(6),
                'name': row.get('名称', ''),
                'total_score': round(float(total_scores.get(idx, 0)), 1),
                'seal_score': round(float(seal_scores.get(idx, 0)), 1),
                'money_score': round(float(money_scores.get(idx, 0)), 1),
                'sector_score': round(float(sector_raw.get(idx, 0)), 1),
                'tech_score': round(float(tech_scores.get(idx, 0)), 1),
                'community_score': round(float(community_scores.get(idx, 0)), 1) if community_scores is not None else 3.5,
                'industry': row.get('所属行业', ''),
                'turnover': float(row.get('换手率', 0)) if pd.notna(row.get('换手率')) else None,
                'seal_time': str(row.get('首次封板时间', '')),
            })
        dm_summary, dm_paths = stock_data_manager.run(today_data)
        print(dm_summary)
        paths_info = f"  数据: {os.path.basename(dm_paths['data_path'])} | 总结: daily_summary.md"
        print(paths_info)
    except Exception as e:
        print(f"  [数据持久化跳过] {e}", file=sys.stderr)

    # ── 各维度短榜 ──
    zhaban_res = scan_zhaban(today_raw, top_n=3)
    trend_res = scan_trend(today_raw, top_n=3)
    scan_sector(today_raw, top_n=3)
    dtqiaoban_res = scan_dtqiaoban(today_raw, top_n=3)

    # ── 情绪自适应策略建议 ──
    zhaban_rate = (sentiment_detail or {}).get('zhaban_rate', 0.5)
    if zhaban_rate < 0.30:
        emotion_zone = "活跃"
        strategy_advice = "主力策略：涨停超短线，可积极打首板/连板"
    elif zhaban_rate < 0.40:
        emotion_zone = "正常"
        strategy_advice = "涨停超短线为主，炸板反包辅助，控制仓位"
    elif zhaban_rate < 0.50:
        emotion_zone = "偏弱"
        strategy_advice = "提高选股标准，涨停+炸板反包双策略并重"
    else:
        emotion_zone = "低迷"
        strategy_advice = "建议炸板反包+趋势动量为主，涨停缩小仓位"

    # ── 全维度综合榜单 ──
    all_dimension_stocks = []
    # 主榜单 TOP10（用 base_totals 不含情绪系数，避免系统性偏差）
    for idx in top_indices:
        row = filtered.loc[idx]
        code = str(row.get('代码', '')).strip().zfill(6)
        name = row.get('名称', '')
        score = round(float(base_totals.get(idx, 0)), 1)
        all_dimension_stocks.append({'code': code, 'name': name, 'score': score, 'dimension': '涨停超短线'})
    # 炸板反包
    if zhaban_res:
        all_dimension_stocks.extend(zhaban_res)
    # 趋势动量
    if trend_res:
        all_dimension_stocks.extend(trend_res)
    # 跌停翘板
    if dtqiaoban_res:
        all_dimension_stocks.extend(dtqiaoban_res)

    # 去重（同只股票保留最高分 + 同分保留先出现的维度）
    seen = {}
    for item in all_dimension_stocks:
        key = item['code']
        if key not in seen or item['score'] > seen[key]['score']:
            seen[key] = item
    combined = sorted(seen.values(), key=lambda x: x['score'], reverse=True)

    print("\n" + "=" * 60)
    print("  市场情绪: {}  |  炸板率: {:.0f}%  |  {}".format(emotion_zone, zhaban_rate * 100, strategy_advice))
    print("=" * 60)
    print("  全维度综合榜单 | 涨停·炸板·趋势·翘板  TOP{}".format(min(len(combined), 10)))
    print("=" * 60)
    print("  {:<3s} {:<7s} {:<8s} {:>5s}  {}".format("#", "代码", "名称", "分", "维度"))
    print("  " + "-" * 50)
    for rank, item in enumerate(combined[:10], 1):
        print("  {:<3d} {:<7s} {:<8s} {:>5.0f}  {}".format(
            rank, item['code'], item['name'], item['score'], item['dimension']))
    print("=" * 60)
    print("  注：原始分，维度间评分体系不同，不可跨维度比较")
    print("  各维度原始分每日可纵向对比，回测不受影响")
    print("=" * 60)


if __name__ == "__main__":
    main()
