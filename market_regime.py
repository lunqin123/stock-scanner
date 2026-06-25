#!/usr/bin/env python3
"""
市场状态分类器 (Market Regime Classifier)

基于主力资金理论：不同市场状态下，主力资金的行为模式完全不同。
识别当前市场由谁主导 → 调整策略偏好和仓位。

五种市场状态:
  1. 北向驱动市 — 北向持续流入 + 权重股强于小票 → 跟随北向，重仓权重板块
  2. 游资情绪市 — 涨停>80只 + 连板率高 + 炸板率低 → 积极打板
  3. 机构调仓市 — 板块轮动快 + 高低切换 → 趋势策略为主
  4. 量化主导市 — 成交量极端 + 日内反转频繁 → 减少操作，做T为主
  5. 防御避险市 — 高股息涨 + 北向流出 + 缩量 → 轻仓/空仓

用法:
    from market_regime import classify_regime
    regime = classify_regime()
    print(regime['label'], regime['position_advice'])
"""

import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

from scanner_utils import _CST, get_market_status, get_today_str

# ═══════════════════════════════════════════
#  数据获取 (降级安全)
# ═══════════════════════════════════════════

def _get_today_limit_counts() -> tuple:
    """获取今日涨停/炸板/跌停数。返回 (limit_up, zhaban, dieting)"""
    try:
        import akshare as ak
        today = datetime.now(_CST).strftime('%Y%m%d')
        lt = ak.stock_zt_pool_em(date=today)
        zb = ak.stock_zt_pool_zbgc_em(date=today)
        dt = ak.stock_zt_pool_dtgc_em(date=today)
        return (
            len(lt) if lt is not None and not lt.empty else 0,
            len(zb) if zb is not None and not zb.empty else 0,
            len(dt) if dt is not None and not dt.empty else 0,
        )
    except Exception:
        return 0, 0, 0


def _get_north_flow_streak() -> tuple:
    """获取北向资金连续流入/流出天数。返回 (streak_days, direction, history)"""
    try:
        from north_flow_tracker import get_north_flow_history
        history = get_north_flow_history(10)
        if not history:
            return 0, '无数据', []

        # 计算连续方向
        streak = 1
        base_direction = history[0]['direction']
        for i in range(1, len(history)):
            if history[i]['direction'] == base_direction:
                streak += 1
            else:
                break
        return streak, base_direction, history
    except Exception:
        return 0, '无数据', []


def _get_sector_rotation_speed() -> float:
    """
    计算板块轮动速度 (高=快速轮动=机构调仓).
    简化方案: 比较今日涨停板块 vs 上一交易日涨停板块的重叠度。
    重叠度越低 → 轮动越快。
    返回: 0-1 (0=完全重叠/慢, 1=完全轮换/快)
    """
    try:
        import akshare as ak
        today = datetime.now(_CST).strftime('%Y%m%d')
        yesterday = (datetime.now(_CST) - timedelta(days=1)).strftime('%Y%m%d')

        today_pool = ak.stock_zt_pool_em(date=today)
        yesterday_pool = ak.stock_zt_pool_em(date=yesterday)

        if today_pool is None or today_pool.empty:
            return 0.5
        if yesterday_pool is None or yesterday_pool.empty:
            return 0.5

        ind_col = '所属行业' if '所属行业' in today_pool.columns else (
            today_pool.columns[15] if len(today_pool.columns) > 15 else None)
        if ind_col is None:
            return 0.5

        today_inds = set(today_pool[ind_col].dropna().unique())
        yesterday_inds = set(yesterday_pool[ind_col].dropna().unique())

        if not today_inds or not yesterday_inds:
            return 0.5

        overlap = len(today_inds & yesterday_inds) / len(today_inds | yesterday_inds)
        speed = 1.0 - overlap  # 重叠度越低=轮动越快
        return round(speed, 2)
    except Exception:
        return 0.5


def _get_promotion_rate() -> float:
    """获取昨日涨停晋级率 (今日继续涨停的比例)"""
    try:
        import akshare as ak
        today = datetime.now(_CST).strftime('%Y%m%d')
        prev = ak.stock_zt_pool_previous_em(date=today)
        if prev is None or prev.empty:
            return 0.0

        chg_col = prev.columns[3]
        changes = prev[chg_col].astype(float)
        promo = (changes > 9).sum() / len(prev)
        return round(float(promo), 2)
    except Exception:
        return 0.0


# ═══════════════════════════════════════════
#  市场状态分类
# ═══════════════════════════════════════════

