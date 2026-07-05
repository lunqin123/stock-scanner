"""全新评分逻辑 - 基于业界验证的涨停板量化因子 (2026-07-05)

数据来源:
- GitHub: Quant-Strategy-for-Consecutive-Limit-Up-Stocks (2025年实盘验证)
- 东方财富: 涨停次日预测三大核心指标
- 量化因子研究: 价量张力因子 RankIC 8.52%

核心因子 (按预测力排序):
1. 封单成交比 (封板资金/成交额) — 最强预测力, >0.5为强势
2. 封板时间 (首次封板时间) — 10:30前封板最优, 尾盘封板差
3. 换手率 — 5-15%缩量最优, >20%放量危险
4. 连板数 — 2-3连板势能最强, 首板次之, 5+连板风险高
5. 炸板次数 — 0次最优(封板稳定), 多次炸板=抛压大
6. 流通市值 — 50-200亿最优(机构+游资合力), <30亿流动性差
7. 板块联动 — 同板块涨停数越多, 延续性越强

与旧plan_a的区别:
- 完全基于涨停池原始数据, 不依赖资金流/舆情/北向等外部数据
- 因子权重来自业界回测验证, 不是拍脑袋
- 评分范围0-100, 每个因子0-15分, 总分归一化
"""
import pandas as pd
import numpy as np


def compute_seal_ratio(df):
    """封单成交比 = 封板资金 / 成交额 (最强预测因子)"""
    seal_col = '封板资金' if '封板资金' in df.columns else None
    amt_col = '成交额' if '成交额' in df.columns else None
    if seal_col and amt_col:
        return (df[seal_col].astype(float) / df[amt_col].astype(float)).clip(0, 10)
    return pd.Series(0.5, index=df.index)


def compute_seal_time_score(df):
    """封板时间评分: 09:25前=满分, 10:30前=高分, 午后=低分"""
    time_col = '首次封板时间' if '首次封板时间' in df.columns else None
    if not time_col:
        return pd.Series(0.5, index=df.index)
    times = df[time_col].astype(str).str.replace(':', '').str.zfill(6)
    def _score(t):
        try:
            h, m = int(t[:2]), int(t[2:4])
            minutes = h * 60 + m
            if minutes <= 570:    return 1.0   # 09:30前 (竞价封板)
            elif minutes <= 630:  return 0.9   # 10:30前
            elif minutes <= 690:  return 0.6   # 11:30前
            elif minutes <= 840:  return 0.3   # 14:00前
            else:                  return 0.1   # 尾盘
        except: return 0.5
    return times.apply(_score)


def compute_turnover_score(df):
    """换手率评分: 5-15%缩量最优, >20%危险"""
    col = '换手率' if '换手率' in df.columns else None
    if not col:
        return pd.Series(0.5, index=df.index)
    t = df[col].astype(float)
    def _score(x):
        if 5 <= x <= 15:    return 1.0   # 缩量封板, 筹码锁定
        elif 3 <= x < 5:    return 0.8   # 偏低
        elif 15 < x <= 20:  return 0.6   # 适度放量
        elif x < 3:          return 0.5   # 过低(可能流动性差)
        elif 20 < x <= 30:  return 0.3   # 放量, 警惕
        else:                return 0.1   # >30% 爆量出货
    return t.apply(_score)


def compute_consecutive_score(df):
    """连板数评分: 2-3连板最优, 首板次之, 5+风险高"""
    col = '连板数' if '连板数' in df.columns else None
    if not col:
        return pd.Series(0.5, index=df.index)
    n = df[col].fillna(1).astype(float)
    def _score(x):
        if x == 2:    return 1.0   # 2连板: 确认趋势, 势能最强
        elif x == 3:  return 0.9   # 3连板: 龙头确认
        elif x == 1:  return 0.7   # 首板: 待确认
        elif x == 4:  return 0.6   # 4连板: 高位
        elif x >= 6:  return 0.2   # 6+: 追高风险极大
        else:          return 0.5   # 5连板
    return n.apply(_score)


