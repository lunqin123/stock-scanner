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
    'seal': 7.0,       # 封板强度（降权：早盘板次日买不到）
    'tech': 16.0,      # 量价结构（换手区间，有效次日预测因子）
    'sector_res': 10.0,# 板块共振（今日板块涨停集中度）
    'sentiment': 25.0, # 市场情绪（核心独立因子）
    'sector_mom': 15.0,# 晋级预期（板块持续性）
    'history': 10.0,   # 历史股性（涨停频率有回测证据）
    'money': 5.0,      # 资金驱动（降权：超短线中预测力有限）
    'buyability': 12.0,# ↑开盘可行性（次日买得到为首位，权重从8→12）
}
TOTAL_WEIGHT = sum(DEFAULT_WEIGHTS.values())  # 100
BACKTEST_FACTORS = ['seal', 'sector_mom', 'tech']  # 回测中可调权的因子

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
_RAW_MAX = {'seal': 25.0, 'money': 20.0, 'sector_res': 8.0, 'sentiment': 10.0,
            'sector_mom': 12.0, 'tech': 10.0, 'history': 6.0, 'buyability': 12.0}
_RAW_TOTAL = sum(_RAW_MAX.values())  # 93


def apply_weights(seal_scores, money_scores, sector_res, sector_mom, buyability_scores,
                  tech_scores, history_scores, sentiment_score,
                  weights=None):
    """
    将原始分数用动态权重加权后归一化到百分制(0-100)。
    情绪因子改为乘法调节，而非加法：
      sentiment(5=中性) → 不调节
      sentiment(10=高潮) → ×1.3
      sentiment(0=冰点) → ×0.7
    这样在冰点行情下总分不会被情绪撑高，风控更严格。
    """
    w = weights if weights else DEFAULT_WEIGHTS

    # 不含情绪的基础加权得分
    weighted = (seal_scores * (w['seal'] / _RAW_MAX['seal']) +
                money_scores * (w['money'] / _RAW_MAX['money']) +
                sector_res * (w['sector_res'] / _RAW_MAX['sector_res']) +
                sector_mom * (w['sector_mom'] / _RAW_MAX['sector_mom']) +
                tech_scores * (w['tech'] / _RAW_MAX['tech']) +
                history_scores * (w['history'] / _RAW_MAX['history']) +
                buyability_scores * (w['buyability'] / _RAW_MAX['buyability']))
    base_scores = weighted / (TOTAL_WEIGHT - w['sentiment']) * 100

    # 情绪乘法调节 (0.7 ~ 1.3)
    if isinstance(sentiment_score, pd.Series):
        s_val = float(sentiment_score.iloc[0])
    else:
        s_val = float(sentiment_score)
    mult = np.clip(1.0 + (s_val - 5.0) * 0.06, 0.7, 1.3)

    return base_scores * mult


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
    save_weights(new_weights)

    # 摘要
    corr_str = " | ".join(f"{k}: {mean_corrs[k]:+.3f}" for k in ['seal', 'sector_mom', 'tech'] if k in mean_corrs)
    changes = []
    for k in DEFAULT_WEIGHTS:
        delta = new_weights[k] - current_weights[k]
        changes.append(f"{k}: {current_weights[k]:.0f}→{new_weights[k]:.0f} ({delta:+.1f})")
    summary = f"  周调权 ({len(week_data)}天均值): {corr_str}\n  {' | '.join(changes)}"

    return new_weights, summary