def classify_regime(verbose: bool = False) -> dict:
    """
    分类当前市场状态，输出策略建议。

    Returns:
        dict with:
        - regime: 'north_driven' | 'sentiment_driven' | 'institution_rotation' |
                  'quant_dominant' | 'defensive'
        - label: 中文标签
        - confidence: 0-1 置信度
        - position_advice: 仓位建议 (0.3-1.2)
        - strategy_weights: {tab: weight_multiplier} 各策略权重乘数
        - signals: {信号名: 值} 各维度信号
        - summary: 一句话策略建议
    """
    market_status = get_market_status()

    # 非交易时段
    if market_status not in ('trading', 'lunch', 'closed'):
        return {
            'regime': 'unknown', 'label': '非交易时段',
            'confidence': 0, 'position_advice': 1.0,
            'strategy_weights': {},
            'signals': {'status': market_status},
            'summary': f'当前{market_status}, 市场状态分析需在交易时段进行',
        }

    # ── 收集信号 ──
    signals = {}
    scores = {'north': 0, 'sentiment': 0, 'rotation': 0, 'quant': 0, 'defensive': 0}

    # 1. 北向资金信号
    streak, nf_direction, nf_history = _get_north_flow_streak()
    signals['north_streak'] = f"{nf_direction}{streak}天"
    if nf_direction == '流入' and streak >= 3:
        scores['north'] = 3
        signals['north_signal'] = '强流入'
    elif nf_direction == '流入' and streak >= 1:
        scores['north'] = 2
        signals['north_signal'] = '流入'
    elif nf_direction == '流出' and streak >= 3:
        scores['defensive'] += 2  # 北向持续流出=防御信号
        signals['north_signal'] = '持续流出'
    elif nf_direction == '流出':
        scores['north'] = -1
        signals['north_signal'] = '流出'
    else:
        signals['north_signal'] = '无数据'

    # 2. 涨停板信号 (游资情绪)
    limit_cnt, zhaban_cnt, dt_cnt = _get_today_limit_counts()
    total_limit_events = limit_cnt + zhaban_cnt + dt_cnt
    signals['limit_up_cnt'] = limit_cnt
    signals['zhaban_cnt'] = zhaban_cnt
    signals['dieting_cnt'] = dt_cnt

    if limit_cnt >= 80 and zhaban_cnt < limit_cnt * 0.3:
        scores['sentiment'] = 3
        signals['sentiment_signal'] = '高潮'
    elif limit_cnt >= 50:
        scores['sentiment'] = 2
        signals['sentiment_signal'] = '活跃'
    elif limit_cnt >= 30:
        scores['sentiment'] = 1
        signals['sentiment_signal'] = '正常'
    elif total_limit_events == 0:
        signals['sentiment_signal'] = '无数据'
    else:
        scores['sentiment'] = 0
        signals['sentiment_signal'] = '低迷'

    # 3. 晋级率
    promo_rate = _get_promotion_rate()
    signals['promotion_rate'] = promo_rate
    if promo_rate > 0.3:
        scores['sentiment'] += 1
    elif promo_rate < 0.1:
        scores['sentiment'] -= 1

    # 4. 板块轮动速度
    rotation_speed = _get_sector_rotation_speed()
    signals['rotation_speed'] = rotation_speed
    if rotation_speed > 0.7:
        scores['rotation'] = 3
        signals['rotation_signal'] = '快速轮动'
    elif rotation_speed > 0.4:
        scores['rotation'] = 1
        signals['rotation_signal'] = '中等轮动'
    else:
        signals['rotation_signal'] = '板块稳定'

    # 5. 跌停数 → 量化/恐慌信号
    if dt_cnt > 30:
        scores['quant'] += 2
        signals['dieting_signal'] = '大量跌停'
    elif dt_cnt > 10:
        scores['quant'] += 1
        signals['dieting_signal'] = '跌停偏多'
    else:
        signals['dieting_signal'] = '正常'

    # 6. 炸板率
    if total_limit_events > 0:
        zhaban_rate = zhaban_cnt / total_limit_events
        signals['zhaban_rate'] = round(zhaban_rate, 2)
        if zhaban_rate > 0.4:
            scores['quant'] += 2
        elif zhaban_rate > 0.3:
            scores['quant'] += 1
    else:
        signals['zhaban_rate'] = 0

    # ── 状态判定 ──
    max_score = max(scores.values())
    max_regimes = [k for k, v in scores.items() if v == max_score]

    # 主状态 = 得分最高的
    primary = max_regimes[0]

    regime_map = {
        'north': ('north_driven', '北向驱动市'),
        'sentiment': ('sentiment_driven', '游资情绪市'),
        'rotation': ('institution_rotation', '机构调仓市'),
        'quant': ('quant_dominant', '量化主导市'),
        'defensive': ('defensive', '防御避险市'),
    }

    regime_key, regime_label = regime_map.get(primary, ('unknown', '无法分类'))

    # 置信度: 最高分 vs 第二高分差距
    sorted_scores = sorted(scores.values(), reverse=True)
    gap = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
    if gap >= 2:
        confidence = 0.8
    elif gap >= 1:
        confidence = 0.6
    else:
        confidence = 0.4
        # 多状态情况: 混和描述
        if len(max_regimes) > 1:
            regime_label = '混和市'

    # ── 仓位建议 ──
    position_advice = {
        'north_driven': 1.1,         # 偏重仓
        'sentiment_driven': 1.0,      # 正常
        'institution_rotation': 0.8,  # 轻仓
        'quant_dominant': 0.5,        # 极轻仓
        'defensive': 0.3,             # 空仓/观望
    }.get(regime_key, 1.0)

    # ── 策略权重分配 ──
    strategy_weights = {
        'north_driven': {
            'limit-up': 1.1, 'trend': 1.0, 'zhaban': 0.8,
            'dtqiaoban': 0.5, 'reversal': 0.8, 'sector': 1.2,
        },
        'sentiment_driven': {
            'limit-up': 1.2, 'trend': 0.8, 'zhaban': 1.1,
            'dtqiaoban': 0.8, 'reversal': 1.0, 'sector': 1.0,
        },
        'institution_rotation': {
            'limit-up': 0.7, 'trend': 1.2, 'zhaban': 0.8,
            'dtqiaoban': 0.6, 'reversal': 0.9, 'sector': 0.8,
        },
        'quant_dominant': {
            'limit-up': 0.5, 'trend': 0.8, 'zhaban': 0.5,
            'dtqiaoban': 1.0, 'reversal': 0.5, 'sector': 0.5,
        },
        'defensive': {
            'limit-up': 0.3, 'trend': 0.5, 'zhaban': 0.3,
            'dtqiaoban': 0.5, 'reversal': 0.3, 'sector': 0.3,
        },
    }.get(regime_key, {})

    # ── 策略建议文案 ──
    advice_map = {
        'north_driven': '北向持续流入主导，关注外资偏好的大消费、新能源权重板块，跟随聪明钱做趋势',
        'sentiment_driven': '游资情绪高涨，涨停板赚钱效应好，积极打首板和龙头，注意控制连板高位票仓位',
        'institution_rotation': '板块快速轮动，追高风险大，适合趋势低吸策略，优选调整到位的强势板块龙头',
        'quant_dominant': '量化交易主导，日内反转频繁，控制仓位，以做T降低成本为主，减少新开仓',
        'defensive': '市场避险情绪重，北向流出+缩量+高股息走强，建议轻仓或空仓，等待情绪修复',
    }
    summary = advice_map.get(regime_key, '市场状态不明，保持现有策略')

    if verbose:
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"  市场状态分类器 | {datetime.now(_CST).strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)
        print(f"  主状态: {regime_label} (置信度 {confidence:.0%})", file=sys.stderr)
        print(f"  仓位建议: {position_advice*100:.0f}%", file=sys.stderr)
        print(f"  " + "-" * 40, file=sys.stderr)
        for k, v in scores.items():
            label = regime_map.get(k, (k, k))[1]
            bar = '█' * v + '░' * (3 - v)
            print(f"  {label:<10s} {v}/3 {bar}", file=sys.stderr)
        print(f"  " + "-" * 40, file=sys.stderr)
        print(f"  信号: {signals}", file=sys.stderr)
        print(f"  策略: {summary}", file=sys.stderr)
        if strategy_weights:
            print(f"  策略权重: {strategy_weights}", file=sys.stderr)

    return {
        'regime': regime_key,
        'label': regime_label,
        'confidence': round(confidence, 2),
        'position_advice': position_advice,
        'strategy_weights': strategy_weights,
        'signals': signals,
        'scores': {regime_map.get(k, (k, k))[1]: v for k, v in scores.items()},
        'summary': summary,
    }


# ═══════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import json
    regime = classify_regime(verbose=True)
    if '--json' in sys.argv:
        print(json.dumps(regime, ensure_ascii=False, indent=2))
