#!/usr/bin/env python3
"""
Plan B — a-stock-data 增强: +北向资金 +融资融券 +研报评级 +涨停归因

评分链路:
  1. Plan A 的 9 因子 (复用 compute_factors)
  2. 4 个新因子 (北向/融资/研报/涨停归因) — a-stock-data 降级安全
  3. apply_weights: 13 因子归一化 + 大盘情绪温和系数
  4. score_danger_signals: 交叉惩罚
  5. build_stocks: 组装前端卡片 (复用 Plan A)

降级: a-stock-data 不可用时新增因子给中性分,不影响扫描主流程。
"""

PLAN_NAME = "B"
PLAN_DESC = "a-stock-data增强: +北向+融资+研报+涨停归因"

import pandas as pd
import numpy as np
import sys
import threading

from plans.plan_a import (
    compute_factors,
    _gen_auction_check,
    build_stocks as _build_stocks_a,
)


# ═══════════════════════════════════════════
#  Plan B 专属权重
# ═══════════════════════════════════════════

PLAN_B_WEIGHTS = {
    'seal': 18.0,
    'money': 10.0,
    'sector': 10.0,
    'tech': 6.0,
    'history': 4.0,
    'stock_sentiment': 8.0,
    'principal': 6.0,
    'north_flow': 6.0,
    'margin_ratio': 4.0,
    'inst_rating': 4.0,
    'limit_reason': 4.0,
}
PLAN_B_TOTAL = sum(PLAN_B_WEIGHTS.values())  # 80

# Plan B 因子满分 (新增因子)
PLAN_B_RAW_MAX = {
    'seal': 28.0, 'money': 20.0, 'sector': 12.0, 'tech': 10.0,
    'history': 6.0, 'stock_sentiment': 10.0, 'principal': 10.0,
    'north_flow': 6.0, 'margin_ratio': 4.0, 'inst_rating': 4.0, 'limit_reason': 4.0,
}


# ═══════════════════════════════════════════
#  新增因子计算 (a-stock-data 降级安全)
# ═══════════════════════════════════════════

def _compute_north_flow(df: pd.DataFrame, today_str: str = None) -> pd.Series:
    """
    北向资金净流入评分 (0-6)。
    优先用 a-stock-data 直连东财 push2,不可用时给中性 3 分。
    """
    scores = pd.Series(3.0, index=df.index)
    try:
        from datasource import get_north_flow_batch
        codes = [str(df.loc[idx, '代码']).strip().zfill(6) for idx in df.index]
        flows = get_north_flow_batch(codes, date_str=today_str)
        for idx, code in zip(df.index, codes):
            net = flows.get(code, 0)
            # 阶梯: 净流入>1亿=6, >5千万=5, >1千万=4, >0=3.5, 0=3, 净流出=2
            if net > 1e8: scores[idx] = 6.0
            elif net > 5e7: scores[idx] = 5.0
            elif net > 1e7: scores[idx] = 4.0
            elif net > 0: scores[idx] = 3.5
            elif net == 0: scores[idx] = 3.0
            else: scores[idx] = 2.0
    except Exception as e:
        print(f"  [PlanB] 北向资金不可用(降级中性): {e}", file=sys.stderr)
    return scores


def _compute_margin_ratio(df: pd.DataFrame, today_str: str = None) -> pd.Series:
    """
    融资余额/流通市值 评分 (0-4)。
    适度杠杆(2-5%)看多信号,过高(>10%)风险,过低(<1%)无杠杆动力。
    """
    scores = pd.Series(2.0, index=df.index)
    try:
        from datasource import get_margin_batch
        codes = [str(df.loc[idx, '代码']).strip().zfill(6) for idx in df.index]
        margins = get_margin_batch(codes)
        for idx, code in zip(df.index, codes):
            ratio = margins.get(code, 0)
            if 2 <= ratio < 5: scores[idx] = 4.0       # 黄金杠杆区间
            elif 1 <= ratio < 2 or 5 <= ratio < 8: scores[idx] = 3.0
            elif 8 <= ratio <= 10: scores[idx] = 2.0    # 偏高风险
            elif ratio > 10: scores[idx] = 1.0           # 过度杠杆
            else: scores[idx] = 1.5                       # 无杠杆动力
    except Exception as e:
        print(f"  [PlanB] 融资融券不可用(降级中性): {e}", file=sys.stderr)
    return scores