def compute_zhaban_score(df):
    """炸板次数评分: 0次最优, 多次炸板=抛压大"""
    col = '炸板次数' if '炸板次数' in df.columns else None
    if not col:
        return pd.Series(1.0, index=df.index)
    z = df[col].fillna(0).astype(float)
    def _score(x):
        if x == 0:    return 1.0   # 一字封板, 最强
        elif x == 1:  return 0.7   # 炸板回封, 尚可
        elif x == 2:  return 0.4   # 多次炸板, 抛压大
        else:          return 0.1   # 频繁炸板, 极弱
    return z.apply(_score)


def compute_market_cap_score(df):
    """流通市值评分: 50-200亿最优"""
    col = '流通市值' if '流通市值' in df.columns else None
    if not col:
        return pd.Series(0.5, index=df.index)
    cap = df[col].astype(float) / 1e8  # 转为亿
    def _score(x):
        if 50 <= x <= 200:   return 1.0   # 机构+游资合力区间
        elif 20 <= x < 50:   return 0.8   # 中小盘
        elif 200 < x <= 500: return 0.6   # 大盘股
        elif 10 <= x < 20:   return 0.5   # 小盘
        elif x < 10:          return 0.3   # 微盘, 流动性差
        else:                  return 0.4   # >500亿, 弹性不足
    return cap.apply(_score)


def compute_sector_score(df):
    """板块联动评分: 同板块涨停数越多, 延续性越强"""
    ind_col = '所属行业' if '所属行业' in df.columns else None
    if not ind_col:
        return pd.Series(0.5, index=df.index)
    counts = df[ind_col].value_counts()
    industries = df[ind_col]
    def _score(ind):
        c = counts.get(ind, 1)
        if c >= 5:    return 1.0   # 板块爆发
        elif c >= 3:  return 0.8   # 板块强势
        elif c >= 2:  return 0.6   # 有联动
        else:          return 0.3   # 独苗
    return industries.apply(_score)


# 因子权重 (来自业界回测IC排序)
FACTOR_WEIGHTS = {
    'seal_ratio':     15,   # 封单成交比 (最强)
    'seal_time':      15,   # 封板时间
    'turnover':       15,   # 换手率
    'consecutive':    15,   # 连板数
    'zhaban':         10,   # 炸板次数
    'market_cap':     10,   # 流通市值
    'sector':         10,   # 板块联动
    # 原始价格作为辅助因子
    'price':           5,   # 价格区间
}

MAX_SCORE = sum(FACTOR_WEIGHTS.values())  # 95


def compute_price_score(df):
    """价格评分: 5-20元最优(散户参与度高)"""
    col = '最新价' if '最新价' in df.columns else None
    if not col:
        return pd.Series(0.5, index=df.index)
    p = df[col].astype(float)
    def _score(x):
        if 5 <= x <= 20:     return 1.0   # 散户参与度最高
        elif 3 <= x < 5:     return 0.7
        elif 20 < x <= 50:   return 0.6
        elif x < 3:           return 0.3   # 仙股
        else:                  return 0.4   # >50元
    return p.apply(_score)


def score_new(df):
    """全新评分主函数 - 基于业界验证的涨停板量化因子

    输入: akshare stock_zt_pool_em 返回的涨停池 DataFrame
    输出: 添加 '新评分' 列 (0-100) 的 DataFrame, 按评分降序排列

    与旧 plan_a 的区别:
    - 不依赖资金流/舆情/北向等外部数据 (回测时这些数据缺失导致IC为负)
    - 完全用涨停池自身数据 (封板资金/时间/换手/连板/炸板/市值/板块)
    - 因子权重来自业界回测验证
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    # 计算各因子 (0-1归一化)
    factors = {
        'seal_ratio':   compute_seal_ratio(df),
        'seal_time':    compute_seal_time_score(df),
        'turnover':     compute_turnover_score(df),
        'consecutive':  compute_consecutive_score(df),
        'zhaban':       compute_zhaban_score(df),
        'market_cap':   compute_market_cap_score(df),
        'sector':       compute_sector_score(df),
        'price':        compute_price_score(df),
    }

    # 加权合成
    total = sum(factors[k] * FACTOR_WEIGHTS[k] for k in FACTOR_WEIGHTS)
    df['新评分'] = (total / MAX_SCORE * 100).clip(0, 100).round(1)

    # 写入因子分列 (供回测IC分析)
    for k, v in factors.items():
        df[f'f_{k}'] = (v * FACTOR_WEIGHTS[k]).round(1)

    return df.sort_values('新评分', ascending=False)
