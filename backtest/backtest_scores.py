"""6 tab 回测评分包装 (2026-08-01 自 backtest_engine.py 拆分)。

每个 _score_xxx(df, date_str, capital) 输出带排名评分列的 DataFrame;
SCORE_FUNCS / SCORE_COLUMNS 供主循环派发。权重统一走 weight_manager.load_tab_weights
(score_new 权重经 scoring.score_new.load_factor_weights)。
"""
import sys
import os
import pandas as pd

from scanner import (
    filter_non_main_board, filter_xr_xd_dr,
    score_zhaban_data, score_dtqiaoban_data,
    _score_reversal as scanner_score_reversal,
    _score_trend as scanner_score_trend,
    _score_sector as scanner_score_sector,
)
from config import MAX_PRICE, MAX_MARKET_CAP
from t1_real_backtest import CAPITAL_DEFAULT, TOP_N_DEFAULT
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
from backtest_tabs import (
    TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR,
)

_BACKTEST_USE_SCORE_NEW = True
_BACKTEST_USE_V2_DEFAULT = True
def _apply_v2_to_score(df: pd.DataFrame, score_col: str, today_str: str,
                       use_v2: bool = None) -> pd.DataFrame:
    """对 ``df[score_col]`` 乘上 position_factor 调整。

    factor = (0.85 + mc/50) * (0.90 + pd/50)
        mc=10 pd=10 → 1.05 × 1.10 = 1.155 (+15.5%)
        mc=0  pd=0  → 0.85 × 0.90 = 0.765 (-23.5%)
        mc=pd=5 (中性) → 0.95 × 1.00 = 0.95 (-5%, 与 baseline 校准)

    历史票 mc/pd 拿不到时降为 5.0 → factor=0.95。这是 archive.db 在 v8/v9/v10
    cache 升级后被 backfill_archive.py (commit 7b1549f) 补回的覆盖 — 现在 mc/pd
    真值命中率 ~50%, 50% 票用 5.0 默认温和调整。

    Args:
        df: 评分后 DataFrame (含 '代码'/'最新价'/'名称' 列)
        score_col: 要调整的评分列名 (如 '动量评分', '反转评分', '总分', '翘板评分')
        today_str: YYYYMMDD 格式, 内部转 YYYY-MM-DD 给 factors_v2
        use_v2: None=读 _BACKTEST_USE_V2_DEFAULT, True/False=强制
    """
    if df is None or df.empty:
        return df
    if use_v2 is None:
        use_v2 = _BACKTEST_USE_V2_DEFAULT
    if not use_v2:
        return df
    try:
        from plans.factors_v2 import compute_v2_factors as _compute_v2
        today_iso = f'{today_str[:4]}-{today_str[4:6]}-{today_str[6:8]}'
        v2 = _compute_v2(df, today_iso)
        mc = v2['momentum_consistency']
        pd_ = v2['pullback_depth']
        mc_factor = 0.85 + mc / 50.0
        pd_factor = 0.90 + pd_ / 50.0
        position = (mc_factor * pd_factor).reindex(df.index, fill_value=0.95)
        df = df.copy()
        df[score_col] = (df[score_col].astype(float) * position).round(1)
    except Exception as e:
        # V2 不可用 (历史不足等) — 静默回退到 baseline, 不阻塞回测
        print(f'  [_apply_v2_to_score] {today_str} 跳过: {type(e).__name__}: {str(e)[:80]}',
              file=__import__('sys').stderr)
    return df
