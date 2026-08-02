"""多 Tab 回测引擎 (P1.1 骨架)

提供统一的批量回测入口 run_tab_backtest(tab, ...),按 tab 维度调度:
1. 信号池获取 (signal_pool_fetcher)
2. 评分函数 (score_func)  -返回带 score 列的 DataFrame
3. 策略模拟 (开盘买) -复用 t1_real_backtest 的 OHLCV / 聚合
4. 缓存 + 持久化

向后兼容: run_tab_backtest('limit-up', ...) 等价于 t1_real_backtest.run_t1_backtest(...)

设计原则:
- 复用优于重写: OHLCV 拉取 / _is_limit_open / _aggregate 直接 import t1_real_backtest
- 派发表优于 if-else: SIGNAL_POOL_FETCHERS / SCORE_FUNCS 是两个 dict
- 缓存键含 tab: bt_result_{tab}_{start}_{end}_{top_n}_{capital}
"""
import sys, time, os
# 项目根目录 (__file__ 在 backtest/ 子目录, 上翻一级)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime, timedelta

from cache import (
    _is_trading_day,
    get as _cache_get, put as _cache_put,
    persistent_get as _persistent_get, persistent_put as _persistent_put,
    make_key,
)
# 兼容旧 alias (代码里有些地方用 _daily_get/_daily_set 命名空间访问)
_daily_get = _persistent_get
_daily_set = _persistent_put
from config import COMMISSION_ROUNDTRIP_PCT as _COMMISSION_PCT, SLIPPAGE_PCT as _SLIPPAGE_PCT, MAX_PRICE, MAX_MARKET_CAP

# 复用 t1 的工具函数
from t1_real_backtest import (
    _get_ohlcv_batch, _is_limit_open, _next_trading_date,
    CAPITAL_DEFAULT, TOP_N_DEFAULT, MAX_WORKERS,
)

from scanner import (
    filter_non_main_board, filter_xr_xd_dr,
    score_zhaban_data, score_dtqiaoban_data,
    _score_reversal as scanner_score_reversal,
    _score_trend as scanner_score_trend,
    _score_sector as scanner_score_sector,
)
from data_manager import save_backtest_result as _save_backtest_result
# ── 子模块 (2026-08-01 拆分: 常量/指标/信号池/OHLCV/评分) ──
# 本文件保留全部历史符号 (公共 API 不变), 实现见各子模块。
from backtest_tabs import (
    TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR,
    ALL_TABS, TAB_NAMES_CN, _TAB_BUY_TIME, _PENDING_TABS, _SELF_FETCHING_TABS,
)
from backtest_metrics import _deterministic_fill, _aggregate, _compute_factor_ics
from backtest_pools import (
    SIGNAL_POOL_FETCHERS, _detect_available_days, _try_local_fallback,
    _is_placeholder_data, _cached_pool_get, _pool_cache_put,
    _fetch_limit_up_pool, _fetch_reversal_pool, _fetch_zhaban_pool,
    _fetch_dtqiaoban_pool, _fetch_trend_pool, _fetch_sector_pool,
    _TAB_POOL_TYPE, _LOCAL_FALLBACK_ENABLED,
)
from backtest_ohlcv import (
    _get_daily_ohlcv_batch, _try_archive_db_ohlcv, _try_stock_daily_ohlcv,
    _ARCHIVE_DB_PATH, _ARCHIVE_OHLCV_CACHE, _SPOT_DISABLED,
)
from backtest_scores import (
    SCORE_FUNCS, SCORE_COLUMNS, _apply_v2_to_score, _BACKTEST_USE_SCORE_NEW,
    _BACKTEST_USE_V2_DEFAULT, _score_limit_up, _score_zhaban, _score_dtqiaoban,
    _score_reversal, _score_trend, _score_sector,
)



# 本地归档 fallback (P1.3: 回测引擎从本地 pickle 读取历史池数据)
try:
    from archiver import _load_pool_pickle
except ImportError:
    _load_pool_pickle = None  # archiver 未安装时降级

# 是否启用本地归档 fallback (默认启用, 服务器无 akshare 历史数据时从本地读)
_LOCAL_FALLBACK_ENABLED = True

# ─── 回测准确度相关开关 (2026-07-05) ────────────────────────

# ✅ 2026-07-06 修复 v2: 回测与实盘统一用 score_new 排名
# plan_a.score() 在生产中会用 score_new 覆盖 total_score (见 plans/plan_a.py:398-422),
# 回测必须同样使用 score_new 排名, 否则回测验证的是 plan_a 但用户交易的是 score_new。
# plan_a 因子分列保留供 IC 分析和自动调权 (调的是 plan_a 权重, score_new 是独立覆盖层)。

# 是否填仓: 当 top_n 中部分股票一字板买不到时,
# True  = 沿评分列表向下继续扫描, 凑满 top_n 只买入
# False = 买不到的跳过不补, 当日实买数可能 < top_n
# True 更接近实盘操作 (资金要投出去, 买不到第一选择就找第二选择),
# 但可能拉低单笔平均收益 (买入排名更靠后的股票)。
_BACKTEST_FILL_SLOTS = False

# 正确性修复 (2026-08-01): archive.db / stock_daily 构造的 OHLCV
# (buy_open≈信号日收盘、sell_open≈次日收盘) 会系统性高估/失真收益。
# 默认严格模式: 买卖价任一来自构造数据则跳过该笔, 只统计真实历史 OHLCV。
# 可通过 strict_ohlcv=False 关闭(回到含构造价的宽松口径)。
_BACKTEST_STRICT_OHLCV = True

# ═══════════════════════════════════════════
#  Tab 常量
# ═══════════════════════════════════════════

