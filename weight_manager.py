#!/usr/bin/env python3
"""
评分权重管理器
每日根据回测结果自动调整评分权重，以 JSON 持久化到缓存目录。
学习率低(0.02)，基于近5日滚动相关性均值，避免单日波动过大。
"""
import json
import os
import sys

import pandas as pd
import numpy as np

DEFAULT_WEIGHTS = {
    'seal': 22.0,      # 封板强度（回测r=+0.126，唯一显著预测因子，seal20+黄金区均涨+5.9%）
    'tech': 6.0,       # 量价结构（简化为换手率评级，回测r=+0.027）
    'sector': 12.0,    # 板块合力（合并原sector_res+sector_mom，消除重复计算）
    'sentiment': 25.0, # 大盘情绪（系数调节，不参与加权和）
    'sector_res': 0.0, # DEPRECATED: 已合并到sector
    'sector_mom': 0.0, # DEPRECATED: 已合并到sector
    'history': 4.0,    # 历史股性（回测中为默认值，降权）
    'money': 12.0,     # 资金驱动（阶梯式分级，回测不可验证但实盘关键）
    'buyability': 0.0, # DEPRECATED: 降为纯过滤器(can_buy_filter)，不参与加权
    'stock_sentiment': 9.0,   # 个股情绪（资金态度+确定性+板块领先度）
    'principal_score': 6.0,   # 本金适配（提权，增强低价小市值标的区分度）
}
TOTAL_WEIGHT = sum(DEFAULT_WEIGHTS.values())  # 96

# 回测中可调权的因子
BACKTEST_FACTORS = ['seal', 'tech', 'sector', 'history']

_WEIGHTS_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "weights.json"
)


