"""策略过滤器 — 基于30天回测数据挖掘的最优过滤规则

发现 (2026-06-14):
1. 涨停 gap 0~5% 甜点区: EV=+1.87% (vs 全量 +1.21%)
2. 趋势 rank=1 + gap>-2%: EV=+1.74% (vs 全量 -0.37%)
3. 涨停 周二+周五: EV=+2.69%~+3.69%
4. 趋势 chg 反向: 高涨幅→次日低收益 (IC=-2.09)
5. 涨停 Q2评分(38-66)最优: EV=+3.74%, Q4(74-99)反而 EV=-0.88%
"""

from datetime import datetime

# ═══════════════════════════════════════════
#  过滤器函数
# ═══════════════════════════════════════════

def gap_sweet_spot(trade: dict, tab: str) -> bool:
    """涨停/趋势: gap甜点区过滤.

    涨停: gap 0~5% 最优 (EV+1.87%), gap<-2% 全亏 (-2.37%)
    趋势: gap>-2% 避免大幅低开
    """
    gap = trade.get('gap_open_pct', 0)
    if tab == 'limit-up':
        return 0 <= gap <= 5
    elif tab == 'trend':
        return gap > -2
    return True


def rank1_only(trade: dict, tab: str) -> bool:
    """只保留每日评分第一的票.

    趋势 rank=1: EV=+0.56% (vs top_n=5 全量 -0.37%)
    涨停 rank=1: EV=+2.12% (vs top_n=5 全量 +1.21%)
    """
    return trade.get('rank', 99) == 1


def weekday_filter(trade: dict, tab: str) -> bool:
    """星期效应过滤.

    涨停: 周二+周五最优 (EV+2.69%, +3.69%), 周一最差 (-1.50%)
    趋势: 周一+周二最优 (EV+0.53%), 周五最差 (-1.22%)
    """
    d = trade.get('signal_date', '')
    if len(d) != 8:
        return True
    w = datetime.strptime(d, '%Y%m%d').weekday()  # 0=Mon
    if tab == 'limit-up':
        return w in [1, 4]  # Tue, Fri
    elif tab == 'trend':
        return w in [0, 1]  # Mon, Tue
    return True


def score_sweet_spot(trade: dict, tab: str) -> bool:
    """评分甜点区过滤.

    涨停 Q2(38-66): EV+3.74%, Q4(74-99): EV-0.88% -- 最高分反而是陷阱
    """
    score = trade.get('score', 0)
    if tab == 'limit-up':
        return 38 <= score <= 72  # Q2+Q3, 避开Q4(>72)陷阱
    elif tab == 'trend':
        return 27 <= score <= 54  # Q2+Q3
    return True


# ═══════════════════════════════════════════
#  预定义策略组合
# ═══════════════════════════════════════════

PRESETS = {
    'trend-elite': {
        'name': '趋势精选 (rank1+gap+周一/二)',
        'filters': [
            lambda t: rank1_only(t, 'trend'),
            lambda t: gap_sweet_spot(t, 'trend'),
            lambda t: weekday_filter(t, 'trend'),
        ],
        'top_n_boost': 1,  # 用更大top_n拉池, 然后精选
    },
    'limit-sweet': {
        'name': '涨停甜点 (gap0~5+周二/五+避开Q4)',
        'filters': [
            lambda t: gap_sweet_spot(t, 'limit-up'),
            lambda t: weekday_filter(t, 'limit-up'),
            lambda t: score_sweet_spot(t, 'limit-up'),
        ],
        'top_n_boost': 5,
    },
    'limit-prime': {
        'name': '涨停黄金 (rank1+gap0~5+周二/五)',
        'filters': [
            lambda t: rank1_only(t, 'limit-up'),
            lambda t: gap_sweet_spot(t, 'limit-up'),
            lambda t: weekday_filter(t, 'limit-up'),
        ],
        'top_n_boost': 3,
    },
}

def apply_filters(trades: list, filters: list) -> list:
    """对交易列表依次应用过滤器."""
    result = trades
    for f in filters:
        result = [t for t in result if f(t)]
    return result


def get_preset(name: str) -> dict:
    """获取预定义策略."""
    return PRESETS.get(name, {})