TAB_LIMIT_UP = 'limit-up'
TAB_TREND = 'trend'
TAB_ZHABAN = 'zhaban'
TAB_DTQIAOBAN = 'dtqiaoban'
TAB_REVERSAL = 'reversal'
TAB_SECTOR = 'sector'

ALL_TABS = [TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR]

# ─── 各 tab 默认 best preset (用于 strategy='auto') ────────────────
# 2026-07-05: 用户确认 limit-prime / trend-elite / limit-sweet 等 preset
# 始终不如 plan_a 评分本身 (IC-driven 优化后的 plan_a 评分排序已经
# 包含了合理的过滤逻辑, 无需再用外部 preset 套娃). 删除 _AUTO_PRESETS
# 和 strategy 参数完全无效, run_tab_backtest 永远用 plan_a 评分.
TAB_NAMES_CN = {
    TAB_LIMIT_UP: '涨停扫描',
    TAB_TREND: '趋势扫描',
    TAB_ZHABAN: '炸板反包',
    TAB_DTQIAOBAN: '跌停翘板',
    TAB_REVERSAL: '涨停回调',
    TAB_SECTOR: '板块联动',
}

# v3.0: 各 tab 最优买入策略 (tab → buy_time)
#   close ↔ T日尾盘买 T+1开盘卖 (隔夜超短线)
#   open  ↔ T+1开盘买 T+N开盘卖 (原策略)
# 基于30天回测 PnL 对比选定:
#   limit-up: close  (+2,343) > open (-1,795)
#   dtqiaoban: open  (-2,237) > close (-24,923) 翘板不能隔夜!
#   trend:    open  (-5,504) > close (-9,363)
#   zhaban:   close (-2,727) ≈ open (-3,051)  close略优
#   reversal: open  (-9,212) > close (-12,525)
_TAB_BUY_TIME = {
    TAB_LIMIT_UP: 'close',
    TAB_DTQIAOBAN: 'open',
    TAB_TREND: 'open',
    TAB_ZHABAN: 'open',    # v3.3i: 等次日确认, 不接飞刀
    TAB_REVERSAL: 'open',
    TAB_SECTOR: 'open',
}

# 各 tab 的实现状态 (P1.1 之后逐步点亮)
_PENDING_TABS = set()  # 全部实现
# 已实现: limit-up / zhaban / dtqiaoban / reversal / trend / sector (P1.1 + P2.1 + P2.2 + P2.3)

# 这些 tab 的 score_fn 自行拉数据 (不依赖 fetcher 返回的 pool)
_SELF_FETCHING_TABS = {TAB_SECTOR}
# P1.2.1: OHLCV 批量缓存进程级开关 (默认禁用,服务器环境东方财富 spot 接口不稳定)
# 正确性修复 (2026-08-01): stock_zh_a_spot_em 是"今日实时快照",
# 对历史日期无效 → 一律禁用批量 spot 路径, 回测只用逐股历史 API
# (腾讯/东财 stock_zh_a_hist)。保留开关仅供极端场景手动启用。

# P1.3: 自动检测本地归档可用天数 (不再硬编码 7)
# 每个 tab 的 pool_type 对应 archive_pools/ 中的 pickle 文件名前缀



# ═══════════════════════════════════════════
#  信号池获取函数 (P1.1 骨架: limit-up 和 reversal 可用,其他 TBD 待 P2)
#  P1.3: 所有 fetcher 在 akshare 返回空时 fallback 读本地 pickle 归档
# ═══════════════════════════════════════════





# Tier1.C: archive.db 兜底 OHLCV
# 当 akshare 历史 OHLCV 拉不到时, 用 archive.db daily_stocks + stock_daily 构造简化 OHLCV。
#
# 数据源:
#   - daily_stocks.price:  D 日前一日(昨收) → D close = price * (1 + change_pct/100)
#   - daily_stocks.change_pct:  D 日涨跌幅 (用来推 D 收盘)
#   - daily_stocks.next_day_change:  D+1 日涨跌幅 (D+1 close - D close) / D close
#   - stock_daily.chg_pct:  T+1 (D+1) 的真实涨跌幅 (与 next_day_change 含义类似,
#                         但 next_day_change 已经填了, stock_daily 可用作交叉验证)
#   - stock_daily D+2 (再下一天) 的 chg_pct 可推 T+2 开盘卖的 return
#
# 构造:
#   signal_close (D close)  = price * (1 + change_pct/100)
#   buy_open    (D+1 开盘)  = D close  (假设无跳空, A 股一字板会触发 _is_limit_open)
#   buy_close   (D+1 收盘)  = D close * (1 + next_day_change/100)
#   sell_open   (D+2 开盘)  = buy_close (T+2 数据缺失, 退化)
# 这样 T+1 开盘买的 return = (sell_open / buy_open - 1)
#                       ≈ (D+1 close / D close - 1) = next_day_change (粗略)
# 偏差: 忽略 D+1 跳空和 D+2 走势, 实测偏差 ~1-3%, 但比 skip 强






















# ═══════════════════════════════════════════
#  V2 因子注入 helper
# ═══════════════════════════════════════════
# 目标: 给所有 5 个 tab 的回测评分叠加 mc/pd position_factor, 复用 ACE0597
# 的 limit-up tab 接入路径, 让 trend/reversal/zhaban/dtqiaoban 也吃到
# V2 因子预测力 (数据驱动分析见: commit ace0597, n=2231 票, ρ=+0.0906 p<0.001)





# ═══════════════════════════════════════════
#  评分函数 (P1.1: limit-up / zhaban / dtqiaoban 可用)
# ════════════════════════════════════














# 各 tab 的评分列名

# ═══════════════════════════════════════════
#  OHLCV 批量缓存 (P1.2 性能优化)
# ═══════════════════════════════════════════



# ═══════════════════════════════════════════
#  交易日工具
# ═══════════════════════════════════════════

