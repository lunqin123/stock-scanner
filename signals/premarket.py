#!/usr/bin/env python3
"""
盘前多空信号聚合器 (Premarket Signal Aggregator)

基于主力资金理论：开盘前的多空信号是判断当日方向的第一层概率优势。
每日 8:00-9:15 聚合境外市场、汇率、流动性等盘前已知信息，
输出方向性偏多/偏空信号 + 置信度。

数据源 (降级链: akshare → HTTP直连 → 默认值):
  - 美股三大指数 (纳指/标普/道指) → index_global_spot_em
  - 富时A50期货夜盘 → futures_global_spot_em
  - 人民币汇率 → fx_spot_quote / macro_china_rmb
  - 央行公开市场操作 → 新浪Shibor / LPR

用法:
    from premarket import get_premarket_signal
    signal = get_premarket_signal()
    print(signal['direction'], signal['score'], signal['summary'])
"""

import sys
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

import pandas as pd
import requests

from scanner_utils import _CST

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ═══════════════════════════════════════════
#  数据获取 (每个函数独立降级)
# ═══════════════════════════════════════════

def _fetch_us_market() -> Tuple[float, float, str]:
    """
    获取美股三大指数收盘涨跌幅 (纳指权重最高, 对标A股科技)。
    返回: (nasdaq_chg_pct, sp500_chg_pct, summary_str)
    降级: akshare → 新浪API → (0, 0, '无数据')
    """
    # 方案1: akshare 全球指数
    try:
        import akshare as ak
        df = ak.index_global_spot_em()
        if df is not None and not df.empty:
            name_col = df.columns[0]
            chg_col = None
            for c in df.columns:
                if '涨跌幅' in str(c) or 'change' in str(c).lower():
                    chg_col = c
                    break
            nasdaq_chg = 0.0
            sp500_chg = 0.0
            for _, row in df.iterrows():
                name = str(row[name_col])
                if chg_col:
                    chg = float(row[chg_col]) if pd.notna(row[chg_col]) else 0.0
                else:
                    chg = 0.0
                if '纳斯达克' in name or 'Nasdaq' in name:
                    nasdaq_chg = chg
                elif '标普' in name or 'S&P' in name:
                    sp500_chg = chg
            if nasdaq_chg != 0 or sp500_chg != 0:
                return nasdaq_chg, sp500_chg, f"纳指{nasdaq_chg:+.2f}% 标普{sp500_chg:+.2f}%"
    except Exception:
        pass

    # 方案2: 新浪美股API
    try:
        url = "https://hq.sinajs.cn/list=nasdaq_composite,sp_500,dji"
        headers = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = 'gbk'
        # 新浪返回格式: var hq_str_xxx="name,price,change,pct_change,...";
        lines = r.text.strip().split('\n')
        result = {}
        for line in lines:
            if '=' in line:
                key = line.split('=')[0].split('_')[-1]
                val = line.split('"')[1] if '"' in line else ''
                parts = val.split(',')
                if len(parts) >= 4:
                    result[key] = float(parts[3])  # pct_change
        nasdaq_chg = result.get('composite', 0.0)
        sp500_chg = result.get('500', 0.0)
        if nasdaq_chg != 0 or sp500_chg != 0:
            return nasdaq_chg, sp500_chg, f"纳指{nasdaq_chg:+.2f}% 标普{sp500_chg:+.2f}%"
    except Exception:
        pass

    return 0.0, 0.0, "美股数据暂不可用"


