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
PLAN_SOURCES = [
    'north_flow',            # 北向资金 (同花顺 hsgtApi → akshare)
    'margin_akshare',        # 融资融券 (akshare 沪深两市)
    'inst_rating',           # 机构研报 (akshare → 东财 reportapi)
    'limit_reason',          # 涨停归因 (同花顺热点 API)
    'industry_fund_flow',    # 行业资金流向 (akshare)
]

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
    'seal_quality': 3.0,
    'sector_resonance': 3.0,
    'volume_ratio': 2.0,
    'north_flow': 6.0,
    'margin_ratio': 4.0,
    'inst_rating': 4.0,
    'limit_reason': 4.0,
}
PLAN_B_TOTAL = sum(PLAN_B_WEIGHTS.values())  # 88

PLAN_B_RAW_MAX = {
    'seal': 28.0, 'money': 20.0, 'sector': 12.0, 'tech': 10.0,
    'history': 6.0, 'stock_sentiment': 10.0, 'principal': 10.0,
    'seal_quality': 3.0, 'sector_resonance': 3.0, 'volume_ratio': 2.0,
    'north_flow': 6.0, 'margin_ratio': 4.0, 'inst_rating': 4.0, 'limit_reason': 4.0,
}


# ═══════════════════════════════════════════
#  新增因子计算 (a-stock-data 降级安全)
# ═══════════════════════════════════════════

def _df_to_map(src_df, key_col='code', val_col=None):
    """DataFrame → {code: value} dict, 降级安全"""
    if src_df is None or not hasattr(src_df, 'empty') or src_df.empty:
        return {}
    if key_col not in src_df.columns:
        return {}
    if val_col and val_col in src_df.columns:
        return dict(zip(src_df[key_col].astype(str).str.zfill(6), src_df[val_col]))
    return {}


def _compute_north_flow(df: pd.DataFrame, north_flow_df=None, north_market_df=None) -> pd.Series:
    """
    北向资金评分 (0-6)。
    优先个股数据, 其次市场总览(北向净流入日全体+1), 无数据给中性 3 分。
    """
    scores = pd.Series(3.0, index=df.index)

    # 1) 尝试个股级数据 (a-stock-data)
    flow_map = _df_to_map(north_flow_df, 'code', 'net_flow_yuan')
    if not flow_map:
        flow_map = _df_to_map(north_flow_df, '代码', '净流入') if north_flow_df is not None and not north_flow_df.empty else {}
    if flow_map:
        for idx in df.index:
            code = str(df.loc[idx, '代码']).strip().zfill(6)
            net = flow_map.get(code, 0)
            if net > 1e8: scores[idx] = 6.0
            elif net > 5e7: scores[idx] = 5.0
            elif net > 1e7: scores[idx] = 4.0
            elif net > 0: scores[idx] = 3.5
            elif net == 0: scores[idx] = 3.0
            else: scores[idx] = 2.0
        return scores

    # 2) 退而求其次: 市场总览 (北向净流入日 = 整体偏多)
    if north_market_df is not None and not north_market_df.empty:
        try:
            total_net = 0.0
            for c in north_market_df.columns:
                cl = str(c).lower()
                # 同花顺 hsgtApi: hgt_yi, sgt_yi (亿元)
                # akshare: 沪股通净买入, 深股通净买入
                if cl in ('hgt_yi', 'sgt_yi') or '净买入' in str(c) or '净流入' in str(c):
                    last_val = north_market_df[c].dropna()
                    if len(last_val) > 0:
                        total_net += float(last_val.iloc[-1])
            if total_net > 0:
                scores[:] = 3.8
            elif total_net < 0:
                scores[:] = 2.2
        except Exception:
            pass
    return scores


