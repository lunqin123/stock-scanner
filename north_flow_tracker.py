#!/usr/bin/env python3
"""
北向资金日内追踪 + 市场方向信号 (North-bound Flow Tracker)

基于主力资金理论：北向资金是唯一盘中实时可追踪的"聪明钱"，
其流向和持续性是最有效的日内方向指标。

功能:
  1. 获取当日北向资金累计净流入 (沪股通+深股通)
  2. 判断北向方向 (持续流入/流出/震荡)
  3. 输出做T确认信号 (盘中方向判断辅助)
  4. Plan A 北向因子 (给涨停评分系统提供市场级偏多/偏空加成)

数据源 (降级链):
  - 同花顺 hsgtApi (实时分钟级) → plans/datasource.py:source_north_flow
  - akshare stock_hsgt_north_net_flow_in_em (日级汇总)
  - 降级到无数据 = 中性信号

用法:
    from north_flow_tracker import get_north_flow_signal
    signal = get_north_flow_signal()
    print(signal['direction'], signal['cumulative_net'])
"""

import sys
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple

import pandas as pd
import numpy as np

from scanner_utils import _CST, get_market_status

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ═══════════════════════════════════════════
#  核心: 北向资金获取 + 方向判断
# ═══════════════════════════════════════════

def _fetch_north_flow_minute() -> Optional[pd.DataFrame]:
    """
    获取北向资金分钟级数据 (同花顺 hsgtApi)。
    返回: DataFrame(time, hgt_yi, sgt_yi) 或 None
    """
    # 方案1: 复用 plans/datasource.py 中的同花顺直连
    try:
        from plans.datasource import source_north_flow
        df = source_north_flow(date_str='')
        if df is not None and not df.empty:
            return df
    except Exception:
        pass

    # 方案2: akshare 日级汇总 (无分钟细节但能拿到总量)
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em()
        if df is not None and not df.empty:
            return df
    except Exception:
        pass

    return None


def get_north_flow_signal(verbose: bool = False) -> dict:
    """
    获取北向资金信号，输出方向 + 做T建议。

    Returns:
        dict with:
        - cumulative_net: 当日累计净流入 (亿元)
        - hgt_net: 沪股通净流入 (亿元)
        - sgt_net: 深股通净流入 (亿元)
        - direction: '持续流入' | '小幅流入' | '震荡' | '小幅流出' | '持续流出'
        - direction_score: -5~+5 (正=流入偏多)
        - trend_30min: 最近30分钟趋势 '加速流入' | '转流入' | '持平' | '转流出' | '加速流出'
        - signal: '偏多' | '偏空' | '中性'
        - t0_advice: 做T建议
        - summary: 一句话总结
    """
    market_status = get_market_status()

    # 非交易时段返回空信号
    if market_status not in ('trading', 'lunch'):
        return {
            'cumulative_net': 0, 'hgt_net': 0, 'sgt_net': 0,
            'direction': '无数据',
            'direction_score': 0, 'trend_30min': '无数据',
            'signal': '中性',
            't0_advice': '非交易时段',
            'summary': f'当前{market_status}, 北向资金暂停交易',
            'sentiment_bonus': 0.0,
        }

    df = _fetch_north_flow_minute()
    if df is None or df.empty:
        return {
            'cumulative_net': 0, 'hgt_net': 0, 'sgt_net': 0,
            'direction': '无数据', 'direction_score': 0,
            'trend_30min': '无数据', 'signal': '中性',
            't0_advice': '北向数据不可用, 参考其他指标',
            'summary': '北向资金数据暂不可用',
            'sentiment_bonus': 0.0,
        }

    # ── 解析数据 ──
    # 同花顺 hsgtApi 格式: time[], hgt_yi[], sgt_yi[]
    if 'time' in df.columns and 'hgt_yi' in df.columns:
        # 分钟级数据
        hgt_vals = df['hgt_yi'].dropna()
        sgt_vals = df['sgt_yi'].dropna() if 'sgt_yi' in df.columns else pd.Series([0])
        hgt_latest = float(hgt_vals.iloc[-1]) if len(hgt_vals) > 0 else 0
        sgt_latest = float(sgt_vals.iloc[-1]) if len(sgt_vals) > 0 else 0
        cumulative = hgt_latest + sgt_latest

        # 30分钟趋势: 最近30分钟变化 (分钟级数据每点间隔约1分钟)
        trend_30 = _calc_minute_trend(hgt_vals, sgt_vals, window=30)
    elif '日期' in df.columns or 'date' in df.columns:
        # akshare 日级汇总格式
        net_col = None
        for c in df.columns:
            if '净流入' in str(c):
                net_col = c
                break
        if net_col:
            cumulative = float(df[net_col].iloc[-1]) / 1e8  # 转亿
        else:
            cumulative = 0
        hgt_latest = cumulative * 0.55  # 沪股通通常占55%
        sgt_latest = cumulative * 0.45
        trend_30 = '无分钟数据'
    else:
        cumulative = 0
        hgt_latest = 0
        sgt_latest = 0
        trend_30 = '无分钟数据'

    # ── 方向判断 ──
    if cumulative > 80:
        direction = '持续流入'
        direction_score = 5
    elif cumulative > 50:
        direction = '持续流入'
        direction_score = 4
    elif cumulative > 20:
        direction = '小幅流入'
        direction_score = 2
    elif cumulative > 5:
        direction = '小幅流入'
        direction_score = 1
    elif cumulative > -5:
        direction = '震荡'
        direction_score = 0
    elif cumulative > -20:
        direction = '小幅流出'
        direction_score = -1
    elif cumulative > -50:
        direction = '小幅流出'
        direction_score = -2
    elif cumulative > -80:
        direction = '持续流出'
        direction_score = -4
    else:
        direction = '持续流出'
        direction_score = -5

    # ── 信号 ──
    if direction_score >= 2:
        signal = '偏多'
    elif direction_score <= -2:
        signal = '偏空'
    else:
        signal = '中性'

    # ── 做T建议 ──
    if signal == '偏多':
        if trend_30 and '流入' in str(trend_30):
            t0_advice = '北向持续流入+30分钟加速 → 适合做多T，回调低吸不追高'
        else:
            t0_advice = '北向流入但短线转弱 → 做多T注意节奏，优先低吸'
    elif signal == '偏空':
        if trend_30 and '流出' in str(trend_30):
            t0_advice = '北向持续流出+30分钟加速 → 谨慎做多，做空T为主，等待尾盘'
        else:
            t0_advice = '北向流出 → 减少操作，持币观望为主'
    else:
        t0_advice = '北向震荡 → 个股为主，不做方向性博弈'

    # ── 情绪加成 (用于 Plan A scoring) ──
    # 映射 direction_score (-5~+5) → sentiment_bonus (-2~+2)
    sentiment_bonus = round(direction_score / 5.0 * 2.0, 1)

    result = {
        'cumulative_net': round(cumulative, 1),
        'hgt_net': round(hgt_latest, 1),
        'sgt_net': round(sgt_latest, 1),
        'direction': direction,
        'direction_score': direction_score,
        'trend_30min': trend_30,
        'signal': signal,
        't0_advice': t0_advice,
        'sentiment_bonus': sentiment_bonus,
        'summary': f"北向{direction}({cumulative:+.1f}亿) | 30分钟{trend_30} | {signal} | {t0_advice}",
    }

    if verbose:
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"  北向资金实时追踪 | {datetime.now(_CST).strftime('%H:%M')}", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)
        print(f"  累计净流入: {cumulative:+.1f}亿 (沪{hgt_latest:+.1f} / 深{sgt_latest:+.1f})", file=sys.stderr)
        print(f"  方向: {direction} | 信号: {signal} | 情绪加成: {sentiment_bonus:+.1f}", file=sys.stderr)
        print(f"  30分钟趋势: {trend_30}", file=sys.stderr)
        print(f"  做T建议: {t0_advice}", file=sys.stderr)

    return result


