"""scanner_factors.py - 基础评分因子层

职责: 提供所有"对单只/批量股票打分"的纯函数 (无 IO 副作用,除必要的 akshare 调用)。
约束: 不依赖 scanner_scoring / scanner_scans / scanner_backtest。
      可依赖 utils / filters / data / cache / weight_manager (运行时导入避免循环)。
"""
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import akshare as ak

from scanner_utils import (
    money_str, seal_time_score, _vectorized_seal_time_score, _CST,
)
from config import MAX_PRICE


# ═══════════════════════════════════════════
#  封板质量评分
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  资金面评分
# ═══════════════════════════════════════════

def get_money_flow_scores(df: pd.DataFrame, fund_df=None):
    """通过同花顺批量获取个股资金流，区分主力结构评分。
    列索引: [6]主力净流入 [7]超大单净流入 [8]大单净流入
    fund_df: 可选，预获取的同花顺数据，避免重复请求。
    返回: (scores, raw_values) 评分 Series(0-15) 和对应的主力净额 Series(元)"""
    if fund_df is None:
        from scanner_data import fetch_fund_flow_data
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


# ═══════════════════════════════════════════
#  板块合力评分 (合并 sector_res + sector_mom)
# ═══════════════════════════════════════════

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

        # 基础分（0-8）：板块涨停数越多越高
        base = min(4 + cnt * 2, 8)

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


# ═══════════════════════════════════════════
#  量价关系评分
# ═══════════════════════════════════════════

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

            # 换手率博弈区间评级
            if 5 <= t <= 15:      base = 10.0  # 最佳博弈区间
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


# ═══════════════════════════════════════════
#  个股情绪评分
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  危险信号检测
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  本金适配评分
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  可买到过滤 + 开盘可行性评分
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  市场情绪检测
# ═══════════════════════════════════════════

def detect_market_sentiment(today_str: str):
    """
    市场情绪检测：基于上交易日涨停股今日的溢价表现。
    使用 stock_zt_pool_previous_em 获取上交易日涨停池（含今日涨跌幅）。
    返回: (score, level, details_dict)
    - score: 0-10 分
    - level: 冰点/低迷/正常/活跃/高潮
    """
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


# ═══════════════════════════════════════════
#  龙虎榜分析
# ═══════════════════════════════════════════

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
        print(f"  [scanner_factors] lhb failed: {e}", file=sys.stderr)
    return scores, lhb_data


# ═══════════════════════════════════════════
#  历史股性评分
# ═══════════════════════════════════════════

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
        print(f"  [scanner_factors] history failed: {e}", file=sys.stderr)
    return scores, raw_details