def _compute_margin_ratio(df: pd.DataFrame, margin_df=None) -> pd.Series:
    """
    融资融券评分 (0-4)。兼容两种数据格式:
    - a-stock-data: code + ratio_pct 列
    - akshare: code + margin_balance 列 (需要自己算 ratio = margin_balance / 流通市值)
    """
    scores = pd.Series(2.0, index=df.index)
    if margin_df is None or margin_df.empty:
        return scores

    # 尝试直接 ratio
    margin_map = _df_to_map(margin_df, 'code', 'ratio_pct')

    # 尝试 akshare 格式: 有 margin_balance 列, 自己算 ratio
    if not margin_map and 'margin_balance' in margin_df.columns and 'code' in margin_df.columns:
        cap_col = '流通市值' if '流通市值' in df.columns else None
        if cap_col:
            for idx in df.index:
                try:
                    code = str(df.loc[idx, '代码']).strip().zfill(6)
                    row = margin_df[margin_df['code'].astype(str).str.zfill(6) == code]
                    if not row.empty:
                        balance = float(row.iloc[0]['margin_balance'])
                        cap = float(df.loc[idx, cap_col])
                        if cap > 0:
                            ratio = balance / cap * 100
                            margin_map[code] = ratio
                except Exception:
                    pass

    if not margin_map:
        return scores

    for idx in df.index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        ratio = margin_map.get(code, 0)
        if 2 <= ratio < 5: scores[idx] = 4.0
        elif 1 <= ratio < 2 or 5 <= ratio < 8: scores[idx] = 3.0
        elif 8 <= ratio <= 10: scores[idx] = 2.0
        elif ratio > 10: scores[idx] = 1.0
        else: scores[idx] = 1.5
    return scores


def _compute_inst_rating(df: pd.DataFrame, inst_rating_df=None) -> pd.Series:
    """机构研报评级评分 (0-4)。"""
    scores = pd.Series(2.0, index=df.index)
    rating_map = _df_to_map(inst_rating_df, 'code', 'rating')
    if not rating_map:
        return scores
    rating_score = {'上调': 4, '买入': 3, '增持': 3, '首次': 3,
                   '中性': 2, '持有': 2, '减持': 1, '卖出': 0}
    for idx in df.index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        r = rating_map.get(code, '')
        scores[idx] = rating_score.get(r, 2.0)
    return scores


def _compute_limit_reason(df: pd.DataFrame, limit_reason_df=None) -> pd.Series:
    """
    涨停原因归因质量评分 (0-4)。
    基于同花顺热点归因标签: 标签越多=题材越丰富, 含当前热点主题=加分。
    """
    scores = pd.Series(2.0, index=df.index)
    reason_map = _df_to_map(limit_reason_df, 'code', 'reason')
    if not reason_map:
        return scores

    # 当前市场热点主题 (2026 Q2)
    hot_themes = [
        'AI', '算力', '半导体', '芯片', '机器人', '智能', '新能源', '固态电池',
        '低空', '航天', '军工', '创新药', '减肥药', '数据', '量子',
        '自动驾驶', '液冷', '铜箔', '消费电子', '光伏', '储能',
        '英伟达', '华为', '特斯拉', '苹果',
    ]

    for idx in df.index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        r = reason_map.get(code, '')
        if not r:
            continue

        # 拆分标签 (分隔符: + , /)
        import re
        tags = re.split(r'[+/,]', str(r))
        tags = [t.strip() for t in tags if t.strip()]
        n_tags = len(tags)

        # 检查热点匹配
        hot_hits = 0
        tag_lower = str(r).lower()
        for theme in hot_themes:
            if theme.lower() in tag_lower:
                hot_hits += 1

        # 评分: 标签数量 + 热点命中
        if hot_hits >= 3:
            scores[idx] = 4.0       # 多重热点共振
        elif hot_hits >= 2:
            scores[idx] = 3.5
        elif hot_hits >= 1:
            scores[idx] = 3.0       # 至少1个热点
        elif n_tags >= 4:
            scores[idx] = 2.5       # 标签丰富但非主流热点
        elif n_tags >= 2:
            scores[idx] = 2.0       # 普通
        else:
            scores[idx] = 1.5       # 标签稀少
    return scores


# ═══════════════════════════════════════════
#  Phase 1 零依赖因子 (从现有 DataFrame 衍生)
# ═══════════════════════════════════════════