def _trading_dates_in_range(start_str: str, end_str: str, max_count: int = 60):
    """[start, end] 区间所有交易日,正序"""
    dates = []
    cur = end_str
    while len(dates) < max_count:
        if _is_trading_day(cur):
            dates.append(cur)
        cur_dt = datetime.strptime(cur, '%Y%m%d') - timedelta(days=1)
        cur = cur_dt.strftime('%Y%m%d')
        if cur < start_str:
            break
    dates.reverse()
    return dates


# ═══════════════════════════════════════════
#  聚合辅助 (复用于 t1 的逻辑)
# ═══════════════════════════════════════════





# ═══════════════════════════════════════════
#  IC 因子分析 (回测后, 计算各因子与收益的相关性)
# ═══════════════════════════════════════════



# ═══════════════════════════════════════════
#  主入口: run_tab_backtest
# ═══════════════════════════════════════════

def run_tab_backtest(
    tab: str,
    start_date: str = None,
    end_date: str = None,
    top_n: int = TOP_N_DEFAULT,
    min_score: float = 50.0,
    sell_n: int = 3,
    capital: float = CAPITAL_DEFAULT,
    max_days: int = 30,
    use_cache: bool = True,
    strategy: str = None,
    use_v2: bool = True,
    fill_slots: bool = _BACKTEST_FILL_SLOTS,
    buy_time: str = 'open',
    strict_ohlcv: bool = None,
):
    """多 tab 回测主入口 (v3.0: 支持尾盘买)

    buy_time: 'open'  = T+1 开盘买 T+N 开盘卖 (原策略, 有幸存者偏差)
              'close' = T 日尾盘买 T+1 开盘卖 (隔夜超短线, 评分=T日质量=T+1 gap)

    唯一过滤逻辑 = plan_a 评分 + min_score 阈值 + (sell_n 卖出日).
    历史 strategy='limit-prime'/'trend-elite'/'limit-sweet' preset 已删除,
    strategy 参数保留仅作向后兼容 (被忽略, 即不应用任何额外 preset).

    Args:
        tab: TAB_LIMIT_UP / TAB_TREND / TAB_ZHABAN / TAB_DTQIAOBAN / TAB_REVERSAL / TAB_SECTOR
        start_date/end_date: YYYYMMDD, 默认最近 30 个交易日
        top_n: 每个信号日取前 N 名
        min_score: 最低评分门槛
        sell_n: 卖出日偏移 (2=T+2, 3=T+3, 4=T+4, 5=T+5)
        capital: 单笔本金
        max_days: 默认 30 天
        use_cache: True 走 daily cache
        strategy: [已忽略] 历史 preset 名, 保留兼容性. 真正过滤只靠 min_score.
        use_v2: limit-up tab 是否启用 v2 持续性+回撤位置因子
        fill_slots: True=沿评分列表向下扫描补满 top_n 只买入;
                    False=仅取 top_n 只, 买不到的跳过不补.
        strict_ohlcv: True=买卖价任一来自 archive/stock_daily 构造数据则跳过该笔
                      (默认 _BACKTEST_STRICT_OHLCV=True, 只统计真实历史 OHLCV);
                      False=保留构造价交易 (标记 data_quality='constructed').

    Returns:
        dict: {summary, trades, top5, bottom5, skipped, comparison, generated_at, config}
    """
    # ── 校验 tab ──
    if tab not in ALL_TABS:
        return {
            'summary': {}, 'trades': [], 'skipped': [],
            'generated_at': datetime.now().isoformat(),
            'error': f'未知 tab: {tab}, 支持: {ALL_TABS}',
        }

    # ── 校验评分函数是否已实现 ──
    if tab in _PENDING_TABS:
        return {
            'summary': {}, 'trades': [], 'skipped': [],
            'generated_at': datetime.now().isoformat(),
            'error': f'tab={tab} 的评分函数尚未实现 (当前阶段已实现: {[t for t in ALL_TABS if t not in _PENDING_TABS]})',
        }

    # 2026-07-05: _AUTO_PRESETS / strategy_filters 已删除.
    # strategy 参数保留兼容性但实际不应用任何外部 preset.

    # ── 默认日期 ──
    # 自动检测本地归档可用天数 (超7天时无需手动改配置)。
    # 正确性修复 (2026-08-01): 仅当调用方**未显式指定 start/end** 时才按
    # 本地缓存天数收缩窗口; 显式区间必须原样尊重 (缺数据的日子自然进 skipped),
    # 否则 "30 天回测" 会被悄悄缩成 ~21 天, 且训练/验证窗口相互重叠。
    if start_date is None and end_date is None:
        tab_max = _detect_available_days(tab)
        if max_days > tab_max:
            max_days = tab_max

    if end_date is None:
        from cache import _trading_date as _get_td
        end = _get_td().replace('-', '')
    else:
        end = end_date

    if start_date is None:
        sd = datetime.strptime(end, '%Y%m%d') - timedelta(days=max_days * 2)
        start = sd.strftime('%Y%m%d')
    else:
        start = start_date

    # ── 整体结果缓存 ──
    # 注意: use_v2/fill_slots/strict_ohlcv 必须进 cache_key,
    # version=7 对应 2026-08-01 正确性修复 + 调权闭环 (构造价严格跳过 + 复利累计/
    # 资金曲线回撤 + 确定性成交 + 历史OHLCV数据源修复 + 显式窗口尊重 + 权重更新),
    # 旧 version=6 缓存自动失效。
    # 改权重/评分逻辑时记得递增 version 保证旧缓存失效
    strict = _BACKTEST_STRICT_OHLCV if strict_ohlcv is None else bool(strict_ohlcv)
    if use_cache:
        cache_key = make_key("bt", "result", version=7, tab=tab,
                             start=start, end=end, top_n=top_n,
                             min_score=int(min_score), sell_n=sell_n, capital=int(capital),
                             use_v2="v2" if use_v2 else "nov2",
                             fill_slots="fs" if fill_slots else "nfs",
                             buy_time=buy_time,
                             strict="s" if strict else "ns")
        cached = _daily_get(cache_key)
        if cached and 'summary' in cached:
            return cached

    trade_dates = _trading_dates_in_range(start, end, max_count=max_days)
    if not trade_dates:
        return {
            'summary': {}, 'trades': [], 'skipped': [],
            'generated_at': datetime.now().isoformat(),
            'error': '区间内无交易日',
        }

    # ── 主循环 ──
    fetcher = SIGNAL_POOL_FETCHERS[tab]
    score_fn = SCORE_FUNCS[tab]
    score_col = SCORE_COLUMNS[tab]
    # 2026-07-05: 删除策略B(尾盘买)/策略C(休盘+止损), 仅保留策略A(开盘买)
    records_open, skipped, unbuyable_count = [], [], 0
    total_candidates_scanned = 0  # 填仓模式候选扫描计数

    for d_signal in trade_dates:
        if buy_time == 'close':
            # ── 策略B: T日尾盘买 → T+1 开盘卖 (隔夜超短线) ──
            d_sell = _next_trading_date(d_signal)
            if d_sell is None or d_sell > trade_dates[-1]:
                skipped.append({'signal': d_signal, 'reason': '卖出日超出区间'})
                continue
            d_buy = d_signal  # 同日买
        else:
            # ── 策略A: T+1 开盘买 → T+N 开盘卖 (原策略) ──
            d_buy = _next_trading_date(d_signal)
            if d_buy is None or d_buy > trade_dates[-1]:
                skipped.append({'signal': d_signal, 'reason': '买入日超出区间'})
                continue
            # 多时点卖出：sell_n=T+N 卖出(N 是信号日后的偏移交易日数)
            d_sell = d_buy
            for _si in range(max(0, sell_n - 1)):
                d_sell = _next_trading_date(d_sell)
                if d_sell is None:
                    break
            if d_sell is None:
                skipped.append({'signal': d_signal, 'reason': '卖出日无效'})
                continue
            if d_sell is None or d_sell > trade_dates[-1]:
                skipped.append({'signal': d_signal, 'reason': '卖出日超出区间'})
                continue

        n_scanned_this_day = 0
        try:
            pool = fetcher(d_signal)
            # 板块 tab 等特殊场景: pool 为 None, 由 score_fn 自行处理 (内部拉数据)
            if pool is None:
                if tab in _SELF_FETCHING_TABS:
                    df_scored = score_fn(None, d_signal, capital=capital)
                else:
                    skipped.append({'signal': d_signal, 'reason': '信号池空'})
                    continue
            else:
                try:
                    if hasattr(pool, 'empty') and pool.empty:
                        skipped.append({'signal': d_signal, 'reason': '信号池空'})
                        continue
                except ValueError:
                    pass
                df_scored = score_fn(pool, d_signal, capital=capital)

            if df_scored is None:
                skipped.append({'signal': d_signal, 'reason': '评分后空'})
                continue
            try:
                if hasattr(df_scored, 'empty') and df_scored.empty:
                    skipped.append({'signal': d_signal, 'reason': '评分后空'})
                    continue
            except ValueError:
                pass

            # 评分列容错查找
            actual_score_col = None
            for cand in [score_col, '回测评分', '综合分', '总分', '评分', '翘板评分', '反转评分', '动量评分', '板块强度']:
                if cand in df_scored.columns:
                    actual_score_col = cand
                    break
            if actual_score_col is None:
                skipped.append({'signal': d_signal, 'reason': f'找不到评分列 (尝试过 {score_col})'})
                continue


            # 名称列容错
            name_col = None
            for cand in ['名称', '股票名称']:
                if cand in df_scored.columns:
                    name_col = cand
                    break
            name_col = name_col or df_scored.columns[2]
            code_col = None
            for cand in ['代码']:
                if cand in df_scored.columns:
                    code_col = cand
                    break
            code_col = code_col or df_scored.columns[1]

            # 取评分合格的候选池
            eligible = df_scored[df_scored[actual_score_col] >= min_score]
            skipped_count = max(0, len(df_scored) - len(eligible))
            sorted_eligible = eligible.sort_values(actual_score_col, ascending=False)
            if fill_slots:
                candidates = sorted_eligible  # 扫描全部, 直到凑满 top_n 只
            else:
                candidates = sorted_eligible.head(top_n)

            # ── P1.2 优化: 提前批量拉取 3 个日期的全市场 OHLCV ──
            ohlcv_dates = [d_signal, d_buy, d_sell]
            daily_ohlcv = {}
            for d in ohlcv_dates:
                daily_ohlcv[d] = _get_daily_ohlcv_batch(d)

            bought_count = 0       # 填仓模式下已买入笔数
            n_scanned_this_day = 0  # 本日扫描了多少只候选股票
            for rank, (_, row) in enumerate(candidates.iterrows(), 1):
                if fill_slots and bought_count >= top_n:
                    break
                n_scanned_this_day += 1
                code = str(row.get(code_col, '') or row.iloc[0]).strip().zfill(6)
                name = str(row.get(name_col, '') or row.iloc[0])
                sc = float(row.get(actual_score_col, 0))

                # 优先用批量缓存,缺失则降级逐股拉取
                signal_ohlcv = daily_ohlcv.get(d_signal, {}).get(code)
                buy_ohlcv = daily_ohlcv.get(d_buy, {}).get(code)
                sell_ohlcv = daily_ohlcv.get(d_sell, {}).get(code)
                if not all([signal_ohlcv, buy_ohlcv, sell_ohlcv]):
                    # 降级: 逐股拉取
                    ohlcv_map = _get_ohlcv_batch(code, [d_signal, d_buy, d_sell])
                    signal_ohlcv = signal_ohlcv or ohlcv_map.get(d_signal)
                    buy_ohlcv = buy_ohlcv or ohlcv_map.get(d_buy)
                    sell_ohlcv = sell_ohlcv or ohlcv_map.get(d_sell)
                if not all([signal_ohlcv, buy_ohlcv, sell_ohlcv]):
                    # Tier1.C: archive.db next_day_change fallback
                    # 修复: signal/buy/sell 三个日期分别查 archive.db
                    # 卖价从 d_sell 日的 prev_close 反推 (≈d_sell开盘),
                    # 比之前三个日期共用信号日 OHLCV 更准确。
                    stock_type_for_arch = {
                        TAB_LIMIT_UP: 'limit_up',
                        TAB_ZHABAN: 'limit_up',
                        TAB_REVERSAL: 'limit_up',
                        TAB_DTQIAOBAN: 'limit_up',
                        TAB_TREND: 'limit_up',
                        TAB_SECTOR: 'limit_up',
                    }.get(tab, 'limit_up')
                    if not signal_ohlcv:
                        signal_ohlcv = _try_archive_db_ohlcv(code, d_signal, stock_type_for_arch)
                    # v3.3i: 炸板/翘板不用 archive 兜底 — limit_up 记录的价格是涨停价,
                    # 但炸板票实际收盘远低于涨停价, archive 构造的 OHLCV 买价虚高
                    _skip_archive = tab in (TAB_ZHABAN, TAB_DTQIAOBAN)
                    if not buy_ohlcv and not _skip_archive:
                        buy_arch = _try_archive_db_ohlcv(code, d_buy, stock_type_for_arch)
                        if buy_arch is not None:
                            buy_ohlcv = dict(buy_arch)
                            buy_ohlcv['_fallback'] = 'archive_buy'
                    if not sell_ohlcv and not _skip_archive:
                        sell_arch = _try_archive_db_ohlcv(code, d_sell, stock_type_for_arch)
                        if sell_arch is not None:
                            sell_ohlcv = dict(sell_arch)
                            sell_ohlcv['_sell_open'] = sell_arch.get('close', sell_arch['open'])
                            sell_ohlcv['_fallback'] = 'archive_sell'
                if not all([signal_ohlcv, buy_ohlcv, sell_ohlcv]):
                    missing = []
                    if not signal_ohlcv: missing.append(f'signal={d_signal}')
                    if not buy_ohlcv: missing.append(f'buy={d_buy}')
                    if not sell_ohlcv: missing.append(f'sell={d_sell}')
                    skipped.append({'signal': d_signal, 'reason': f'{name}({code}) OHLCV缺失: {", ".join(missing)}'})
                    continue

                # ── P2 修复: 跳过 stock_daily 归一化假数据 (价格基准 100, 完全不可信) ──
                if buy_ohlcv.get('_normalized') or sell_ohlcv.get('_normalized'):
                    skipped.append({'signal': d_signal, 'reason': f'{name}({code}) 买卖价来自归一化假数据, 跳过'})
                    unbuyable_count += 1
                    continue
                # archive.db 构造数据 (_fallback) ≠ 完全假, 但 buy_open=d_close 忽略跳空
                # → 标记 trade 为低可信, 不入统计 / 仅当 gap 可控时保留
                _is_constructed_buy = bool(buy_ohlcv.get('_fallback'))
                _is_constructed_sell = bool(sell_ohlcv.get('_fallback'))
                _is_constructed_signal = bool(signal_ohlcv.get('_fallback'))
                _is_constructed = _is_constructed_buy or _is_constructed_sell or _is_constructed_signal
                if _is_constructed:
                    print(f"  [OHLCV] {name}({code}) {d_signal}: "
                          f"{'买' if _is_constructed_buy else ''}{'卖' if _is_constructed_sell else ''}"
                          f"价来自 archive.db 构造, 收益可能不准",
                          file=sys.stderr)
                # 正确性修复: 严格模式跳过构造价交易 (系统性高估/失真收益)
                if strict and _is_constructed:
                    skipped.append({'signal': d_signal,
                                    'reason': f'{name}({code}) 构造OHLCV价(archive/stock_daily)不可信, 严格模式跳过'})
                    continue
                data_quality = 'constructed' if _is_constructed else 'real'

                signal_close = signal_ohlcv['close']
                buy_open_real = buy_ohlcv['open']
                gap_pct = round((buy_open_real / signal_close - 1) * 100, 1)

                # ── P3 修复: 跳空高开买入偏差 ──
                # 原逻辑: 仅 gap>9.5% 视为买不到 (太宽松, 7-9%跳空实际也买不到)
                # 新逻辑:
                #   gap >= 9.5%: 一字板/秒板, 买不到 (limit_open)
                #   5% ≤ gap < 9.5%: 高开,可买但买不到开盘价,用 signal_close*1.05 模拟
                #   gap < 5%: 正常开盘, 用 buy_open 买入
                is_normalized = signal_ohlcv.get('_normalized', False)
                limit_open = (not is_normalized) and _is_limit_open(buy_ohlcv, signal_close)  # gap>=9.5
                _gap_medium_threshold = 5.0  # 5% 以上跳空不能按开盘价买入
                is_gap_medium = (not is_normalized) and (gap_pct > _gap_medium_threshold) and not limit_open
                buyable = not limit_open
                # v3.3i: 炸板隔夜确认 — 次日开盘崩>3%不买(飞刀还在掉)
                if tab == TAB_ZHABAN and buyable and not is_normalized:
                    if gap_pct < -3.0:
                        buyable = False
                        skipped.append({'signal': d_signal, 'reason': f'{name} 炸板隔夜崩{gap_pct:+.1f}%, 不接飞刀'})
                        continue
                    elif gap_pct < -1.0:
                        # 微跌开盘: 能买到但用实际开盘价(偏低价买入=更好的入场)
                        pass  # buyable stays True, buy at open
                if not buyable:
                    unbuyable_count += 1
                    skipped.append({'signal': d_signal, 'reason': f'{name} 跳空{gap_pct:+.1f}%>=9.5%一字板'})
                # gap 5-9.5%: 能买到但买不到开盘价, 用 signal_close*1.05 模拟排队买入
                if is_gap_medium:
                    _adjusted_buy_px = round(signal_close * 1.05, 2)
                    print(f"  [跳空偏差] {name}({code}) gap={gap_pct:+.1f}%, "
                          f"买入价 {buy_open_real}→{_adjusted_buy_px}(排队模拟)",
                          file=sys.stderr)
                    # override buy price — 但保留原始 buy_open 在 intraday 里供参考
                    buy_ohlcv = dict(buy_ohlcv)  # copy
                    buy_ohlcv['_adjusted_buy_px'] = _adjusted_buy_px

                if sell_ohlcv is None:
                    missing.append(f'sell={d_sell}')
                    skipped.append({'signal': d_signal, 'reason': f'{name}({code}) OHLCV缺失: sell={d_sell}'})
                    continue

                intraday = {
                    'buy_high': round(buy_ohlcv['high'], 2),
                    'buy_low': round(buy_ohlcv['low'], 2),
                    'buy_close': round(buy_ohlcv['close'], 2),
                    'buy_turnover': round(buy_ohlcv['turnover'], 2),
                    'sell_high': round(sell_ohlcv.get('high', 0), 2) if sell_ohlcv else 0,
                    'sell_low': round(sell_ohlcv.get('low', 0), 2) if sell_ohlcv else 0,
                    'sell_close': round(sell_ohlcv.get('close', 0), 2) if sell_ohlcv else 0,
                    'signal_close': round(signal_close, 2),
                    'gap_open_pct': gap_pct,
                    'buyable': buyable,
                    'adjusted_buy_px': round(buy_ohlcv.get('_adjusted_buy_px', 0), 2) if buy_ohlcv.get('_adjusted_buy_px') else None,
                }

                sell_px = sell_ohlcv.get('_sell_open') or sell_ohlcv['open']

                if buy_time == 'close':
                    # ── 策略B: T日尾盘买 → T+1开盘卖 (隔夜超短线) ──
                    # v3.3f: 只有涨停板才需要封单成交比尾盘可买到建模
                    #   炸板(zhaban)收盘价低于涨停价, 没有买不到的问题
                    if tab == TAB_LIMIT_UP:
                        _seal_fund = float(row.get('封板资金', 0) or 0)
                        _amount = float(row.get('成交额', 0) or 1)
                        _seal_ratio = _seal_fund / _amount if _amount > 0 else 0
                        if _seal_ratio > 2.0:
                            unbuyable_count += 1
                            skipped.append({'signal': d_signal, 'reason': f'{name} 封单成交比{_seal_ratio:.1f}>2.0, 尾盘封死买不到'})
                            continue
                        elif _seal_ratio > 1.0:
                            # 正确性修复: 原 random.random() 让回测不可复现;
                            # 改为 (code,date) 确定性哈希, 结果稳定可复现。
                            _fill_prob = max(0.0, 2.0 - _seal_ratio)  # 1.0→100%, 1.5→50%, 2.0→0%
                            if not _deterministic_fill(code, d_signal, _fill_prob):
                                skipped.append({'signal': d_signal, 'reason': f'{name} 封单成交比{_seal_ratio:.1f}, 排队未成交(确定性模拟)'})
                                continue
                        rec_seal_ratio = round(_seal_ratio, 2)
                    else:
                        rec_seal_ratio = 0
                    buy_px = signal_ohlcv['close']
                    raw_ret = (sell_px / buy_px - 1) * 100
                    net_ret = raw_ret - _COMMISSION_PCT - _SLIPPAGE_PCT
                    rec = {
                        'signal_date': d_signal, 'buy_date': d_signal, 'sell_date': d_sell,
                        'rank': rank, 'code': code, 'name': name, 'score': round(sc, 1),
                        'buy_price': round(buy_px, 2), 'sell_price': round(sell_px, 2),
                        'raw_ret_pct': round(raw_ret, 2), 'net_ret_pct': round(net_ret, 2),
                        'pnl': round(capital * net_ret / 100, 0), **intraday,
                        'seal_ratio': rec_seal_ratio, 'data_quality': data_quality,
                    }
                    # 因子分列 (与 open-buy 分支一致, 供 IC 分析/调权)
                    for fk in ['trend_chg','trend_turnover','trend_amount','trend_vr','trend_nh','trend_ma',
                               'rev_turnover','rev_consecutive','rev_pullback','rev_sector',
                               'zb_seal','zb_money','zb_feature','zb_turnover','zb_sector','zb_market_cap',
                               'dt_deal','dt_seal','dt_cont','dt_turnover','dt_time']:
                        val = row.get(fk)
                        if val is not None:
                            rec[fk] = round(float(val), 1)
                    for fk in ['f_alpha','f_seal','f_money','f_sector','f_tech','f_history',
                               'f_stock_sentiment','f_principal','f_north_flow',
                               'f_v2_mc','f_v2_pd',
                               'f_seal_ratio','f_seal_time','f_turnover','f_consecutive',
                               'f_zhaban','f_market_cap','f_price']:
                        val = row.get(fk)
                        if val is not None:
                            rec[fk] = round(float(val), 1)
                    records_open.append(rec)
                    bought_count += 1
                elif buyable:
                    # ── 策略A: T+1开盘买 → T+N开盘卖 (原策略) ──
                    # P3 修复: gap 5-9.5% 时用排队模拟价而非开盘价
                    buy_px = buy_ohlcv.get('_adjusted_buy_px', buy_ohlcv['open'])
                    raw_ret = (sell_px / buy_px - 1) * 100
                    net_ret = raw_ret - _COMMISSION_PCT - _SLIPPAGE_PCT
                    rec = {
                        'signal_date': d_signal, 'buy_date': d_buy, 'sell_date': d_sell,
                        'rank': rank, 'code': code, 'name': name, 'score': round(sc, 1),
                        'buy_price': round(buy_px, 2), 'sell_price': round(sell_px, 2),
                        'raw_ret_pct': round(raw_ret, 2), 'net_ret_pct': round(net_ret, 2),
                        'pnl': round(capital * net_ret / 100, 0), **intraday,
                        'data_quality': data_quality,
                    }
                    # P4: 趋势因子分列(供调权)
                    for fk in ['trend_chg','trend_turnover','trend_amount','trend_vr','trend_nh','trend_ma',
                               'rev_turnover','rev_consecutive','rev_pullback','rev_sector',
                               'zb_seal','zb_money','zb_feature','zb_turnover','zb_sector','zb_market_cap',
                               'dt_deal','dt_seal','dt_cont','dt_turnover','dt_time']:
                        val = row.get(fk)
                        if val is not None:
                            rec[fk] = round(float(val), 1)
                    # IC 因子分列 (f_ 前缀, 用于 Information Coefficient 分析)
                    # plan_a 因子 + score_new 因子 + v2 因子 (v3.3d: v2 适用于所有tab)
                    for fk in ['f_alpha','f_seal','f_money','f_sector','f_tech','f_history',
                               'f_stock_sentiment','f_principal','f_north_flow',
                               'f_v2_mc','f_v2_pd',
                               'f_seal_ratio','f_seal_time','f_turnover','f_consecutive',
                               'f_zhaban','f_market_cap','f_sector','f_price']:
                        val = row.get(fk)
                        if val is not None:
                            rec[fk] = round(float(val), 1)
                    records_open.append(rec)
                    bought_count += 1  # 填仓计数

        except Exception as e:
            skipped.append({'signal': d_signal, 'reason': f'错误: {str(e)[:80]}'})
        time.sleep(0.5)
        total_candidates_scanned += n_scanned_this_day

    # ── 聚合 ──
    sum_open = _aggregate(records_open, '开盘买')

    # ── 近30天聚合 ──
    cutoff_30d = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
    records_open_30d = [r for r in records_open if r['signal_date'] >= cutoff_30d]
    sum_open_30d = _aggregate(records_open_30d, '开盘买(近30天)')

    if sum_open is None:
        empty_summary = {'trade_count': 0, 'win_rate': 0, 'avg_ret': 0,
                    'total_pnl': 0, 'plr': 0, 'max_dd': 0, 'best': 0,
                    'worst': 0, 'ev': 0, 'cumulative_ret': 0}
        result = {
            'summary': dict(empty_summary),
            'summary_30d': dict(empty_summary),
            'trades': [],
            'skipped': skipped,
            'generated_at': datetime.now().isoformat(),
            'config': {'tab': tab, 'start': start, 'end': end, 'top_n': top_n, 'sell_n': sell_n, 'capital': capital},
            'error': '无有效交易',
        }
        return result

    # ── 策略过滤器 (2026-07-05 删除: 不再用外部 preset) ──
    # 历史 limit-prime / trend-elite / limit-sweet 已删除, 唯一过滤逻辑
    # = plan_a 评分 + min_score 阈值 + sell_n 卖出日.
    # strategy 参数保留兼容但被忽略 (永远等价 None).

    sorted_trades = sorted(records_open, key=lambda x: -x['net_ret_pct']) if records_open else []
    # P1.24.3 修复: top5/bot5 在数据少时 overlap (用户报"买/卖反"实为 bot5 混入赚票)
    # 正确做法: 总数 N < 10 时, top5 取前 ceil(N/2) 条, bot5 取后 floor(N/2) 条
    # 总数 N >= 10 时, top5/bot5 各 5 条, 互斥 (因 N>=10 时重叠概率为 0)
    n_total = len(sorted_trades)
    if n_total == 0:
        top5, bot5 = [], []
    elif n_total < 10:
        top_n_cnt = (n_total + 1) // 2  # ceil(n/2): 3→2, 4→2, 5→3, ...
        bot_n = n_total - top_n_cnt
        top5 = sorted_trades[:top_n_cnt]
        bot5 = sorted_trades[-bot_n:][::-1] if bot_n > 0 else []
    else:
        top5 = sorted_trades[:5]
        bot5 = sorted_trades[-5:][::-1]

    # 填仓模式诊断数据
    fill_diag = {
        'fill_slots': fill_slots,
        'total_scanned': total_candidates_scanned,
        'total_bought': len(records_open),
        'scan_efficiency_pct': round(len(records_open) / max(1, total_candidates_scanned) * 100, 1),
    }
    # 因子 IC 分析
    factor_ics = _compute_factor_ics(records_open, tab=tab)
    result = {
        'summary': sum_open,
        'summary_30d': sum_open_30d,
        'trades': records_open,
        'top5': top5, 'bottom5': bot5,
        'skipped': skipped,
        'fill_diagnostics': fill_diag,
        'factor_ics': factor_ics,
        'generated_at': datetime.now().isoformat(),
        'config': {
            'tab': tab, 'start': start, 'end': end,
            'top_n': top_n, 'min_score': min_score, 'sell_n': sell_n, 'capital': capital,
            'commission_pct': _COMMISSION_PCT,
            'slippage_pct': _SLIPPAGE_PCT,
            'strategy': '尾盘买 T收→T+1开' if buy_time == 'close' else '开盘买 T+1开→T+N开',
            'fill_slots': fill_slots,
            'buy_time': buy_time,
        },
        'comparison': {
            'open_buy': {'summary': sum_open, 'trades': records_open},
            'unbuyable_count': unbuyable_count,
        },
    }

    # 缓存
    if use_cache:
        _daily_set(cache_key, result)

    # 持久化
    try:
        _save_backtest_result(result)
    except Exception as _e:
        print(f"  [引擎持久化] 写入失败: {_e}", file=sys.stderr)

    # 保存 tab 表现 → 供 tab 仓位权重参考 (不做因子调权)
    try:
        from weight_manager import save_tab_performance
        save_tab_performance(tab, result.get('summary', {}))
    except Exception:
        pass

    # 因子级自动调权 — ⛔ 已禁用，权重已手动优化锁定
    # from scanner import get_market_status
    # if get_market_status() == 'trading':
    #     return result
    # if tab == TAB_TREND and records_open:
    #     from weight_manager import adjust_trend_weights_from_backtest
    #     new_w, msg = adjust_trend_weights_from_backtest(records_open)
    # if tab == TAB_REVERSAL and records_open:
    #     from weight_manager import adjust_reversal_weights_from_backtest
    #     new_w, msg = adjust_reversal_weights_from_backtest(records_open)
    # if tab in (TAB_ZHABAN, TAB_DTQIAOBAN) and records_open:
    #     from weight_manager import adjust_tab_weights_from_backtest
    #     new_w, msg = adjust_tab_weights_from_backtest(tab, records_open)

    return result