def _score_limit_up(df: pd.DataFrame, date_str: str, capital=None):
    """涨停评分: plan_a 9因子 (与前端排名一致)

    P3.1: 替代 backtest_score_prev (6因子简化版),
    直接调用 plan_a 完整评分管道, 回测胜率与实盘推荐对齐。
    fund_df/history_scores/lhb_bonus 等实时数据用降级模式(全零/默认值)。
    capital: 本金 (用于本金过滤 + score_by_principal), 默认 CAPITAL_DEFAULT。
    """
    if df is None or df.empty:
        return None

    # plan_a 内部 auto_verify_backtest 需要 YYYYMMDD 格式
    today_fmt = date_str

    # 1. 过滤 (保留全池做 scoring_base, 与 scan pipeline 对齐归一化)
    from scanner import filter_non_main_board
    scoring_base = filter_non_main_board(df.copy())
    if scoring_base.empty:
        return None

    cap_col = '流通市值' if '流通市值' in scoring_base.columns else None
    if cap_col and cap_col in scoring_base.columns:
        scoring_base = scoring_base[scoring_base[cap_col].astype(float) <= 200 * 1e8]
    price_col = '最新价' if '最新价' in scoring_base.columns else (scoring_base.columns[4] if len(scoring_base.columns) > 4 else None)
    if price_col and price_col in scoring_base.columns:
        scoring_base = scoring_base[scoring_base[price_col].astype(float) <= MAX_PRICE]
    if scoring_base.empty:
        return None

    filtered = scoring_base.copy()

    # ── P4 修复: 本金过滤 (与实盘 _principal_filter 一致, 至少买 2 手) ──
    try:
        from scanner import _dynamic_positions
        _n_positions = _dynamic_positions(principal)
        _position_size = principal / _n_positions
        _price_col = '最新价' if '最新价' in filtered.columns else (filtered.columns[4] if len(filtered.columns) > 4 else filtered.columns[3])
        _mask = pd.Series(True, index=filtered.index)
        for _idx in filtered.index:
            _p = float(filtered.loc[_idx, _price_col])
            if _position_size / (_p * 100) < 2:
                _mask[_idx] = False
        _excluded = (~_mask).sum()
        if _excluded > 0:
            print(f"  [PlanA/回测] 本金过滤排除 {_excluded} 只 (本金{principal}买不了2手)", file=sys.stderr)
        filtered = filtered[_mask]
    except Exception:
        pass
    if filtered.empty:
        return None

    # 2. plan_a 因子计算 (在 scoring_base 上归一化, 与前端一致)
    from plans.plan_a import compute_factors, apply_scores
    # 正确性修复: 原硬编码 30000, 与 run_tab_backtest(capital=) 脱钩
    principal = capital or CAPITAL_DEFAULT

    # P3.2: 尝试加载历史归档的实时数据
    try:
        from archiver import load_scan_inputs
        archived = load_scan_inputs(date_str)
    except Exception:
        archived = None

    if archived is not None:
        fund_df = archived.get('fund_df')
        sentiment_score = archived.get('sentiment_score', 3.0)
        sentiment_level = archived.get('sentiment_level', 'neutral')
        sentiment_detail = archived.get('sentiment_detail', {})
        sentiment_ok = archived.get('sentiment_ok', True)
        # lhb_bonus/history_scores 重新对齐到当前 filtered.index
        lhb_raw = archived.get('lhb_bonus', pd.Series(0.0, index=filtered.index))
        if lhb_raw is not None and hasattr(lhb_raw, 'reindex'):
            lhb_bonus = lhb_raw.reindex(filtered.index, fill_value=0.0)
        else:
            lhb_bonus = pd.Series(0.0, index=filtered.index)
        hist_raw = archived.get('history_scores', pd.Series(2.5, index=filtered.index))
        if hist_raw is not None and hasattr(hist_raw, 'reindex'):
            history_scores = hist_raw.reindex(filtered.index, fill_value=2.5)
        else:
            history_scores = pd.Series(2.5, index=filtered.index)
    else:
        fund_df = None
        sentiment_score = 3.0
        sentiment_level = 'neutral'
        sentiment_detail = {}
        sentiment_ok = True
        lhb_bonus = pd.Series(0.0, index=filtered.index)
        history_scores = pd.Series(2.5, index=filtered.index)

    factors = compute_factors(scoring_base, fund_df=fund_df, principal=principal,
                              today_str=date_str)

    # v2 因子 (持续性 + 回撤位置) — 回测路径此前缺失, 导致 mc/pd 一律 5.0,
    # 等价于 baseline (use_v2=False). 现补回显式注入, 与 plan_a.score() 前端路径对齐。
    from plans.factors_v2 import compute_v2_factors as _compute_v2
    v2_factors = _compute_v2(scoring_base, today_fmt)
    factors['momentum_consistency'] = v2_factors['momentum_consistency']
    factors['pullback_depth'] = v2_factors['pullback_depth']
    n_with_hist = int((v2_factors['momentum_consistency'] != 5.0).sum())
    print(f"  [PlanA v2 / backtest] {n_with_hist}/{len(scoring_base)} 票有历史, mc/pd 启用",
          file=sys.stderr)

    # 使用与生产环境一致的权重（load_weights = DEFAULT_WEIGHTS，live 端也是用同一份）
    # BUG-FIX 2026-07-14: 此前误用 _load_tab_weights('limit-up')（DEFAULT_WEIGHTS_LIMIT_UP，
    # seal=20/sector=25），而 live 端 plan_a.score()→apply_scores 未传 weights 时
    # 走 load_weights()（DEFAULT_WEIGHTS, seal=28/sector=17/money=17/alpha=8）→ 两套权重
    # 不一致，导致回测结果与实盘不可比。现统一为 load_weights()。
    from weight_manager import load_weights
    limit_up_weights = load_weights()

    # use_v2=True 已经内置在 apply_scores 路径 (factors 里已注入 mc/pd),
    # 无需再走 _apply_v2_to_score (那是给其它 tab 评分函数末位套用的 helper)
    total_scores, base_scores, danger_flags, weights = apply_scores(
        filtered, factors, sentiment_score, history_scores, lhb_bonus, today_fmt,
        weights=limit_up_weights, use_v2=_BACKTEST_USE_V2_DEFAULT)

    # 3. 附加评分列到 DataFrame
    filtered = filtered.copy()
    filtered['plan_a总分'] = total_scores.round(1)
    filtered['plan_a基础分'] = base_scores.round(1)
    # 危险信号标记
    filtered['_danger'] = ''
    for idx, flags in danger_flags.items():
        if flags and idx in filtered.index:
            filtered.loc[idx, '_danger'] = ','.join(flags)

    # ── IC 分析: 存储 plan_a 各因子原始分 (回测引擎用 f_ 前缀捕获) ──
    _FACTOR_IC_COLS = {
        'seal': 'f_seal', 'money': 'f_money',
        'sector_mom': 'f_sector', 'tech': 'f_tech',
        'stock_sentiment': 'f_stock_sentiment', 'principal': 'f_principal',
        'north_flow': 'f_north_flow',
        'momentum_consistency': 'f_v2_mc', 'pullback_depth': 'f_v2_pd',
        'alpha': 'f_alpha',
    }
    for fk, col_name in _FACTOR_IC_COLS.items():
        if fk in factors:
            filtered[col_name] = factors[fk].reindex(filtered.index, fill_value=0.0).round(1)
    filtered['f_history'] = history_scores.reindex(filtered.index, fill_value=2.5).round(1)

    # ── v3.3c: score_new 评分 (与生产排行榜一致) ──
    # score_new 使用涨停池自身数据 (封板/时间/换手/连板/炸板/市值/板块),
    # 不依赖外部数据, 回测和生产天然对齐。权重和=100, 直接映射 0-100 分。
    if _BACKTEST_USE_SCORE_NEW:
        try:
            from scoring.score_new import score_new as _score_new_fn
            import sys as _sys
            today_iso = f'{today_fmt[:4]}-{today_fmt[4:6]}-{today_fmt[6:8]}'
            scored_new = _score_new_fn(filtered, today_str=today_iso)
            if scored_new is not None and not scored_new.empty and '新评分' in scored_new.columns:
                # 将 score_new 的评分列和因子分列合并到 filtered
                filtered['新评分'] = scored_new['新评分'].reindex(filtered.index, fill_value=50.0).round(1)
                # score_new 因子分列 (供 IC 分析)
                for col in scored_new.columns:
                    if col.startswith('f_') and col not in filtered.columns:
                        filtered[col] = scored_new[col].reindex(filtered.index, fill_value=0.0).round(1)
                n_scored = len(filtered)
                print(f"  [score_new/回测] {n_scored} 票完成评分, "
                      f"范围 {filtered['新评分'].min():.0f}-{filtered['新评分'].max():.0f}",
                      file=_sys.stderr)
        except Exception as e:
            print(f"  [score_new/回测] 跳过: {e}", file=_sys.stderr)
            # fallback: 用 plan_a 总分
            filtered['新评分'] = filtered['plan_a总分']

    # ── v2 硬过滤 (与生产环境 app.py 一致) ──
    # 对评分后的 stocks 应用换手率/封板时间/行业/连板数等硬规则过滤
    # 使用 score_new 的 新评分 做排序 (如果可用)
    _rank_col = '新评分' if '新评分' in filtered.columns else 'plan_a总分'
    try:
        from config import ENABLE_V2_HARD_FILTER
        if ENABLE_V2_HARD_FILTER and len(filtered) > 0:
            from strategy_filters_v2 import apply_v2_with_fallback
            _stocks_list = []
            _code_col = '代码' if '代码' in filtered.columns else filtered.columns[1]
            _name_col = '名称' if '名称' in filtered.columns else filtered.columns[2]
            for _idx in filtered.index:
                _r = filtered.loc[_idx]
                _stocks_list.append({
                    'code': str(_r.get('代码', '')).strip().zfill(6),
                    'name': str(_r.get('名称', '')),
                    'total_score': float(_r.get(_rank_col, 0)),
                })
            _filtered_stocks, _used_scheme = apply_v2_with_fallback(
                _stocks_list, filtered, top_n=len(_stocks_list), tier_min=1)
            if _filtered_stocks:
                _valid_codes = set(s['code'] for s in _filtered_stocks)
                _before = len(filtered)
                filtered = filtered[filtered[_code_col].astype(str).str.strip().str.zfill(6).isin(_valid_codes)].copy()
                print(f"  [v2 硬过滤/回测] {_used_scheme}: {_before}→{len(filtered)} 票", file=sys.stderr)
            else:
                print(f"  [v2 硬过滤/回测] 三档过滤后 0 票, 保持原池 {len(filtered)} 票", file=sys.stderr)
    except Exception as e:
        print(f"  [v2 硬过滤/回测] 跳过: {e}", file=sys.stderr)

    return filtered  # 含 '新评分' + 'plan_a总分' 列