def _compute_inst_rating(df: pd.DataFrame) -> pd.Series:
    """
    机构研报评级评分 (0-4)。
    近30天评级上调=4, 维持买入=3, 首次覆盖=3, 下调=1, 无覆盖=2。
    """
    scores = pd.Series(2.0, index=df.index)
    try:
        from datasource import get_rating_batch
        codes = [str(df.loc[idx, '代码']).strip().zfill(6) for idx in df.index]
        ratings = get_rating_batch(codes)
        rating_score = {'上调': 4, '买入': 3, '增持': 3, '首次': 3,
                       '中性': 2, '持有': 2, '减持': 1, '卖出': 0}
        for idx, code in zip(df.index, codes):
            r = ratings.get(code, '')
            scores[idx] = rating_score.get(r, 2.0)
    except Exception as e:
        print(f"  [PlanB] 机构研报不可用(降级中性): {e}", file=sys.stderr)
    return scores


def _compute_limit_reason(df: pd.DataFrame) -> pd.Series:
    """
    涨停原因归因质量评分 (0-4)。
    强题材(政策/产业突破)=4, 公告利好=3, 板块跟风=2, 无原因/杂项=1。
    """
    scores = pd.Series(2.0, index=df.index)
    try:
        from datasource import get_limit_reason_batch
        codes = [str(df.loc[idx, '代码']).strip().zfill(6) for idx in df.index]
        reasons = get_limit_reason_batch(codes)
        quality = {'政策': 4, '产业': 4, '突破': 4, '公告': 3,
                  '业绩': 3, '重组': 3, '跟风': 2, '补涨': 2,
                  '超跌': 1, '次新': 1}
        for idx, code in zip(df.index, codes):
            r = reasons.get(code, '')
            score = 2.0
            for kw, s in quality.items():
                if kw in str(r):
                    score = float(s)
                    break
            scores[idx] = score
    except Exception as e:
        print(f"  [PlanB] 涨停归因不可用(降级中性): {e}", file=sys.stderr)
    return scores


# ═══════════════════════════════════════════
#  Plan B 加权
# ═══════════════════════════════════════════

def apply_weights_plan_b(seal_scores, money_scores, sector_scores, tech_scores,
                          history_scores, sentiment_series, stock_sent_scores,
                          principal_scores, north_flow, margin_ratio,
                          inst_rating, limit_reason) -> pd.Series:
    """Plan B 13 因子加权归一化到百分制"""
    w = PLAN_B_WEIGHTS
    rmax = PLAN_B_RAW_MAX

    weighted = (
        seal_scores * (w['seal'] / rmax['seal']) +
        money_scores * (w['money'] / rmax['money']) +
        sector_scores * (w['sector'] / rmax['sector']) +
        tech_scores * (w['tech'] / rmax['tech']) +
        history_scores * (w['history'] / rmax['history']) +
        stock_sent_scores * (w['stock_sentiment'] / rmax['stock_sentiment']) +
        principal_scores * (w['principal'] / rmax['principal']) +
        north_flow * (w['north_flow'] / rmax['north_flow']) +
        margin_ratio * (w['margin_ratio'] / rmax['margin_ratio']) +
        inst_rating * (w['inst_rating'] / rmax['inst_rating']) +
        limit_reason * (w['limit_reason'] / rmax['limit_reason'])
    )
    base_scores = weighted / max(1, PLAN_B_TOTAL) * 100

    # 大盘情绪温和系数 (同 Plan A: ×0.85~×1.15)
    if isinstance(sentiment_series, pd.Series):
        s_val = float(sentiment_series.iloc[0])
    else:
        s_val = float(sentiment_series)
    mult = np.clip(1.0 + (s_val - 5.0) * 0.03, 0.85, 1.15)

    return base_scores * mult


