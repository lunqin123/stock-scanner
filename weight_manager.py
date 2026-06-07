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

# Plan B 可调权因子 (所有16个因子都参与IC检验, 弱因子自动归零)
BACKTEST_FACTORS_B = [
    'seal', 'money', 'sector', 'tech', 'history',
    'stock_sentiment', 'principal',
    'seal_quality', 'sector_resonance', 'volume_ratio',
    'north_flow', 'margin_ratio', 'inst_rating', 'limit_reason',
]

# IC 阈值: |IC| < 此值 → 权重归零 (统计噪声)
IC_NOISE_THRESHOLD = 0.02

_WEIGHTS_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "weights.json"
)
_WEIGHTS_FILE_B = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "weights_b.json"
)


def load_weights(plan_name: str = 'A') -> dict:
    """加载权重。Plan A 用 weights.json, Plan B 用 weights_b.json, 无文件返回默认值"""
    path = _WEIGHTS_FILE_B if plan_name.upper() == 'B' else _WEIGHTS_FILE
    defaults = DEFAULT_WEIGHTS
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            weights = dict(defaults)
            weights.update({k: v for k, v in data.items() if k in defaults})
            return weights
    except Exception as e:
        print(f"  [weight_manager L47] failed: {e}", file=sys.stderr)
    return dict(defaults)


def save_weights(weights: dict, plan_name: str = 'A'):
    """持久化权重。Plan A → weights.json, Plan B → weights_b.json"""
    path = _WEIGHTS_FILE_B if plan_name.upper() == 'B' else _WEIGHTS_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(weights, f, ensure_ascii=False, indent=None, separators=(',', ':'))
    except Exception as e:
        print(f"  [WARN] 权重保存失败: {e}", file=sys.stderr)


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