def _score_zhaban(df: pd.DataFrame, date_str: str, capital=None):
    """炸板评分: score_zhaban_data + 可调权 (P5)

    P8 修复: 尝试加载历史存档的资金流数据，避免评分使用实时数据产生未来偏差。
    """
    if df is None or df.empty:
        return None
    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)
    if df.empty:
        return None
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    cap_col = '流通市值' if '流通市值' in df.columns else None
    if cap_col and cap_col in df.columns:
        df = df[df[cap_col].astype(float) <= MAX_MARKET_CAP * 1e8]
    df = df[df[price_col].astype(float) <= MAX_PRICE]
    if df.empty:
        return None
    try:
        from weight_manager import load_tab_weights
        w = load_tab_weights('zhaban')
    except Exception:
        w = None
    # 尝试加载存档资金流数据（回测时信号日的历史数据）
    fund_df = None
    try:
        from archiver import load_scan_inputs
        archived = load_scan_inputs(date_str)
        if archived is not None:
            fund_df = archived.get('fund_df')
    except Exception:
        pass
    # v3.3d: 传入 date_str 启用 v2 position_factor
    return score_zhaban_data(df, date_str, weights=w, fund_df=fund_df)
def _score_dtqiaoban(df: pd.DataFrame, date_str: str, capital=None):
    """跌停翘板评分: score_dtqiaoban_data + 可调权 (P5)"""
    if df is None or df.empty:
        return None
    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)
    if df.empty:
        return None
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    cap_col = '流通市值' if '流通市值' in df.columns else None
    if cap_col and cap_col in df.columns:
        df = df[df[cap_col].astype(float) <= MAX_MARKET_CAP * 1e8]
    df = df[df[price_col].astype(float) <= MAX_PRICE]
    if df.empty:
        return None
    try:
        from weight_manager import load_tab_weights
        w = load_tab_weights('dtqiaoban')
    except Exception:
        w = None
    # v3.3d: 传入 date_str 启用 v2 position_factor
    return score_dtqiaoban_data(df, weights=w, today_str=date_str)
