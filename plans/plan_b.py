"""Plan B — 数据驱动评分 (2026-07-05)

基于服务器1872笔26天回测数据分析, 抛弃原有因子加权体系,
用简单的实盘可知规则做硬过滤+排序.

核心发现 (数据驱动):
1. score<40 的票反而最赚 (+37195), 高分票最亏 → 原评分系统反向了
2. gap_open_pct[3,5) 胜率50.8% +20074, gap[-1,0) 胜率36.2% -85600
3. dtqiaoban 是唯一全量盈利 tab (+17509)
4. 价格>40元 胜率50.2% +12160
5. reversal + gap<2 + price>=20 → 86笔 50% +15735

策略: 不再用原评分排序, 改用 "实盘可知特征" 做新评分:
  - 跳空区间 (买入日开盘 vs 信号日收盘的跳空, 实盘竞价时可算)
  - 价格区间 (信号日收盘价, 实盘可知)
  - tab 类型 (dtqiaoban 加分)
  - 原评分仅作 tiebreaker (反着用: 低分略加分)

注意: gap_open_pct 在实盘时 = 竞价价/昨收 - 1, 可知.
      回测时 = buy_open/signal_close - 1, 同样可知.
"""

# 数据驱动的评分规则 (基于1872笔回测)
# 每个规则贡献 0-30 分, 总分 0-100

def score_plan_b(gap_open_pct: float, signal_close: float, tab: str, original_score: float = 50) -> float:
    """数据驱动评分, 返回 0-100 分.

    Args:
        gap_open_pct: 买入日开盘跳空 (buy_open/signal_close - 1) * 100
        signal_close: 信号日收盘价
        tab: 'limit-up'/'trend'/'reversal'/'zhaban'/'dtqiaoban'
        original_score: 原评分系统的分 (仅作 tiebreaker)
    Returns:
        0-100 的新评分, 越高越值得买
    """
    score = 50.0  # 基础分

    # 1. 跳空区间 (最强预测因子, IC最高)
    #    数据: gap[3,5)=50.8%+20074, gap[5,+)=66.7%+2830, gap[-1,0)=36.2%-85600
    if 3 <= gap_open_pct < 5:
        score += 25   # 最佳跳空区间
    elif gap_open_pct >= 5:
        score += 20   # 高开强势
    elif 1 <= gap_open_pct < 3:
        score += 10   # 温和高开
    elif 0 <= gap_open_pct < 1:
        score += 0    # 平开 (中性)
    elif -3 <= gap_open_pct < 0:
        score -= 10   # 小幅低开 (不利)
    elif -5 <= gap_open_pct < -3:
        score -= 15   # 大幅低开 (可能抄底)
    else:  # < -5
        score -= 5    # 深跌 (可能有反弹, 略减)

    # 2. 价格区间
    #    数据: price>40=50.2%+12160, price[0,5)=39.7%-4215
    if signal_close >= 40:
        score += 15   # 高价股 (机构票, 稳定)
    elif signal_close >= 20:
        score += 10   # 中高价
    elif signal_close >= 10:
        score += 5    # 中价
    elif signal_close >= 5:
        score += 0    # 低价
    else:
        score -= 5    # 超低价 (仙股风险)

    # 3. Tab 类型调整
    #    数据: dtqiaoban=45.5%+17509(唯一盈利), trend=38.1%-56045(最差)
    tab_bonus = {
        'dtqiaoban': 10,   # 唯一全量盈利 tab
        'reversal':  5,    # 接近持平 (-10683)
        'limit-up':  0,    # 亏损 (-46976)
        'zhaban':   -5,    # 大亏 (-56716)
        'trend':    -10,   # 最差 (-56045)
    }
    score += tab_bonus.get(tab, 0)

    # 4. 原评分反用 (数据: score<40=+37195, score>70=-43262)
    #    原高分票反而亏, 给低分票小幅加分
    if original_score < 40:
        score += 5    # 低分票反而好
    elif original_score > 70:
        score -= 5    # 高分票反而差

    return max(0, min(100, round(score, 1)))


def should_buy_plan_b(gap_open_pct: float, signal_close: float, tab: str) -> bool:
    """硬过滤: 只买数据验证盈利的组合.

    数据验证的盈利规则:
    - dtqiaoban: 全量盈利, 不过滤
    - reversal + gap<2% + price>=20: 86笔 50% +15735
    - limit-up + gap[2,5) + price[10,20): 50笔 52% +8338
    - 其他: gap >= 0 (不买低开的票, gap[-1,0)亏损-85600)
    """
    # dtqiaoban 全量放行
    if tab == 'dtqiaoban':
        return True

    # 不买低开的票 (gap < 0 是最大亏损来源)
    if gap_open_pct < 0:
        return False

    # reversal: 价格>=20 且 gap<2% 时最佳
    if tab == 'reversal':
        return signal_close >= 10  # 放宽到10元+, 保证笔数

    # limit-up: gap[2,5) + price[10,20) 最佳, 但也允许 gap[0,5) + price>=5
    if tab == 'limit-up':
        return signal_close >= 5 and gap_open_pct < 5

    # zhaban/trend: 只在 gap[3,5) 时允许 (数据上唯一盈利区间)
    if tab in ('zhaban', 'trend'):
        return 3 <= gap_open_pct < 5

    return True


# 最优参数 (数据驱动)
PLAN_B_PARAMS = {
    'limit-up':   {'min_score': 50, 'sell_n': 2},   # gap[0,5)+price>=5, sell_n=2(T+2卖)
    'trend':      {'min_score': 50, 'sell_n': 2},   # 仅gap[3,5), sell_n=2
    'reversal':   {'min_score': 50, 'sell_n': 2},   # gap>=0+price>=10, sell_n=2
    'zhaban':     {'min_score': 50, 'sell_n': 5},   # 仅gap[3,5), sell_n=5(数据:sn=5优于sn=2)
    'dtqiaoban':  {'min_score': 50, 'sell_n': 2},   # 全量, sell_n=2
}