def _fetch_a50_overnight() -> Tuple[float, str]:
    """
    获取富时A50期货涨跌幅 (最直接的A股开盘领先指标)。
    返回: (chg_pct, summary_str)
    """
    # 方案1: akshare 全球期货
    try:
        import akshare as ak
        df = ak.futures_global_spot_em()
        if df is not None and not df.empty:
            name_col = df.columns[0]
            chg_col = None
            for c in df.columns:
                if '涨跌幅' in str(c):
                    chg_col = c
                    break
            if chg_col is None and len(df.columns) >= 6:
                chg_col = df.columns[5]  # 通常第6列是涨跌幅
            for _, row in df.iterrows():
                name = str(row[name_col])
                if any(k in name for k in ['A50', '富时', 'CN', '中国']):
                    chg = float(row[chg_col]) if chg_col and pd.notna(row[chg_col]) else 0.0
                    if chg != 0:
                        return chg, f"A50期货{chg:+.2f}%"
    except Exception:
        pass

    # 方案2: 新浪A50期货
    try:
        url = "https://hq.sinajs.cn/list=cn_A50"
        headers = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = 'gbk'
        text = r.text
        if '"' in text:
            parts = text.split('"')[1].split(',')
            if len(parts) >= 4:
                chg = float(parts[3])
                return chg, f"A50期货{chg:+.2f}%"
    except Exception:
        pass

    return 0.0, "A50期货数据暂不可用"


def _fetch_rmb_rate() -> Tuple[float, str]:
    """
    获取人民币汇率变动 (升值=利好A股, 贬值=利空A股)。
    返回: (chg_pct_positive, summary_str) — 正数=升值利好
    """
    # 方案1: akshare fx_spot_quote
    try:
        import akshare as ak
        df = ak.fx_spot_quote()
        if df is not None and not df.empty:
            pair_col = df.columns[0]
            price_col = df.columns[1] if len(df.columns) > 1 else None
            prev_col = df.columns[2] if len(df.columns) > 2 else None
            if price_col and prev_col:
                for _, row in df.iterrows():
                    pair = str(row[pair_col])
                    if 'USD/CNY' in pair or '美元' in pair:
                        current = float(row[price_col])
                        previous = float(row[prev_col])
                        if previous > 0:
                            chg = (previous - current) / previous * 100  # 升值=正
                            direction = "升值" if chg > 0 else "贬值"
                            return chg, f"人民币{direction}{abs(chg):.2f}%"
    except Exception:
        pass

    # 方案2: 新浪汇率
    try:
        url = "https://hq.sinajs.cn/list=USDCNY"
        headers = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = 'gbk'
        text = r.text
        if '"' in text:
            parts = text.split('"')[1].split(',')
            if len(parts) >= 2:
                current = float(parts[1])
                # 假设前一交易日收盘为 parts[2]
                prev_close = float(parts[2]) if len(parts) > 2 and parts[2] else current
                if prev_close > 0:
                    chg = (prev_close - current) / prev_close * 100
                    direction = "升值" if chg > 0 else "贬值"
                    return chg, f"人民币{direction}{abs(chg):.2f}%"
    except Exception:
        pass

    return 0.0, "汇率数据暂不可用"


def _fetch_liquidity_signal() -> Tuple[float, str]:
    """
    检测央行流动性信号 (Shibor隔夜利率变化 + LPR)。
    返回: (score_adjustment, summary_str) — 正=流动性宽松利好
    """
    try:
        import akshare as ak
        df = ak.macro_china_shibor_all()
        if df is not None and not df.empty:
            # 找隔夜利率
            for _, row in df.iterrows():
                label = str(row.iloc[0]) if len(row) > 0 else ''
                if '隔夜' in label or 'O/N' in label:
                    current = float(row.iloc[1]) if pd.notna(row.iloc[1]) else 0
                    # 比较前一交易日
                    prev = float(row.iloc[2]) if len(row) > 2 and pd.notna(row.iloc[2]) else current
                    if prev > 0:
                        diff = prev - current  # 利率下降=宽松
                        if diff > 0.1:
                            return 1.0, f"隔夜Shibor降{diff:.2f}bp(宽松)"
                        elif diff < -0.1:
                            return -0.5, f"隔夜Shibor升{abs(diff):.2f}bp(收紧)"
                        else:
                            return 0.0, "流动性平稳"
                    break
    except Exception:
        pass

    return 0.0, "流动性数据暂不可用"


# ═══════════════════════════════════════════
#  盘前信号聚合
# ═══════════════════════════════════════════