def _compute_seal_quality(df: pd.DataFrame) -> pd.Series:
    """
    封板质量评分 (0-3): 炸板次数 + 封板时间。零依赖, 从现有列直接算。
    """
    scores = pd.Series(1.5, index=df.index)

    # 炸板次数=0 → +1.5
    if '炸板次数' in df.columns:
        zban = df['炸板次数'].fillna(0).astype(float)
        scores += np.where(zban == 0, 1.5, 0)

    # 封板时间在 10:00 前 → +1.5 (首次封板时间 < "100000")
    if '首次封板时间' in df.columns:
        st = df['首次封板时间'].fillna('999999').astype(str)
        scores += np.where(st.str[:2].astype(int) < 10, 1.5, 0)

    return scores.clip(0, 3.0)


def _compute_sector_fund_resonance(df: pd.DataFrame, raw_money: pd.Series,
                                     industry_fund_df=None) -> pd.Series:
    """
    板块资金共振 (0-3): 个股资金流向 vs 行业资金方向。零依赖(基础) + akshare(增强)。
    有 industry_fund_df 时优先用行业级主力净流入方向。
    """
    scores = pd.Series(1.5, index=df.index)
    ind_col = '所属行业' if '所属行业' in df.columns else (
        df.columns[15] if len(df.columns) > 15 else None)
    if ind_col is None:
        return scores

    # 行业级资金方向 (akshare 增强)
    sector_fund_dir = {}  # {行业名: net_flow}
    if industry_fund_df is not None and not industry_fund_df.empty:
        try:
            for _, row in industry_fund_df.iterrows():
                name = str(row.iloc[0]) if len(row) > 0 else ''
                net = 0.0
                for c in industry_fund_df.columns:
                    cl = str(c).lower()
                    if '净流入' in str(c) or '主力净流入' in str(c):
                        try: net = float(row[c])
                        except: pass
                if name:
                    sector_fund_dir[name] = net
        except Exception:
            pass

    # 个股级资金方向 (raw_money)
    try:
        money_series = raw_money.reindex(df.index, fill_value=0).astype(float) if raw_money is not None and not raw_money.empty else pd.Series(0.0, index=df.index)
        df_money = pd.DataFrame({
            'industry': df[ind_col].astype(str),
            'money': money_series,
        })
        sector_avg = df_money.groupby('industry')['money'].mean()
        for idx in df.index:
            ind = str(df.loc[idx, ind_col])
            mn = float(df_money.loc[idx, 'money']) if idx in df_money.index else 0
            # 行业方向: 优先 akshare 行业级数据, 回退到涨停池内个股均值
            if ind in sector_fund_dir:
                sa = sector_fund_dir[ind]
            else:
                sa = float(sector_avg.get(ind, 0))
            if mn > 0 and sa > 0:
                scores[idx] = 3.0
            elif mn > 0 and sa < 0:
                scores[idx] = 2.0
            elif mn < 0 and sa > 0:
                scores[idx] = 1.0
            else:
                scores[idx] = 0.5
    except Exception:
        pass
    return scores


def _compute_volume_ratio(df: pd.DataFrame, today_str: str = None) -> pd.Series:
    """
    量比评分 (0-2): 当日换手率 vs 同行业涨停股中位换手率。
    零依赖 — 不拉 akshare, 从当前涨停池直接算相对活跃度。
    """
    scores = pd.Series(1.0, index=df.index)
    turnover_col = '换手率' if '换手率' in df.columns else None
    if turnover_col is None:
        return scores

    today_turnover = df[turnover_col].fillna(0).astype(float)
    ind_col = '所属行业' if '所属行业' in df.columns else (
        df.columns[15] if len(df.columns) > 15 else None)

    try:
        if ind_col and len(df) >= 3:
            df_temp = pd.DataFrame({
                'industry': df[ind_col].astype(str),
                'turnover': today_turnover,
            })
            sector_median = df_temp.groupby('industry')['turnover'].median()
            for idx in df.index:
                ind = str(df.loc[idx, ind_col])
                med = sector_median.get(ind, today_turnover.median())
                if med > 0:
                    ratio = today_turnover[idx] / med
                    if ratio > 2.0: scores[idx] = 2.0
                    elif ratio > 1.5: scores[idx] = 1.5
                    elif ratio > 1.0: scores[idx] = 1.0
                    elif ratio > 0.5: scores[idx] = 0.5
                    else: scores[idx] = 0.3
        else:
            # 股票太少: 用全局中位做基准
            med = today_turnover.median()
            if med > 0:
                for idx in df.index:
                    ratio = today_turnover[idx] / med
                    if ratio > 2.0: scores[idx] = 2.0
                    elif ratio > 1.5: scores[idx] = 1.5
                    elif ratio > 1.0: scores[idx] = 1.0
                    else: scores[idx] = 0.5
    except Exception:
        pass
    return scores