def _score_reversal(df: pd.DataFrame, date_str: str, capital=None):
    """反转评分: scanner._score_reversal + 可调权 (P5)"""
    try:
        from weight_manager import load_tab_weights
        w = load_tab_weights('reversal')
    except Exception:
        w = None
    # v3.3d: today_str 已传入, v2 position_factor 在 _score_reversal 内部应用
    return scanner_score_reversal(df, today_str=date_str, weights=w)
def _score_trend(df: pd.DataFrame, date_str: str, capital=None):
    """趋势评分: scanner._score_trend + 可调权 (P4)

    df 来自 _fetch_trend_pool (stock_zt_pool_strong_em 当日强势池)
    _score_trend 内部已含板块/价格/市值过滤 + 评分
    """
    if df is None or df.empty:
        return None

    df = df.copy()
    code_col = '代码' if '代码' in df.columns else df.columns[1]
    df = filter_non_main_board(df, code_col=code_col)

    cap_col = '流通市值' if '流通市值' in df.columns else None
    if cap_col and cap_col in df.columns:
        df = df[df[cap_col].astype(float) <= 200 * 1e8]

    price_col = '最新价' if '最新价' in df.columns else (df.columns[4] if len(df.columns) > 4 else None)
    if price_col and price_col in df.columns:
        df = df[df[price_col].astype(float) <= MAX_PRICE]

    change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
    changes = df[change_col].astype(float)
    df = df[(changes >= 2.5) & (changes < 8.5)]

    if df.empty:
        return None

    # P4: 用可调权评分
    try:
        from weight_manager import load_tab_weights
        w = load_tab_weights('trend')
    except Exception:
        w = None
    # v3.3d: 传入 date_str 启用 v2 position_factor
    return scanner_score_trend(df, weights=w, today_str=date_str)
