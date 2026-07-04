#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan A 新因子 v2: 持续性 + 回撤位置

设计目的: 解决"评分高=追高陷阱"问题
  - 老评分偏"当日动量",高分=已大涨的票,T+1 容易回调
  - 新评分加"持续性"维度,识别"持续强势"vs"一日游"
  - 新评分加"回撤位置"维度,识别"刚回踩后回升"vs"深度回撤中"

数据基础: archive.db daily_stocks (trade_date, code, change_pct, price, stock_type)
  - 限制: 只对 stock_type='limit_up' 的票有历史数据
  - 其他 tab 票无历史 → 新因子降级到 5.0 (中性分, 不影响总评分)

新因子设计:
  - momentum_consistency (0-10): 过去 10 日涨幅一致性
      算法: 取过去 10 个 trade_date, 计算:
        1. 正涨幅天数占比 (40% 权重)
        2. 累计涨幅 (30% 权重)
        3. 标准差倒数 (稳定度, 30% 权重)
      评分逻辑:
        一日游 (单日 +9.5% 然后停滞): 一致性低 → 低分
        持续小涨 (10 日 7 涨): 一致性高 → 高分
        横盘震荡: 中性

  - pullback_depth (0-10): 从近期高点回撤程度
      算法: 取过去 20 日最高价, 计算回撤 = (current - high20) / high20 * 100
      评分逻辑:
        current 接近 high20 (-2% 以内) → 10 分 (高位强势, 等回调入场)
        current 回撤 -5%~-10% → 5-7 分 (回踩中, 可能反弹)
        current 回撤 > -15% → 0-3 分 (深度下跌, 不参与)
"""
import os
import sys
import sqlite3
import pandas as pd
from typing import Optional

_ARCHIVE_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'archive.db')


def _load_history_prices(code: str, today_str: str, days: int = 30) -> pd.DataFrame:
    """从 archive.db 拉该票过去 N 个交易日的日线

    Args:
        code: 6位股票代码
        today_str: YYYY-MM-DD 格式
        days: 取最近 N 个交易日

    Returns:
        DataFrame with columns [trade_date, change_pct, price]
        按 trade_date 升序排列
    """
    try:
        conn = sqlite3.connect(_ARCHIVE_DB, timeout=5)
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, change_pct, price
            FROM daily_stocks
            WHERE code = ? AND trade_date < ?
            ORDER BY trade_date DESC
            LIMIT ?
        """, (code, today_str, days))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=['trade_date', 'change_pct', 'price'])
    df = df.sort_values('trade_date').reset_index(drop=True)
    return df


def compute_momentum_consistency(filtered: pd.DataFrame, today_str: str) -> pd.Series:
    """momentum_consistency (0-10): 过去 10 日涨幅一致性

    高分 = 持续小涨 / 持续强势
    低分 = 一日游 / 横盘震荡
    无数据 = 5.0 (中性)
    """
    code_col = '代码' if '代码' in filtered.columns else (filtered.columns[1] if len(filtered.columns) > 1 else filtered.columns[0])
    today_clean = today_str.replace('-', '')

    scores = pd.Series(5.0, index=filtered.index)

    for idx in filtered.index:
        code = str(filtered.loc[idx, code_col]).strip().zfill(6)
        hist = _load_history_prices(code, today_clean, days=15)

        if hist.empty or len(hist) < 5:
            continue  # 数据不足,保持中性 5.0

        recent = hist.tail(10)  # 最近 10 个交易日
        changes = recent['change_pct'].astype(float).values

        # 子分1: 正涨幅天数占比 (40%)
        pos_days_ratio = sum(1 for c in changes if c > 0) / len(changes)

        # 子分2: 累计涨幅 (30%) - log 缩放避免极端值
        import math
        cum_ret = sum(changes)
        # 累计 10% → 5 分, 累计 30% → 8 分, 累计 50%+ → 10 分
        cum_score = min(10, max(0, cum_ret / 5))

        # 子分3: 稳定性 (30%) - 标准差倒数,但一日游标准差也高
        # 一日游: [9.5, 0, 0, 0, 0, 0, 0, 0, 0, 0] → std 大
        # 持续涨: [1.5, 1.2, 1.8, 1.0, 1.5, 1.2, 1.8, 1.0, 1.5, 1.2] → std 小
        # 区分: 持续涨正涨幅天数占比高
        # 用正涨幅占比作为主要信号, 累计涨幅作为辅助
        if pos_days_ratio >= 0.7:
            stab_score = 10  # 70%+ 日子上涨
        elif pos_days_ratio >= 0.5:
            stab_score = 7
        elif pos_days_ratio >= 0.3:
            stab_score = 4
        else:
            stab_score = 2  # 多数日子跌

        final = pos_days_ratio * 10 * 0.4 + cum_score * 0.3 + stab_score * 0.3
        scores[idx] = round(max(0, min(10, final)), 1)

    return scores


def compute_pullback_depth(filtered: pd.DataFrame, today_str: str) -> pd.Series:
    """pullback_depth (0-10): 从近期高点回撤程度

    高分 = 当前价接近近期高点 (高位强势 / 刚突破)
    低分 = 当前价远低于近期高点 (深度下跌 / 弱势)
    无数据 = 5.0 (中性)
    """
    code_col = '代码' if '代码' in filtered.columns else (filtered.columns[1] if len(filtered.columns) > 1 else filtered.columns[0])
    today_clean = today_str.replace('-', '')

    scores = pd.Series(5.0, index=filtered.index)

    for idx in filtered.index:
        code = str(filtered.loc[idx, code_col]).strip().zfill(6)
        # 取最近价
        try:
            current_price = float(filtered.loc[idx, '最新价']) if '最新价' in filtered.columns else None
        except (KeyError, ValueError, TypeError):
            current_price = None

        if current_price is None or current_price <= 0:
            continue

        hist = _load_history_prices(code, today_clean, days=25)
        if hist.empty or len(hist) < 5:
            continue

        # 取过去 20 日最高价
        recent = hist.tail(20)
        high20 = recent['price'].astype(float).max()
        if high20 <= 0:
            continue

        # 回撤幅度 (% 负数=下跌)
        pullback = (current_price - high20) / high20 * 100  # 负值=从高点回撤

        # 评分: -2% 以内 10 分, -5% 7 分, -10% 4 分, -20%+ 0 分
        if pullback >= -2:
            score = 10
        elif pullback >= -5:
            score = 7
        elif pullback >= -10:
            score = 4
        else:
            score = max(0, 2 + pullback / 5)  # 深度下跌

        scores[idx] = round(score, 1)

    return scores


def compute_v2_factors(filtered: pd.DataFrame, today_str: str) -> dict:
    """一次性算两个 v2 因子

    Returns:
        dict with keys 'momentum_consistency', 'pullback_depth'
    """
    mc = compute_momentum_consistency(filtered, today_str)
    pd_ = compute_pullback_depth(filtered, today_str)
    return {'momentum_consistency': mc, 'pullback_depth': pd_}