# ═══════════════════════════════════════════
#  评分聚合 (Plan B 版)
# ═══════════════════════════════════════════

def compute_all_factors(filtered, scoring_base, fund_df, principal, today_str=None):
    """
    计算所有 13 个因子: Plan A 9 因子 + Plan B 4 因子。
    scoring_base: 因子归一化基准集 (>=filtered, 比 filtered 更全)
    """
    # Plan A 的 9 因子在 scoring_base 上计算以保证归一化稳定
    factors_a = compute_factors(scoring_base if len(scoring_base) > len(filtered) else filtered,
                                fund_df, principal)

    # Plan B 新增 4 因子在 scoring_base 上计算
    base = scoring_base if len(scoring_base) > len(filtered) else filtered
    factors_b = {
        'north_flow': _compute_north_flow(base, today_str),
        'margin_ratio': _compute_margin_ratio(base, today_str),
        'inst_rating': _compute_inst_rating(base),
        'limit_reason': _compute_limit_reason(base),
    }

    # 缩到 filtered 索引
    if len(scoring_base) > len(filtered):
        common_idx = filtered.index.intersection(scoring_base.index)
        for fdict in [factors_a, factors_b]:
            for k in list(fdict.keys()):
                v = fdict[k]
                if hasattr(v, 'loc') and len(v) > 0:
                    fdict[k] = v.reindex(common_idx).fillna(
                        v.median() if (hasattr(v, 'median') and not v.empty and v.notna().any()) else 3.0)

    # 合并
    factors_a.update(factors_b)
    return factors_a


def apply_scores(filtered, factors, sentiment_score, history_scores,
                 lhb_bonus, today_str):
    """Plan B 加权 + 危险信号"""
    from scanner import score_danger_signals

    money = factors['money']
    if hasattr(lhb_bonus, 'loc') and not lhb_bonus.empty:
        money = (money + lhb_bonus.loc[filtered.index].reindex(
            money.index, fill_value=0)).clip(upper=20.0)
    else:
        money = money.clip(upper=20.0)

    sentiment_series = pd.Series(float(sentiment_score), index=filtered.index)
    h_scores = history_scores.loc[filtered.index] if hasattr(
        history_scores, 'loc') and len(filtered.index) > 0 \
        else pd.Series(2.5, index=filtered.index)

    sector_merged = (factors['sector_res'] + factors['sector_mom']) / 2.0

    base = apply_weights_plan_b(
        factors['seal'], money, sector_merged,
        factors['tech'], h_scores, sentiment_series,
        factors['stock_sentiment'], factors['principal'],
        factors['north_flow'], factors['margin_ratio'],
        factors['inst_rating'], factors['limit_reason'],
    )

    danger_penalty, danger_flags = score_danger_signals(
        filtered, factors['raw_money'], today_str)
    total = (base + danger_penalty).clip(lower=0)

    return total, base, danger_flags, PLAN_B_WEIGHTS


# ═══════════════════════════════════════════
#  组装前端数据 (扩展 Plan A)
# ═══════════════════════════════════════════

def build_stocks(filtered, factors, total_scores, base_scores, danger_flags,
                 sentiment_score, history_scores, pool=None):
    """在 Plan A 的 build_stocks 基础上追加 Plan B 专属字段"""
    stocks = _build_stocks_a(filtered, factors, total_scores, base_scores,
                             danger_flags, sentiment_score, history_scores, pool)
    # 追加新因子到已有 stocks（O(1) dict 查找）
    code_to_idx = {str(filtered.loc[i, '代码']).strip().zfill(6): i for i in filtered.index}
    for s in stocks:
        idx = code_to_idx.get(s['code'])
        if idx is not None:
            s['north_flow_score'] = round(float(factors['north_flow'].get(idx, 3)), 1)
            s['margin_score'] = round(float(factors['margin_ratio'].get(idx, 2)), 1)
            s['inst_rating_score'] = round(float(factors['inst_rating'].get(idx, 2)), 1)
            s['limit_reason_score'] = round(float(factors['limit_reason'].get(idx, 2)), 1)
    return stocks