def _score_sector(df: pd.DataFrame, date_str: str, capital=None):
    """板块 tab 回测评分: P2.3 已抽到 scanner._score_sector

    df 参数被忽略 (板块 tab 自己拉数据生成个股级 DF)
    date_str 用于调用 scanner._score_sector(date_str)
    """
    try:
        return scanner_score_sector(date_str, top_n=TOP_N_DEFAULT)
    except Exception as e:
        print(f"  [sector] {date_str} 评分失败: {e}", file=sys.stderr)
        return None
SCORE_FUNCS = {
    TAB_LIMIT_UP: _score_limit_up,
    TAB_REVERSAL: _score_reversal,
    TAB_ZHABAN: _score_zhaban,
    TAB_DTQIAOBAN: _score_dtqiaoban,
    TAB_TREND: _score_trend,
    TAB_SECTOR: _score_sector,
}
SCORE_COLUMNS = {
    TAB_LIMIT_UP: '新评分',    # v3.3c: 统一用 score_new 排名, 与生产排行榜一致
    TAB_REVERSAL: '反转评分',
    TAB_ZHABAN: '总分',         # score_zhaban_data 输出
    TAB_DTQIAOBAN: '翘板评分',
    TAB_TREND: '动量评分',
    TAB_SECTOR: '板块强度',
}