# ═══════════════════════════════════════════
#  向后兼容: run_t1_backtest = run_tab_backtest('limit-up', ...)
# ═══════════════════════════════════════════

def run_tab_backtest_auto(tab: str, **kwargs):
    """按 tab 自动选择最优买入策略后跑回测 (v3.0)
    覆盖: 显式传 buy_time 则使用传入值
    """
    bt = kwargs.pop('buy_time', _TAB_BUY_TIME.get(tab, 'open'))
    return run_tab_backtest(tab=tab, buy_time=bt, **kwargs)


def run_t1_backtest(start_date=None, end_date=None, top_n=TOP_N_DEFAULT,
                     capital=CAPITAL_DEFAULT, max_days=30, use_cache=True,
                     fill_slots: bool = _BACKTEST_FILL_SLOTS):
    """涨停 tab 的 T+1 回测 - 向后兼容别名"""
    return run_tab_backtest(
        tab=TAB_LIMIT_UP,
        start_date=start_date, end_date=end_date,
        top_n=top_n, capital=capital, max_days=max_days, use_cache=use_cache,
        fill_slots=fill_slots,
    )


# ═══════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import argparse, json
    parser = argparse.ArgumentParser(description='多 Tab 回测引擎')
    parser.add_argument('--tab', default='limit-up',
                        choices=ALL_TABS + ['all'],
                        help='回测 tab,默认 limit-up')
    parser.add_argument('--days', type=int, default=5, help='回测天数 (默认 5,aksahre 实际可用窗口限制)')
    parser.add_argument('--top', type=int, default=TOP_N_DEFAULT, help='每日 TOP N')
    parser.add_argument('--capital', type=float, default=CAPITAL_DEFAULT, help='单笔本金')
    parser.add_argument('--fill-slots', action='store_true', default=False,
                        help='填仓模式: 买不到的票往下续, 凑满 top_n 只')
    parser.add_argument('--close-buy', action='store_true', default=False,
                        help='尾盘买模式: T日收盘买入 T+1开盘卖出')
    args = parser.parse_args()

    tabs_to_run = ALL_TABS if args.tab == 'all' else [args.tab]
    for tab in tabs_to_run:
        print(f"\n{'='*70}")
        print(f"  Tab: {tab} ({TAB_NAMES_CN[tab]}) | TOP{args.top} | {args.days}天回测"
              f"{' | 填仓' if args.fill_slots else ''}")
        print('='*70)
        res = run_tab_backtest(tab=tab, top_n=args.top, capital=args.capital,
                               max_days=args.days, use_cache=False,
                               fill_slots=args.fill_slots,
                               buy_time='close' if args.close_buy else 'open')
        if 'error' in res and not res.get('trades'):
            print(f"  [跳过] {res.get('error')}")
            continue
        s = res['summary']
        print(f"  笔数: {s.get('trade_count', 0)}")
        print(f"  胜率: {s.get('win_rate', 0)}%")
        print(f"  累计收益: {s.get('cumulative_ret', 0):+.2f}%")
        print(f"  总盈亏: ¥{s.get('total_pnl', 0):+,.0f}")
        print(f"  盈亏比: {s.get('plr', 0)}")
        print(f"  最大回撤: {s.get('max_dd', 0):.2f}%")
        print(f"  期望值: {s.get('ev', 0):+.2f}%")
        print(f"  最优: {s.get('best', 0):+.2f}%  最差: {s.get('worst', 0):+.2f}%")
        cmp = res.get('comparison', {})
        print(f"  一字板跳过: {cmp.get('unbuyable_count', 0)} 笔")
        print(f"  跳过信号日: {len(res.get('skipped', []))} 个")
        fd = res.get('fill_diagnostics', {})
        if fd.get('fill_slots'):
            print(f"  扫描效率: {fd.get('scan_efficiency_pct', 0)}% ({fd.get('total_bought', 0)}买/{fd.get('total_scanned', 0)}扫描)")
        ic = res.get('factor_ics', {})
        if ic:
            sorted_ic = sorted(ic.items(), key=lambda x: -abs(x[1]))
            ic_str = ' | '.join(f'{k}: {v:+.4f}' for k, v in sorted_ic)
            print(f"  因子IC(|r|排序): {ic_str}")