# ═══════════════════════════════════════════
#  Plan B 加权
# ═══════════════════════════════════════════

def apply_weights_plan_b(seal_scores, money_scores, sector_scores, tech_scores,
                          history_scores, sentiment_series, stock_sent_scores,
                          principal_scores, seal_quality, sector_resonance,
                          volume_ratio, north_flow, margin_ratio,
                          inst_rating, limit_reason,
                          weights_b=None) -> pd.Series:
    """Plan B 16 因子加权归一化到百分制"""
    w = weights_b if weights_b else PLAN_B_WEIGHTS
    rmax = PLAN_B_RAW_MAX

    weighted = (
        seal_scores * (w.get('seal', 0) / rmax['seal']) +
        money_scores * (w.get('money', 0) / rmax['money']) +
        sector_scores * (w.get('sector', 0) / rmax['sector']) +
        tech_scores * (w.get('tech', 0) / rmax['tech']) +
        history_scores * (w.get('history', 0) / rmax['history']) +
        stock_sent_scores * (w.get('stock_sentiment', 0) / rmax['stock_sentiment']) +
        principal_scores * (w.get('principal', 0) / rmax['principal']) +
        seal_quality * (w.get('seal_quality', 0) / rmax['seal_quality']) +
        sector_resonance * (w.get('sector_resonance', 0) / rmax['sector_resonance']) +
        volume_ratio * (w.get('volume_ratio', 0) / rmax['volume_ratio']) +
        north_flow * (w.get('north_flow', 0) / rmax['north_flow']) +
        margin_ratio * (w.get('margin_ratio', 0) / rmax['margin_ratio']) +
        inst_rating * (w.get('inst_rating', 0) / rmax['inst_rating']) +
        limit_reason * (w.get('limit_reason', 0) / rmax['limit_reason'])
    )
    total_w = sum(w.get(k, 0) for k in rmax)
    base_scores = weighted / max(1, total_w) * 100

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