def load_weights() -> dict:
    """加载权重，无文件则返回默认值"""
    try:
        if os.path.exists(_WEIGHTS_FILE):
            with open(_WEIGHTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            weights = dict(DEFAULT_WEIGHTS)
            weights.update({k: v for k, v in data.items() if k in DEFAULT_WEIGHTS})
            return weights
    except Exception:
        pass
    return dict(DEFAULT_WEIGHTS)


def save_weights(weights: dict):
    """持久化权重"""
    try:
        os.makedirs(os.path.dirname(_WEIGHTS_FILE), exist_ok=True)
        with open(_WEIGHTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(weights, f, ensure_ascii=False, indent=None, separators=(',', ':'))
    except Exception as e:
        print(f"  [WARN] 权重保存失败: {e}", file=sys.stderr)


def adjust_weights(backtest_df: pd.DataFrame, current_weights: dict, lr: float = 0.05) -> dict:
    """
    根据回测结果调整权重。
    对每个回测因子计算其得分与次日涨幅的 Pearson 相关系数，
    高相关因子权重微升，低相关微降。

    backtest_df: backtest_score_prev 返回的 DataFrame，含 seal_factor / seal_mom_factor / tech_factor / 今日涨幅
    lr: 学习率，默认 0.05

    返回调整后的 weights dict 并自动持久化
    """
    weights = dict(current_weights)
    df = backtest_df.copy()

    factor_cols = [f"{f}_factor" for f in BACKTEST_FACTORS]
    needed = factor_cols + ['今日涨幅']
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f"  [WARN] 调整权重缺少列: {missing}", file=sys.stderr)
        return weights

    correlations = {}
    valid_factors = []

    for factor, col in zip(BACKTEST_FACTORS, factor_cols):
        scores = df[col].astype(float)
        returns = df['今日涨幅'].astype(float)

        # 跳过方差过小的列（全同分数无法计算相关系数）
        if scores.std() < 0.01 or returns.std() < 0.01:
            continue

        corr = scores.corr(returns)
        if pd.isna(corr):
            continue

        correlations[factor] = corr
        valid_factors.append(factor)

    if len(valid_factors) < 1:
        return weights  # 数据不足，不动

    mean_corr = np.mean([correlations[f] for f in valid_factors])

    # 计算 delta（仅对回测因子），未回测因子通过重归一化微调
    deltas = {f: 0.0 for f in DEFAULT_WEIGHTS}
    for factor in valid_factors:
        delta = lr * (correlations[factor] - mean_corr) * DEFAULT_WEIGHTS[factor]
        deltas[factor] = delta

    # 应用 delta
    new_weights = {}
    for k in DEFAULT_WEIGHTS:
        w = weights[k] + deltas[k]
        # 软钳制 [0.5×default, 1.5×default]
        lo = DEFAULT_WEIGHTS[k] * 0.5
        hi = DEFAULT_WEIGHTS[k] * 1.5
        new_weights[k] = max(lo, min(hi, w))

    # 重归一化: community 因子固定不变，其他因子在剩余空间内分配
    save_weights(new_weights)
    return new_weights


# 各因子原始满分 (与 scoring 函数实际最大值一致)
_RAW_MAX = {'seal': 28.0, 'money': 20.0, 'sector': 12.0, 'sentiment': 10.0,
            'sector_res': 8.0, 'sector_mom': 12.0, 'tech': 10.0, 'history': 6.0,
            'stock_sentiment': 10.0, 'principal_score': 10.0}


def apply_weights(seal_scores, money_scores, sector_scores, tech_scores,
                  history_scores, sentiment_score,
                  stock_sentiment_scores=None, principal_scores=None,
                  sector_res=None, sector_mom=None,  # DEPRECATED: 向后兼容
                  buyability_scores=None, weights=None):
    """
    7因子加权 + 大盘情绪温和系数。
    sector_res/sector_mom 已合并为 sector_scores，旧参数仅向后兼容。
    大盘情绪(sentiment)温和系数调节(×0.85~×1.15)。
    """
    if stock_sentiment_scores is None:
        stock_sentiment_scores = pd.Series(5.0, index=seal_scores.index)
    if principal_scores is None:
        principal_scores = pd.Series(5.0, index=seal_scores.index)
    # 向后兼容：如果传了sector_res/sector_mom但没传sector_scores，自动合并
    if sector_scores is None:
        if sector_res is not None and sector_mom is not None:
            sector_scores = (sector_res + sector_mom) / 2.0
        elif sector_res is not None:
            sector_scores = sector_res
        elif sector_mom is not None:
            sector_scores = sector_mom
        else:
            sector_scores = pd.Series(6.0, index=seal_scores.index)
    w = weights if weights else DEFAULT_WEIGHTS

    # 7因子加权（sector_res/sector_mom已合并为sector）
    non_sentiment = ['seal', 'money', 'sector', 'tech', 'history',
                     'stock_sentiment', 'principal_score']
    actual_sum = sum(w[k] for k in non_sentiment)
    weighted = (seal_scores * (w['seal'] / _RAW_MAX['seal']) +
                money_scores * (w['money'] / _RAW_MAX['money']) +
                sector_scores * (w['sector'] / _RAW_MAX['sector']) +
                tech_scores * (w['tech'] / _RAW_MAX['tech']) +
                history_scores * (w['history'] / _RAW_MAX['history']) +
                stock_sentiment_scores * (w['stock_sentiment'] / _RAW_MAX['stock_sentiment']) +
                principal_scores * (w['principal_score'] / _RAW_MAX['principal_score']))
    base_scores = weighted / max(1, actual_sum) * 100

    # 大盘情绪温和系数 (×0.85 ~ ×1.15, 缩小到±15%)
    if isinstance(sentiment_score, pd.Series):
        s_val = float(sentiment_score.iloc[0])
    else:
        s_val = float(sentiment_score)
    mult = np.clip(1.0 + (s_val - 5.0) * 0.03, 0.85, 1.15)

    return base_scores * mult


# ─── 滚动每日调权系统 ───
from datetime import date, timedelta

_ROLLING_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "rolling_correlations.json"
)

# 每日调权参数
DAILY_LR = 0.02       # 每日学习率（低，避免单日波动）
ROLLING_WINDOW = 5    # 滚动窗口：取最近N天相关性均值