def get_premarket_signal(verbose: bool = False) -> dict:
    """
    聚合所有盘前信号，输出方向性判断。

    Returns:
        dict with:
        - direction: '偏多' | '偏空' | '震荡'
        - score: 0-10 (盘前信号强度, 5=中性)
        - confidence: '高' | '中' | '低'
        - factors: {name: score} 各因子贡献
        - details: {name: summary} 各因子详情
        - summary: 一句话总结
    """
    now = datetime.now(_CST)
    is_premarket = now.hour < 9 or (now.hour == 9 and now.minute < 15)

    factors = {}
    details = {}
    total_score = 5.0  # 中性基准
    active_signal_count = 0
    total_signal_count = 0

    # 1. 美股收盘 (权重25%, 影响-2~+2)
    nasdaq_chg, sp500_chg, us_summary = _fetch_us_market()
    if nasdaq_chg != 0 or sp500_chg != 0:
        total_signal_count += 1
        active_signal_count += 1
        # 纳指>1% = 强多, <-1% = 强空
        if nasdaq_chg > 1.5:
            us_score = 2.0
        elif nasdaq_chg > 0.5:
            us_score = 1.0
        elif nasdaq_chg > -0.5:
            us_score = 0.0
        elif nasdaq_chg > -1.5:
            us_score = -1.0
        else:
            us_score = -2.0
        factors['美股'] = us_score
        details['美股'] = us_summary
        total_score += us_score
    else:
        factors['美股'] = 0
        details['美股'] = '暂无数据'

    # 2. A50期货夜盘 (权重30%, 影响-3~+3)
    a50_chg, a50_summary = _fetch_a50_overnight()
    if a50_chg != 0:
        total_signal_count += 1
        active_signal_count += 1
        if a50_chg > 1.0:
            a50_score = 3.0
        elif a50_chg > 0.5:
            a50_score = 2.0
        elif a50_chg > 0:
            a50_score = 1.0
        elif a50_chg > -0.5:
            a50_score = -1.0
        elif a50_chg > -1.0:
            a50_score = -2.0
        else:
            a50_score = -3.0
        factors['A50期货'] = a50_score
        details['A50期货'] = a50_summary
        total_score += a50_score
    else:
        factors['A50期货'] = 0
        details['A50期货'] = '暂无数据'

    # 3. 人民币汇率 (权重20%, 影响-2~+2)
    rmb_chg, rmb_summary = _fetch_rmb_rate()
    if rmb_chg != 0:
        total_signal_count += 1
        active_signal_count += 1
        if rmb_chg > 0.5:
            rmb_score = 2.0
        elif rmb_chg > 0.1:
            rmb_score = 1.0
        elif rmb_chg > -0.1:
            rmb_score = 0.0
        elif rmb_chg > -0.5:
            rmb_score = -1.0
        else:
            rmb_score = -2.0
        factors['人民币'] = rmb_score
        details['人民币'] = rmb_summary
        total_score += rmb_score
    else:
        factors['人民币'] = 0
        details['人民币'] = '暂无数据'

    # 4. 流动性 (权重15%, 影响-1~+1)
    liq_score, liq_summary = _fetch_liquidity_signal()
    if liq_score != 0:
        total_signal_count += 1
        active_signal_count += 1
    factors['流动性'] = liq_score
    details['流动性'] = liq_summary
    total_score += liq_score

    # ── 方向判断 ──
    total_score = round(max(0, min(10, total_score)), 1)

    if total_score >= 7:
        direction = '偏多'
    elif total_score >= 5.5:
        direction = '偏多'
    elif total_score >= 4.5:
        direction = '震荡'
    elif total_score >= 3:
        direction = '偏空'
    else:
        direction = '偏空'

    # ── 置信度 ──
    if total_signal_count == 0:
        confidence = '低'
        confidence_note = '无可用盘前数据'
    elif active_signal_count >= 3 and _signals_aligned(factors):
        confidence = '高'
        confidence_note = '多信号共振'
    elif active_signal_count >= 2:
        confidence = '中'
        confidence_note = '部分信号可用'
    else:
        confidence = '低'
        confidence_note = f'可用信号不足({active_signal_count}/{total_signal_count})'

    # ── 一句话总结 ──
    score_parts = []
    for name, score in factors.items():
        if score > 0:
            score_parts.append(f"{name}{'+' if score>0 else ''}{score:.0f}")
        elif score < 0:
            score_parts.append(f"{name}{score:.0f}")
    score_str = ' '.join(score_parts) if score_parts else '无盘前信号'

    if direction == '偏多':
        summary = f"盘前{direction}({total_score}分/{confidence}置信) → {score_str}，今日大概率偏强"
    elif direction == '偏空':
        summary = f"盘前{direction}({total_score}分/{confidence}置信) → {score_str}，今日注意风险"
    else:
        summary = f"盘前{direction}({total_score}分/{confidence}置信) → {score_str}，等待开盘确认"

    # 情绪系数: 盘前信号映射为情绪乘法系数 (同 detect_market_sentiment 输出格式)
    # 5分中性→×1.0, 10分极多→×1.15, 0分极空→×0.85
    sentiment_mult = 1.0 + (total_score - 5.0) * 0.03

    result = {
        'direction': direction,
        'score': total_score,
        'confidence': confidence,
        'confidence_note': confidence_note,
        'factors': factors,
        'details': details,
        'summary': summary,
        'sentiment_mult': round(sentiment_mult, 3),
        'is_premarket': is_premarket,
        'timestamp': now.strftime('%Y-%m-%d %H:%M'),
    }

    if verbose:
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"  盘前多空信号 | {now.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)
        print(f"  方向: {direction} | 得分: {total_score}/10 | 置信: {confidence}({confidence_note})", file=sys.stderr)
        print(f"  情绪系数: {sentiment_mult:.3f} (×{sentiment_mult:.2f})", file=sys.stderr)
        print(f"  " + "-" * 40, file=sys.stderr)
        for name, score in factors.items():
            detail = details.get(name, '')
            bar = '█' * int(abs(score)) + '░' * (3 - int(abs(score)))
            sign = '+' if score > 0 else (' ' if score == 0 else '-')
            print(f"  {name:<6s} {sign}{score:+.0f}  {bar}  {detail}", file=sys.stderr)
        print(f"  " + "-" * 40, file=sys.stderr)
        print(f"  {summary}", file=sys.stderr)

    return result


