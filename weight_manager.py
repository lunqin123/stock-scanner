#!/usr/bin/env python3
"""
评分权重管理器
每日根据回测结果自动调整评分权重，以 JSON 持久化到缓存目录。
学习率低(0.05)，避免单日波动过大。
"""
import json
import os
import sys

import pandas as pd
import numpy as np

# ─── 默认权重（与当前硬编码值一致） ───
DEFAULT_WEIGHTS = {
    'seal': 28.0,     # 涨停强度（回测相关+0.386，最强因子）
    'money': 16.0,    # 资金面
    'sector': 20.0,   # 板块热度（A股板块联动效应极强）
    'tech': 10.0,     # 量价关系
    'history': 5.0,   # 历史股性
    'community': 7.0, # 舆情热评
    'principal': 8.0, # 本金适配（价格 + 流动性）
}
TOTAL_WEIGHT = sum(DEFAULT_WEIGHTS.values())  # 94
BACKTEST_FACTORS = ['seal', 'sector', 'tech']  # 回测中可用的因子

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

    backtest_df: backtest_score_prev 返回的 DataFrame，含 seal_factor / sector_factor / tech_factor / 今日涨幅
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
    comm_w = new_weights.get('community', DEFAULT_WEIGHTS.get('community', 7.0))
    # 从 total 中去掉 community，对剩余因子重归一
    remaining_total = TOTAL_WEIGHT - comm_w
    adj_sum = sum(new_weights[k] for k in new_weights if k != 'community')
    if adj_sum > 0 and abs(adj_sum - remaining_total) > 0.05:
        scale = remaining_total / adj_sum
        for k in new_weights:
            if k != 'community':
                new_weights[k] = round(new_weights[k] * scale, 1)
    new_weights['community'] = round(comm_w, 1)

    save_weights(new_weights)
    return new_weights


# 各因子原始满分 (与 scoring 函数实际最大值一致)
_RAW_MAX = {'seal': 25.0, 'money': 20.0, 'sector': 12.0, 'tech': 10.0, 'history': 6.0, 'community': 7.0, 'principal': 10.0}
_RAW_TOTAL = sum(_RAW_MAX.values())  # 90


def apply_weights(seal_scores, money_scores, sector_scores, tech_scores, history_scores,
                  community_scores=None, principal_scores=None, weights=None):
    """
    将原始分数用动态权重加权后归一化到百分制(0-100)。
    weights=None 时使用 DEFAULT_WEIGHTS。
    community_scores / principal_scores 可选。
    返回加权总分 Series。
    """
    w = weights if weights else DEFAULT_WEIGHTS
    weighted = (seal_scores * (w['seal'] / _RAW_MAX['seal']) +
                money_scores * (w['money'] / _RAW_MAX['money']) +
                sector_scores * (w['sector'] / _RAW_MAX['sector']) +
                tech_scores * (w['tech'] / _RAW_MAX['tech']) +
                history_scores * (w['history'] / _RAW_MAX['history']))
    if community_scores is not None:
        weighted += community_scores * (w['community'] / _RAW_MAX['community'])
    if principal_scores is not None:
        weighted += principal_scores * (w['principal'] / _RAW_MAX['principal'])
    total = weighted / TOTAL_WEIGHT * 100
    return total


# ─── 滚动周调权系统 ───
from datetime import date, timedelta

_ROLLING_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "rolling_correlations.json"
)


def save_daily_correlations(correlations: dict):
    """保存当日因子相关性到滚动缓存。周一至周四存数据，周五调权。"""
    if not correlations:
        return
    today_str = date.today().isoformat()
    try:
        data = _load_rolling_data()
        data = [d for d in data if d['date'] != today_str]
        data.append({'date': today_str, 'correlations': dict(correlations)})
        data = data[-30:]  # keep rolling window
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


def get_weekly_progress() -> str:
    """返回本周已积累几天数据（用于展示）"""
    today = date.today()
    if today.weekday() >= 5:
        return ""
    monday = today - timedelta(days=today.weekday())
    all_data = _load_rolling_data()
    week_days = [d for d in all_data if d['date'] >= monday.isoformat()]
    return f"本周回测数据 {len(week_days)}/5 天"


def weekly_adjust_weights(current_weights: dict, lr: float = 0.05):
    """
    周五统一调权：累积本周（周一至周五）的因子相关性均值。
    返回 (new_weights, summary_str) 或 (None, None)
    """
    today = date.today()
    if today.weekday() != 4:
        return None, None

    monday = today - timedelta(days=today.weekday())
    all_data = _load_rolling_data()
    week_data = [d for d in all_data if d['date'] >= monday.isoformat()]

    if len(week_data) < 3:
        return None, f"  本周仅 {len(week_data)} 天数据，不足 3 天，跳过周调权"

    # 聚合本周各因子相关性均值
    factor_vals = {}
    for entry in week_data:
        for k, v in entry.get('correlations', {}).items():
            factor_vals.setdefault(k, []).append(v)

    mean_corrs = {}
    for k, vals in factor_vals.items():
        mean_corrs[k] = round(float(np.mean(vals)), 4)

    valid_factors = [f for f in BACKTEST_FACTORS if f in mean_corrs]
    if len(valid_factors) < 1:
        return None, "  无有效因子数据"

    values = [mean_corrs[f] for f in valid_factors]
    mean_corr = float(np.mean(values))

    # 计算 delta（同 adjust_weights 逻辑）
    deltas = {f: 0.0 for f in DEFAULT_WEIGHTS}
    for factor in valid_factors:
        delta = lr * (mean_corrs[factor] - mean_corr) * DEFAULT_WEIGHTS[factor]
        deltas[factor] = delta

    # 应用 delta + 软钳制
    new_weights = {}
    for k in DEFAULT_WEIGHTS:
        w = current_weights[k] + deltas[k]
        lo = DEFAULT_WEIGHTS[k] * 0.5
        hi = DEFAULT_WEIGHTS[k] * 1.5
        new_weights[k] = max(lo, min(hi, w))

    # 重归一化（community 固定）
    comm_w = new_weights.get('community', DEFAULT_WEIGHTS.get('community', 7.0))
    remaining_total = TOTAL_WEIGHT - comm_w
    adj_sum = sum(new_weights[k] for k in new_weights if k != 'community')
    if adj_sum > 0 and abs(adj_sum - remaining_total) > 0.05:
        scale = remaining_total / adj_sum
        for k in new_weights:
            if k != 'community':
                new_weights[k] = round(new_weights[k] * scale, 1)
    new_weights['community'] = round(comm_w, 1)

    save_weights(new_weights)

    # 摘要
    corr_str = " | ".join(f"{k}: {mean_corrs[k]:+.3f}" for k in ['seal', 'sector', 'tech'] if k in mean_corrs)
    changes = []
    for k in DEFAULT_WEIGHTS:
        delta = new_weights[k] - current_weights[k]
        changes.append(f"{k}: {current_weights[k]:.0f}→{new_weights[k]:.0f} ({delta:+.1f})")
    summary = f"  周调权 ({len(week_data)}天均值): {corr_str}\n  {' | '.join(changes)}"

    return new_weights, summary