def save_daily_correlations(correlations: dict, trading_date: str = None):
    """保存因子相关性到滚动缓存。trading_date 用交易日而非日历日，避免凌晨重复。"""
    if not correlations:
        return
    # 统一为 YYYY-MM-DD 格式
    if trading_date and len(trading_date) == 8:
        trading_date = trading_date[:4] + '-' + trading_date[4:6] + '-' + trading_date[6:8]
    today_str = trading_date if trading_date else date.today().isoformat()
    try:
        data = _load_rolling_data()
        data = [d for d in data if d['date'] != today_str]
        data.append({'date': today_str, 'correlations': dict(correlations)})
        data = data[-ROLLING_WINDOW * 6:]  # keep enough history
        os.makedirs(os.path.dirname(_ROLLING_FILE), exist_ok=True)
        with open(_ROLLING_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _load_rolling_data() -> list:
    try:
        if os.path.exists(_ROLLING_FILE):
            with open(_ROLLING_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []


def get_rolling_progress() -> str:
    """返回滚动窗口数据积累情况"""
    all_data = _load_rolling_data()
    today = date.today()
    recent = [d for d in all_data if d['date'] >= (today - timedelta(days=7)).isoformat()]
    return f"回测数据 {len(recent)}/{ROLLING_WINDOW} 天"


def daily_adjust_weights(current_weights: dict, lr: float = None):
    """
    每日调权：累积近 ROLLING_WINDOW 天的因子相关性均值。
    数据不足时跳过（至少需要 2 天）。
    返回 (new_weights, summary_str) 或 (None, None)
    """
    if lr is None:
        lr = DAILY_LR

    all_data = _load_rolling_data()
    if len(all_data) < 2:
        return None, f"  回测数据仅 {len(all_data)} 天，至少需要 2 天"

    # 取最近 ROLLING_WINDOW 天的数据
    recent = all_data[-ROLLING_WINDOW:]

    # 聚合各因子相关性均值
    factor_vals = {}
    for entry in recent:
        for k, v in entry.get('correlations', {}).items():
            factor_vals.setdefault(k, []).append(v)

    mean_corrs = {}
    for k, vals in factor_vals.items():
        mean_corrs[k] = round(float(np.mean(vals)), 4)

    valid_factors = [f for f in BACKTEST_FACTORS if f in mean_corrs]
    if len(valid_factors) < 2:
        return None, f"  有效因子仅 {len(valid_factors)} 个，至少需要 2 个"

    values = [mean_corrs[f] for f in valid_factors]
    mean_corr = float(np.mean(values))

    # 计算 delta：高相关的加权重，低相关的减权重
    deltas = {f: 0.0 for f in DEFAULT_WEIGHTS}
    for factor in valid_factors:
        delta = lr * (mean_corrs[factor] - mean_corr) * DEFAULT_WEIGHTS[factor]
        deltas[factor] = delta

    # 应用 delta + 软钳制 [0.5×default, 1.5×default]
    new_weights = {}
    for k in DEFAULT_WEIGHTS:
        w = current_weights[k] + deltas[k]
        lo = DEFAULT_WEIGHTS[k] * 0.5
        hi = DEFAULT_WEIGHTS[k] * 1.5
        new_weights[k] = max(lo, min(hi, w))

    save_weights(new_weights)

    # 摘要
    all_corr_strs = []
    for k in sorted(mean_corrs.keys()):
        all_corr_strs.append(f"{k}: {mean_corrs[k]:+.3f}")
    corr_str = " | ".join(all_corr_strs)
    changes = []
    for k in BACKTEST_FACTORS:
        if k in new_weights:
            delta = new_weights[k] - current_weights[k]
            if abs(delta) > 0.01:
                changes.append(f"{k}: {current_weights[k]:.0f}→{new_weights[k]:.0f} ({delta:+.1f})")
    if not changes:
        return None, f"  权重无显著变化 ({len(recent)}天, 均值相关{mean_corr:+.3f})"

    summary = f"  每日调权 ({len(recent)}天均值): {corr_str}\n  {' | '.join(changes)}"
    return new_weights, summary