def _calc_minute_trend(hgt_vals: pd.Series, sgt_vals: pd.Series,
                       window: int = 30) -> str:
    """计算最近N分钟的北向资金趋势"""
    try:
        # 计算每分钟净流入变化
        all_net = hgt_vals + (sgt_vals if sgt_vals is not None else 0)
        if len(all_net) < window:
            window = max(2, len(all_net))

        recent = all_net.iloc[-window:]
        earlier = all_net.iloc[-window*2:-window] if len(all_net) >= window*2 else all_net.iloc[:len(all_net)//2]

        recent_mean = float(recent.mean())
        earlier_mean = float(earlier.mean()) if len(earlier) > 0 else recent_mean

        diff = recent_mean - earlier_mean

        if diff > 1.0:     return '加速流入'
        elif diff > 0.2:   return '转流入'
        elif diff > -0.2:  return '持平'
        elif diff > -1.0:  return '转流出'
        else:              return '加速流出'
    except Exception:
        return '无法计算'


# ═══════════════════════════════════════════
#  Plan A 集成: 北向因子评分函数
# ═══════════════════════════════════════════

def score_north_flow_factor(df: pd.DataFrame = None) -> Tuple[pd.Series, dict]:
    """
    Plan A 北向因子: 市场级北向资金方向 + 强度, 对所有标的统一评分。

    返回: (scores: pd.Series(0-10), metadata: dict)
    - 所有标的获得相同的北向分数 (因为是市场级数据)
    - 用于 Plan A apply_weights 加权
    """
    signal = get_north_flow_signal()

    # direction_score: -5 ~ +5 → 映射到 0-10 分
    raw_score = 5.0 + signal['direction_score']  # 0-10

    if df is not None and not df.empty:
        scores = pd.Series(raw_score, index=df.index)
    else:
        scores = pd.Series([raw_score])

    metadata = {
        'north_direction': signal['direction'],
        'north_cumulative_net': signal['cumulative_net'],
        'north_signal': signal['signal'],
    }

    return scores, metadata


# ═══════════════════════════════════════════
#  日级批处理: 前N日北向流向序列
# ═══════════════════════════════════════════

def get_north_flow_history(days: int = 5) -> list:
    """
    获取最近N个交易日的北向资金流向序列。
    用于判断北向是否在持续流入/流出 (市场状态分类器使用)。
    返回: [{date, net_flow_yi, direction}, ...]
    """
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em()
        if df is None or df.empty:
            return []

        date_col = None
        net_col = None
        for c in df.columns:
            if '日期' in str(c) or 'date' in str(c).lower():
                date_col = c
            if '净流入' in str(c):
                net_col = c

        if date_col is None or net_col is None:
            return []

        history = []
        for i in range(min(days, len(df))):
            row = df.iloc[-(i+1)]
            net_val = float(row[net_col]) / 1e8  # 转亿
            date_val = str(row[date_col])[:10]
            history.append({
                'date': date_val,
                'net_flow_yi': round(net_val, 1),
                'direction': '流入' if net_val > 0 else '流出',
            })
        return history
    except Exception:
        return []


# ═══════════════════════════════════════════
#  CLI 入口 (python north_flow_tracker.py)
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import json
    signal = get_north_flow_signal(verbose=True)

    print("\n历史流向 (近5日):")
    history = get_north_flow_history(5)
    for h in history:
        print(f"  {h['date']}: {h['net_flow_yi']:+.1f}亿 ({h['direction']})")

    if '--json' in sys.argv:
        print(json.dumps(signal, ensure_ascii=False, indent=2))
