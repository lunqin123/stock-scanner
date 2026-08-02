"""Tab 常量与默认策略 (2026-08-01 自 backtest_engine.py 拆分)。

集中管理 6 个回测 tab 的: 名称常量 / 中文名 / 默认买入策略 / 自取池标记 /
信号池类型映射。评分与主循环分别从 backtest_scores / backtest_engine 引用。
"""

TAB_LIMIT_UP = 'limit-up'
TAB_TREND = 'trend'
TAB_ZHABAN = 'zhaban'
TAB_DTQIAOBAN = 'dtqiaoban'
TAB_REVERSAL = 'reversal'
TAB_SECTOR = 'sector'

ALL_TABS = [TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR]

TAB_NAMES_CN = {
    TAB_LIMIT_UP: '涨停扫描',
    TAB_TREND: '趋势扫描',
    TAB_ZHABAN: '炸板反包',
    TAB_DTQIAOBAN: '跌停翘板',
    TAB_REVERSAL: '涨停回调',
    TAB_SECTOR: '板块联动',
}

# 各 tab 默认买入策略 (close=T日尾盘买 T+1开盘卖; open=T+1开盘买 T+N开盘卖)
_TAB_BUY_TIME = {
    TAB_LIMIT_UP: 'close',
    TAB_DTQIAOBAN: 'open',
    TAB_TREND: 'open',
    TAB_ZHABAN: 'open',
    TAB_REVERSAL: 'open',
    TAB_SECTOR: 'open',
}

# 全部 tab 已实现
_PENDING_TABS = set()

# 这些 tab 的 score_fn 自行拉数据 (不依赖 fetcher 返回的 pool)
_SELF_FETCHING_TABS = {TAB_SECTOR}

# 每个 tab 的 pool_type 对应 archive_pools/ 中的 pickle 文件名前缀
_TAB_POOL_TYPE = {
    TAB_LIMIT_UP: 'limit_up',
    TAB_REVERSAL: 'prev_pool',
    TAB_TREND: 'strong',
    TAB_ZHABAN: 'zhaban',
    TAB_DTQIAOBAN: 'dtqiaoban',
    TAB_SECTOR: 'limit_up',
}