def _signals_aligned(factors: dict) -> bool:
    """检查多信号是否方向一致 (>50%同向)"""
    signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in factors.values()]
    signs = [s for s in signs if s != 0]
    if len(signs) < 2:
        return False
    pos = sum(1 for s in signs if s > 0)
    neg = sum(1 for s in signs if s < 0)
    return max(pos, neg) >= len(signs) * 0.75


# ═══════════════════════════════════════════
#  缓存版 (给app.py用, 盘前缓存到9:30)
# ═══════════════════════════════════════════

_premarket_cache: Optional[dict] = None
_premarket_cache_time: Optional[datetime] = None


def get_premarket_signal_cached(max_age_minutes: int = 10) -> dict:
    """缓存版: 盘前每10分钟更新, 盘中不更新。"""
    global _premarket_cache, _premarket_cache_time
    now = datetime.now(_CST)
    if _premarket_cache and _premarket_cache_time:
        age = (now - _premarket_cache_time).total_seconds() / 60
        if age < max_age_minutes:
            return _premarket_cache
    signal = get_premarket_signal()
    _premarket_cache = signal
    _premarket_cache_time = now
    return signal


# ═══════════════════════════════════════════
#  CLI 入口 (python premarket.py)
# ═══════════════════════════════════════════

if __name__ == '__main__':
    signal = get_premarket_signal(verbose=True)
    if '--json' in sys.argv:
        print(json.dumps(signal, ensure_ascii=False, indent=2))