def compute_all_factors(filtered, scoring_base, fund_df, principal,
                        today_str=None, north_flow_df=None,
                        margin_ratio_df=None, inst_rating_df=None,
                        limit_reason_df=None,
                        margin_akshare_df=None, north_market_df=None,
                        industry_fund_df=None):
    """
    计算所有 16 个因子。
    scoring_base: 因子归一化基准集
    扩展数据在拉取阶段已缓存到 raw_scan_data.pkl.
    """
    # Plan A 的 9 因子在 scoring_base 上计算以保证归一化稳定
    factors_a = compute_factors(scoring_base if len(scoring_base) > len(filtered) else filtered,
                                fund_df, principal)

    # Plan B 新增因子: Phase 1 (零依赖) + Phase 2/3 (数据源)
    base = scoring_base if len(scoring_base) > len(filtered) else filtered
    raw_money_for_resonance = factors_a.get('raw_money')
    # 融资数据: 优先 akshare (margin_akshare_df 有 margin_balance), 其次 a-stock-data
    margin_df = margin_akshare_df if (margin_akshare_df is not None and not margin_akshare_df.empty) else margin_ratio_df
    # 行业资金流: 传给 sector_resonance 增强
    ind_fund_df = industry_fund_df if (industry_fund_df is not None and not industry_fund_df.empty) else None

    factors_b = {
        # Phase 1: 零依赖因子
        'seal_quality': _compute_seal_quality(base),
        'sector_resonance': _compute_sector_fund_resonance(base, raw_money_for_resonance, ind_fund_df),
        'volume_ratio': _compute_volume_ratio(base, today_str),
        # Phase 2/3: 数据源因子
        'north_flow': _compute_north_flow(base, north_flow_df, north_market_df),
        'margin_ratio': _compute_margin_ratio(base, margin_df),
        'inst_rating': _compute_inst_rating(base, inst_rating_df),
        'limit_reason': _compute_limit_reason(base, limit_reason_df),
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
    import weight_manager

    # 加载动态权重 (回测调权后的), 无保存文件时用默认 PLAN_B_WEIGHTS
    saved_weights = weight_manager.load_weights('B')
    # 检查 saved_weights 是否有 Plan B 的因子 (区别于 Plan A 的默认权重)
    if 'seal_quality' in saved_weights:
        weights_b = saved_weights
    else:
        weights_b = dict(PLAN_B_WEIGHTS)

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
        factors['seal_quality'], factors['sector_resonance'],
        factors['volume_ratio'],
        factors['north_flow'], factors['margin_ratio'],
        factors['inst_rating'], factors['limit_reason'],
        weights_b=weights_b,
    )

    danger_penalty, danger_flags = score_danger_signals(
        filtered, factors['raw_money'], today_str)
    total = (base + danger_penalty).clip(lower=0)

    return total, base, danger_flags, weights_b


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
            s['seal_quality_score'] = round(float(factors['seal_quality'].get(idx, 1.5) or 1.5), 1)
            s['sector_resonance_score'] = round(float(factors['sector_resonance'].get(idx, 1.5) or 1.5), 1)
            s['volume_ratio_score'] = round(float(factors['volume_ratio'].get(idx, 1.0) or 1.0), 1)
            s['north_flow_score'] = round(float(factors['north_flow'].get(idx, 3) or 3.0), 1)
            s['margin_score'] = round(float(factors['margin_ratio'].get(idx, 2) or 2.0), 1)
            s['inst_rating_score'] = round(float(factors['inst_rating'].get(idx, 2) or 2.0), 1)
            s['limit_reason_score'] = round(float(factors['limit_reason'].get(idx, 2) or 2.0), 1)
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
        及扩展数据 (north_flow, margin_ratio, inst_rating, limit_reason —
        拉取阶段由 plans.datasource 拉取并缓存到 raw_scan_data.pkl,
        不可用时为空 dict，各因子自动降级给默认分)

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
    # 扩展数据 (DataFrame, 拉取阶段缓存到 raw_scan_data.pkl, 无数据时为 None 或空)
    north_flow_df = inputs.get('north_flow')
    margin_ratio_df = inputs.get('margin_ratio')
    inst_rating_df = inputs.get('inst_rating')
    limit_reason_df = inputs.get('limit_reason')
    margin_akshare_df = inputs.get('margin_akshare')
    # 北向数据: 同花顺 hsgtApi 返回的既是 market 也是唯一可用数据
    north_market_df = inputs.get('north_flow_market', north_flow_df)
    industry_fund_df = inputs.get('industry_fund_flow')

    # 1. 计算 16 因子
    print("  [PlanB] 计算16因子...", file=sys.stderr)
    factors = compute_all_factors(filtered, scoring_base, fund_df,
                                  principal, today_str,
                                  north_flow_df=north_flow_df,
                                  margin_ratio_df=margin_ratio_df,
                                  inst_rating_df=inst_rating_df,
                                  limit_reason_df=limit_reason_df,
                                  margin_akshare_df=margin_akshare_df,
                                  north_market_df=north_market_df,
                                  industry_fund_df=industry_fund_df)

    # 2. 加权 + 危险信号
    print("  [PlanB] 加权+危险信号...", file=sys.stderr)
    total_scores, base_scores, danger_flags, weights = apply_scores(
        filtered, factors, sentiment_score, history_scores,
        lhb_bonus, today_str)

    # 3. 组装 stocks
    print("  [PlanB] 组装TOP股票...", file=sys.stderr)
    stocks = build_stocks(filtered, factors, total_scores, base_scores,
                          danger_flags, sentiment_score, history_scores, pool)

    # 4. 后台回测 — Plan B 全部 16 因子参与 ICIR 调权
    try:
        from scanner import auto_verify_backtest
        threading.Thread(
            target=lambda: auto_verify_backtest(
                today_str, current_weights=weights_b, plan_name='B'),
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
        # Plan B 新增 (Phase 1 零依赖 + Phase 2/3 数据源)
        'seal_quality': factors['seal_quality'],
        'sector_resonance': factors['sector_resonance'],
        'volume_ratio': factors['volume_ratio'],
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