# ═══════════════════════════════════════════
#  主入口: score()
# ═══════════════════════════════════════════

def score(inputs: dict) -> dict:
    """
    Plan B 评分主入口 — 接口与 Plan A 完全一致。

    inputs 必须包含:
        filtered, scoring_base, fund_df, sentiment_score,
        sentiment_level, sentiment_detail, sentiment_ok,
        history_scores, lhb_bonus, today_str, pool, principal

    返回 dict (与 Plan A 相同结构):
        stocks, df, seal_scores, money_scores, raw_money,
        sector_mom, sector_res, tech_scores, history_scores,
        buyability_scores, stock_sent_scores, principal_scores,
        sentiment_score, sentiment_level, sentiment_detail,
        sentiment_ok, date,
        north_flow, margin_ratio, inst_rating, limit_reason  # Plan B 新增
    """
    filtered = inputs['filtered']
    scoring_base = inputs.get('scoring_base', filtered)
    fund_df = inputs['fund_df']
    sentiment_score = inputs['sentiment_score']
    sentiment_level = inputs['sentiment_level']
    sentiment_detail = inputs['sentiment_detail']
    sentiment_ok = inputs['sentiment_ok']
    history_scores = inputs['history_scores']
    lhb_bonus = inputs['lhb_bonus']
    today_str = inputs['today_str']
    pool = inputs['pool']
    principal = inputs['principal']

    # 1. 计算 13 因子 (在 scoring_base 上归一化)
    print("  [PlanB] 计算13因子 (9+北向+融资+研报+涨停归因)...",
          file=sys.stderr)
    factors = compute_all_factors(filtered, scoring_base, fund_df,
                                  principal, today_str)

    # 2. 加权 + 危险信号
    print("  [PlanB] 加权+危险信号...", file=sys.stderr)
    total_scores, base_scores, danger_flags, weights = apply_scores(
        filtered, factors, sentiment_score, history_scores,
        lhb_bonus, today_str)

    # 3. 组装 stocks
    print("  [PlanB] 组装TOP股票...", file=sys.stderr)
    stocks = build_stocks(filtered, factors, total_scores, base_scores,
                          danger_flags, sentiment_score, history_scores, pool)

    # 4. 后台回测（传给调权系统时只含 Plan A 兼容的因子，新因子暂不参与自动调权）
    try:
        from scanner import auto_verify_backtest
        from weight_manager import DEFAULT_WEIGHTS
        backtest_weights = {k: v for k, v in weights.items() if k in DEFAULT_WEIGHTS}
        threading.Thread(
            target=lambda: auto_verify_backtest(
                today_str, current_weights=backtest_weights),
            daemon=True).start()
    except Exception as e:
        print(f"  [回测] 启动失败: {e}", file=sys.stderr)

    return {
        'stocks': stocks,
        'df': filtered,
        'seal_scores': factors['seal'],
        'money_scores': factors['money'],
        'raw_money': factors['raw_money'],
        'sector_mom': factors['sector_mom'],
        'sector_res': factors['sector_res'],
        'tech_scores': factors['tech'],
        'history_scores': history_scores,
        'buyability_scores': factors['buyability'],
        'stock_sent_scores': factors['stock_sentiment'],
        'principal_scores': factors['principal'],
        # Plan B 新增
        'north_flow': factors['north_flow'],
        'margin_ratio': factors['margin_ratio'],
        'inst_rating': factors['inst_rating'],
        'limit_reason': factors['limit_reason'],
        # 大盘情绪
        'sentiment_score': sentiment_score,
        'sentiment_level': sentiment_level,
        'sentiment_detail': sentiment_detail,
        'sentiment_ok': sentiment_ok,
        'date': today_str,
    }