def save_daily_correlations(correlations: dict, trading_date: str = None, plan_name: str = 'A'):
    """保存因子相关性到滚动缓存 (按 Plan 分组)。trading_date 用交易日而非日历日。"""
    if not correlations:
        return
    if trading_date:
        s = trading_date.replace('-', '')
        if len(s) == 8 and s.isdigit():
            trading_date = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    today_str = trading_date if trading_date else date.today().isoformat()
    try:
        data = _load_rolling_data()
        # 去重: 同日期+同Plan
        data = [d for d in data if not (d['date'] == today_str and d.get('plan', 'A') == plan_name)]
        data.append({'date': today_str, 'correlations': dict(correlations), 'plan': plan_name})
        data = data[-ROLLING_WINDOW * 6:]  # keep enough history
        os.makedirs(os.path.dirname(_ROLLING_FILE), exist_ok=True)
        with open(_ROLLING_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [weight_manager L212] failed: {e}", file=sys.stderr)


def _load_rolling_data() -> list:
    try:
        if os.path.exists(_ROLLING_FILE):
            with open(_ROLLING_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"  [weight_manager L221] failed: {e}", file=sys.stderr)
    return []


def get_rolling_progress(plan_name: str = 'A') -> str:
    """返回滚动窗口数据积累情况(按 Plan 分组, 口径与 daily_adjust_weights 一致)"""
    all_data = sorted(_load_rolling_data(), key=lambda d: d['date'])
    plan_data = [d for d in all_data if d.get('plan', 'A') == plan_name]
    recent = plan_data[-ROLLING_WINDOW:]
    label = f"Plan {plan_name}" if plan_name != 'A' else ""
    return f"回测数据 {label} {len(recent)}/{ROLLING_WINDOW} 天"


def daily_adjust_weights(current_weights: dict, lr: float = None, plan_name: str = 'A'):
    """
    IC/ICIR 驱动的每日调权。

    Plan B: ICIR 加权 + |IC| < 0.02 自动归零
    Plan A: 保持原有 delta-based 逻辑 (兼容)

    返回 (new_weights, summary_str)
    """
    if lr is None:
        lr = DAILY_LR

    all_data = sorted(_load_rolling_data(), key=lambda d: d['date'])
    all_data = [d for d in all_data if d.get('plan', 'A') == plan_name]
    if len(all_data) < 2:
        return None, f"  回测数据仅 {len(all_data)} 天，至少需要 2 天"

    recent = all_data[-ROLLING_WINDOW:]

    # 聚合各因子 IC 序列
    factor_vals = {}
    for entry in recent:
        for k, v in entry.get('correlations', {}).items():
            factor_vals.setdefault(k, []).append(v)

    # 计算 IC 均值 + ICIR
    ic_stats = {}
    for k, vals in factor_vals.items():
        if len(vals) < 2:
            continue
        ic_mean = float(np.mean(vals))
        ic_std = float(np.std(vals)) if len(vals) > 1 else 1.0
        icir = abs(ic_mean) / max(0.001, ic_std)
        ic_stats[k] = {
            'ic_mean': round(ic_mean, 4),
            'ic_std': round(ic_std, 4),
            'icir': round(icir, 2),
        }

    if plan_name.upper() == 'B':
        # ── Plan B: ICIR 加权 + 噪声剔除 ──
        from plans.plan_b import PLAN_B_WEIGHTS as defaults_b
        factor_list = BACKTEST_FACTORS_B
        defaults = defaults_b
    else:
        # ── Plan A: 保持原有 delta-based ──
        factor_list = BACKTEST_FACTORS
        defaults = DEFAULT_WEIGHTS

    # ICIR 加权
    valid_factors = [f for f in factor_list if f in ic_stats]
    if len(valid_factors) < 2:
        return None, f"  有效因子仅 {len(valid_factors)} 个，至少需要 2 个"

    # 噪声剔除: |IC| < 阈值 → 权重归零
    active_factors = []
    dropped = []
    for f in valid_factors:
        if abs(ic_stats[f]['ic_mean']) >= IC_NOISE_THRESHOLD:
            active_factors.append(f)
        else:
            dropped.append(f)

    if plan_name.upper() == 'B':
        # ICIR 加权分配
        total_icir = sum(ic_stats[f]['icir'] for f in active_factors)
        new_weights = {}
        for k in defaults:
            new_weights[k] = 0.0
        if total_icir > 0:
            for f in active_factors:
                new_weights[f] = round(defaults[f] * ic_stats[f]['icir'] / total_icir, 1)

        # 软钳制 [0.5×default, 1.5×default] 对非零因子
        for f in active_factors:
            lo = defaults[f] * 0.5
            hi = defaults[f] * 1.5
            new_weights[f] = max(lo, min(hi, new_weights[f]))

        # 保留 sentiment (情绪系数, 不参与加权) — Plan B 没有 sentiment 因子
        save_weights({k: v for k, v in new_weights.items() if v > 0}, plan_name='B')

        # 摘要
        lines = [f"  Plan B ICIR调权 ({len(recent)}天) | 有效{len(active_factors)}/总计{len(valid_factors)}"]
        for f in valid_factors:
            s = ic_stats[f]
            status = "+" if f in active_factors else "x"
            lines.append(f"    {status} {f}: IC={s['ic_mean']:+.3f} σ={s['ic_std']:.3f} ICIR={s['icir']:.1f}")
        if dropped:
            lines.append(f"  噪声剔除(IC<{IC_NOISE_THRESHOLD}): {', '.join(dropped)}")
        return new_weights, '\n'.join(lines)

    else:
        # Plan A: 保持原有 delta-based logic
        mean_corrs = {f: ic_stats[f]['ic_mean'] for f in valid_factors}
        mean_corr = float(np.mean([mean_corrs[f] for f in valid_factors]))

        deltas = {f: 0.0 for f in defaults}
        for factor in valid_factors:
            delta = lr * (mean_corrs[factor] - mean_corr) * defaults.get(factor, 1.0)
            deltas[factor] = delta

        new_weights = {}
        for k in defaults:
            w = current_weights.get(k, defaults.get(k, 0)) + deltas.get(k, 0)
            lo = defaults.get(k, 0) * 0.5
            hi = defaults.get(k, 0) * 1.5
            new_weights[k] = max(lo, min(hi, w)) if defaults.get(k, 0) > 0 else 0.0

        save_weights(new_weights, plan_name='A')

        corr_str = ' | '.join(f"{k}: {mean_corrs[k]:+.3f}" for k in valid_factors)
        changes = []
        for k in factor_list:
            if k in new_weights and k in current_weights:
                delta = new_weights[k] - current_weights[k]
                if abs(delta) > 0.01:
                    changes.append(f"{k}: {current_weights[k]:.0f}→{new_weights[k]:.0f} ({delta:+.1f})")
        if not changes:
            return new_weights, f"  权重无显著变化 ({len(recent)}天)"
        return new_weights, f"  每日调权 ({len(recent)}天): {corr_str}\n  {' | '.join(changes)}"
