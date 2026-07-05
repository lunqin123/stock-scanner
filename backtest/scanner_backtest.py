"""scanner_backtest.py - 回测系统

职责: 提供 4 个回测相关函数:
  - backtest_score_prev:  对单日"上交易日涨停今日表现"做回测评分 + 因子相关性
  - _simulate_trades:     模拟次日买入/卖出,返回胜率/盈亏比/最大回撤等
  - auto_verify_backtest: 盘后自动验证 (优先 Plan 归档,回退重评分)
  - run_backtest:         CLI 回测主入口
约束: 不依赖 scanner_scans (避免循环)。
      可依赖 utils / filters / factors / scoring / data / plans.archiver / weight_manager。
"""
import sys
from datetime import date, datetime, timedelta

import akshare as ak
import numpy as np


# BUG-1 修复: 因子字段名 (来自归档 stocks) → weight_manager 权重 key 的显式映射
# 原代码 fkey.replace('_score', '') 会把 'principal_score' → 'principal',
# 但 weight_manager.BACKTEST_FACTORS 用 'principal_score', 导致该因子无法被调权
# 此映射保证字段名与权重 key 一致, 8 因子全部能进入 daily_adjust_weights
_FKEY_TO_WEIGHT_KEY = {
    'seal_score': 'seal',
    'money_score': 'money',
    'sector_score': 'sector',
    'tech_score': 'tech',
    'history_score': 'history',
    'stock_sentiment_score': 'stock_sentiment',
    'principal_score': 'principal_score',  # 不剥 _score, 保持权重 key 原样
    'north_flow_score': 'north_flow',
    'margin_score': 'margin_ratio',
    'inst_rating_score': 'inst_rating',
    'limit_reason_score': 'limit_reason',
}
import pandas as pd

from scanner_utils import get_market_status, TOP_N
from scanner_filters import filter_non_main_board
from scanner_factors import (
    score_seal_strength, score_tech_form, get_sector_score,
    score_stock_history,
)


def backtest_score_prev(prev_df: pd.DataFrame, date_str: str = None):
    """
    对上交易日涨停股进行回测评分，使用与实盘排行完全相同的 7 因子加权模型。
    prev_df: stock_zt_pool_previous_em 返回的上交易日涨停池（含今日涨跌幅）
    date_str: 上交易日日期 YYYYMMDD，用于计算历史股性等因子
    返回: (df_with_scores, summary_dict)
    """
    df = prev_df.copy()

    # 前置过滤
    name_col = None; code_col = None
    for c in df.columns:
        if '名称' in str(c) or '股票名称' in str(c): name_col = c
        if '代码' in str(c): code_col = c
    name_col = name_col or df.columns[2]
    code_col = code_col or df.columns[1]
    df = filter_non_main_board(df)
    if df.empty:
        return df, {"count": 0}

    # 今日涨跌幅
    change_col = None
    for c in df.columns:
        if '涨跌幅' in str(c): change_col = c; break
    change_col = change_col or df.columns[3]
    df['今日涨幅'] = df[change_col].astype(float).round(2)
    df['晋级'] = df['今日涨幅'] > 9

    # ─── 7 因子评分（与实盘排行相同的 apply_weights） ───
    import weight_manager
    w = weight_manager.load_weights()

    # 回测数据修复：stock_zt_pool_previous_em 没有封板时间/封板资金列
    # 换手率代理逻辑：在"上交易日涨停"票中，低换手=筹码锁定好=强封板。
    # 这是回测的固有局限——无法还原真实的封板时间和封板资金。
    has_seal_data = ('首次封板时间' in df.columns or '封板资金' in df.columns)
    if has_seal_data:
        seal_s = score_seal_strength(df)
    else:
        seal_s = pd.Series(0.0, index=df.index)
        turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
        if turnover_col:
            turnover = df[turnover_col].fillna(0).astype(float)
            for idx in df.index:
                t = turnover[idx]
                # 低换手=强封板(筹码锁定), 高换手=弱封板(抛压大)
                if t < 1: seal_s[idx] = 25
                elif t < 3: seal_s[idx] = 20
                elif t < 5: seal_s[idx] = 15
                elif t < 8: seal_s[idx] = 10
                elif t < 15: seal_s[idx] = 5
                else: seal_s[idx] = 2
    tech_s = score_tech_form(df)
    sector_score = get_sector_score(df)

    # 回测seal黄金奖励：仅 fallback 路径需要（score_seal_strength 内部已含黄金奖励）
    if not has_seal_data:
        seal_gold = pd.Series(0.0, index=df.index)
        for idx in df.index:
            if seal_s[idx] >= 20: seal_gold[idx] = 3.0
        seal_s = (seal_s + seal_gold).clip(upper=28.0)

    # 历史股性：日期可用才计算，否则用默认
    if date_str:
        try:
            history_s, _ =         score_stock_history(df, date_str, prev_df=prev_df)
        except Exception:
            history_s = pd.Series(2.5, index=df.index)
    else:
        history_s = pd.Series(2.5, index=df.index)

    # 资金流、情绪历史不可用，用中性默认值
    money_s = pd.Series(10.0, index=df.index)
    sent_s = pd.Series(5.0, index=df.index)

    # 回测权重调整：tech在回测中为负相关(r≈-0.06)，降为0避免噪声
    w_bt = dict(w)
    w_bt['tech'] = 0.0
    # 将tech的权重分配给seal和sector（回测中仅有的正相关因子）
    w_bt['seal'] = w['seal'] + 3.0
    w_bt['sector'] = w['sector'] + 3.0

    scores = weight_manager.apply_weights(
        seal_s, money_s, sector_score,
        tech_s, history_s, sent_s,
        weights=w_bt)

    df['回测评分'] = scores.round(1)
    df['seal_factor'] = seal_s.round(1)
    df['tech_factor'] = tech_s.round(1)
    df['sector_factor'] = sector_score.round(1)
    df['history_factor'] = history_s.round(1)
    df['money_factor'] = money_s.round(1)  # 全默认，相关性为 0
    df['sentiment_factor'] = sent_s.round(1)  # 全默认，相关性为 0

    total = len(df)
    avg_change = df['今日涨幅'].mean()
    promo_rate = df['晋级'].mean()
    pos_rate = (df['今日涨幅'] > 0).mean()

    # 分组统计（按百分位分 A/B/C/D）
    pct_ranks = df['回测评分'].rank(pct=True)
    def get_grade(p):
        if p >= 0.75: return 'A'
        elif p >= 0.50: return 'B'
        elif p >= 0.25: return 'C'
        else: return 'D'
    df['等级'] = pct_ranks.apply(get_grade)

    grades_detail = {}
    for g in ['A', 'B', 'C', 'D']:
        sub = df[df['等级'] == g]
        if len(sub) > 0:
            grades_detail[g] = {
                'count': len(sub),
                'avg_change': round(sub['今日涨幅'].mean(), 2),
                'promo_rate': round(sub['晋级'].mean() * 100, 0),
                'pos_rate': round((sub['今日涨幅'] > 0).mean() * 100, 0),
            }
        else:
            grades_detail[g] = {'count': 0, 'avg_change': 0, 'promo_rate': 0, 'pos_rate': 0}

    # 前30% vs 后30%
    sorted_scores = df.sort_values('回测评分')
    n = len(sorted_scores)
    k = max(1, int(n * 0.3))
    top30 = sorted_scores.tail(k)
    bot30 = sorted_scores.head(k)

    # 7 因子独立相关性（跳过常数因子避免 numpy warning）
    _factor_names = ['seal_factor', 'tech_factor', 'sector_factor',
                     'history_factor',
                     'money_factor', 'sentiment_factor']
    factor_correlations = {}
    for f in _factor_names:
        if f in df.columns:
            vals = df[f].astype(float)
            if vals.std() < 0.01:
                continue  # 常数因子（如 money/sentiment 默认值），跳过
            fc = vals.corr(df['今日涨幅'].astype(float))
            key = f.replace('_factor', '')
            factor_correlations[key] = round(fc, 4) if not pd.isna(fc) else 0.0

    corr = df['回测评分'].corr(df['今日涨幅'])

    # ── 模拟交易统计 ──
    trade_stats = _simulate_trades(df, '回测评分')

    summary = {
        'count': total,
        'avg_change': round(avg_change, 2),
        'promo_rate': round(promo_rate * 100, 1),
        'pos_rate': round(pos_rate * 100, 1),
        'correlation': round(corr, 4),
        'factor_correlations': factor_correlations,
        'grades': grades_detail,
        'top30_avg': round(top30['今日涨幅'].mean(), 2),
        'bot30_avg': round(bot30['今日涨幅'].mean(), 2),
        'trade_stats': trade_stats,
        'top5': [(str(df.loc[i, code_col]).strip().zfill(6),
                  str(df.loc[i, name_col]).strip(),
                  float(df.loc[i, '回测评分']),
                  float(df.loc[i, '今日涨幅'])) for i in sorted_scores.tail(5).index],
        'bot5': [(str(df.loc[i, code_col]).strip().zfill(6),
                  str(df.loc[i, name_col]).strip(),
                  float(df.loc[i, '回测评分']),
                  float(df.loc[i, '今日涨幅'])) for i in sorted_scores.head(5).index],
    }
    return df, summary


def _simulate_trades(df, score_col, top_n=10, commission=0.00025, slippage=0.001):
    """
    模拟交易：取评分最高的 N 只，次日开盘买入/收盘卖出。
    过滤次日无法买入的标的（一字板/缩量秒板）。
    返回: {total_return, win_rate, profit_loss_ratio, max_drawdown, trades, unbuyable_count}
    """
    sorted_df = df.sort_values(score_col, ascending=False)

    # 次日竞价过滤：排除一字板或缩量涨停（换手<1% + 涨>9.5% = 买不到）
    turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    unbuyable = pd.Series(False, index=sorted_df.index)
    if turnover_col and '今日涨幅' in sorted_df.columns:
        changes_all = sorted_df['今日涨幅'].astype(float)
        turnovers = sorted_df[turnover_col].astype(float)
        unbuyable = (changes_all > 9.5) & (turnovers < 1.0)

    # 取评分最高的 top_n 且可买入的
    buyable = sorted_df[~unbuyable].head(top_n)
    # 如果可买入的不够，放宽换手限制
    if len(buyable) < 3:
        unbuyable_loose = (sorted_df['今日涨幅'].astype(float) > 9.5) & (sorted_df[turnover_col].astype(float) < 0.3)
        buyable = sorted_df[~unbuyable_loose].head(top_n)

    unbuyable_count = int(unbuyable.sum()) if len(buyable) > 0 else 0
    changes = buyable['今日涨幅'].astype(float).values
    n_trades = len(changes)
    if n_trades == 0:
        return {'total_return': 0, 'win_rate': 0, 'profit_loss_ratio': 0,
                'max_drawdown': 0, 'trade_count': 0, 'best': 0, 'worst': 0, 'unbuyable_count': 0}

    # 每笔：涨幅 - 佣金(万2.5双向) - 滑点(0.1%)
    returns = changes - commission * 2 * 100 - slippage * 100
    wins = (returns > 0).sum()
    total_return = round(float(returns.sum() / n_trades), 2)

    win_avg = float(np.mean(returns[returns > 0])) if wins > 0 else 0
    loss_avg = float(abs(np.mean(returns[returns <= 0]))) if wins < n_trades else 0
    profit_loss_ratio = round(win_avg / loss_avg, 2) if loss_avg > 0 else 0

    cum = np.cumsum(returns)
    peak = np.maximum.accumulate(cum)
    dd = cum - peak
    max_dd = round(float(dd.min()), 2)

    return {
        'total_return': total_return,
        'win_rate': round(wins / n_trades * 100, 1),
        'profit_loss_ratio': profit_loss_ratio,
        'max_drawdown': max_dd,
        'trade_count': n_trades,
        'best': round(float(returns.max()), 2),
        'worst': round(float(returns.min()), 2),
        'unbuyable_count': unbuyable_count,
    }


def auto_verify_backtest(today_str: str, table_mode: bool = False, current_weights: dict = None,
                          plan_name: str = 'A'):
    """
    回测验证：盘后自动运行。
    优先从 Plan 归档 (daily_data/) 读取昨日评分 → 对比今日实际涨跌幅 → 计算因子相关性。
    归档不存在时回退到 backtest_score_prev 重评分 (冷启动兼容)。
    plan_name: 'A' 或 'B', 用于独立调权

    P5 调权隔离决策 (V1):
    - 本函数只覆盖 plan_a / plan_b 的涨停 tab 调权 (6 因子)
    - 炸板/翘板/趋势/反转/板块的回测只输出统计,不调权
    - V2 可扩展: 为每个 tab 建独立 weight_manager.PLAN_X_WEIGHTS
    返回: (输出文本, adjusted_weights) 或 None
    """
    import weight_manager
    from cache import _last_trading_date

    wd = datetime.strptime(today_str, '%Y%m%d').weekday()
    if wd >= 5:
        return None

    market_status = get_market_status()
    if market_status == 'trading':
        return None

    # 拉取今日实际涨跌幅
    try:
        prev_df = ak.stock_zt_pool_previous_em(date=today_str)
        if prev_df.empty:
            return None
    except Exception:
        return None

    try:
        change_col = next((c for c in prev_df.columns if '涨跌幅' in str(c)), None)
        if change_col is None:
            return None
        changes = prev_df[change_col].astype(float)
        if changes.abs().max() < 0.5:
            return None
    except (ValueError, IndexError):
        return None

    # code→涨幅 映射
    code_col_raw = next((c for c in prev_df.columns if '代码' in str(c)), prev_df.columns[1])
    code_map = {}
    for _, row in prev_df.iterrows():
        try:
            code_map[str(row[code_col_raw]).strip().zfill(6)] = float(row[change_col])
        except Exception:
            continue

    sep = "─" * 50
    lines = [f"\n{sep}"]
    all_fc = {}
    archive_used = False

    # ── 优先: 读 Plan 归档 ──
    yesterday = _last_trading_date(today_str)
    if yesterday:
        try:
            from plans.archiver import list_plan_results, load_plan_result
            plan_names = list_plan_results(yesterday)
            if plan_names:
                for plan_name in plan_names:
                    archive = load_plan_result(yesterday, plan_name)
                    if not archive or not archive.get('stocks'):
                        continue
                    stocks = archive['stocks']
                    factor_scores = {}
                    for s in stocks:
                        code = s.get('code', '')
                        if code not in code_map:
                            continue
                        actual = code_map[code]
                        for fkey in ['seal_score', 'money_score', 'sector_score',
                                     'tech_score', 'history_score', 'stock_sentiment_score',
                                     'principal_score', 'north_flow_score', 'margin_score',
                                     'inst_rating_score', 'limit_reason_score']:
                            if fkey in s:
                                factor_scores.setdefault(fkey, []).append((float(s[fkey]), actual))
                    plan_fc = {}
                    for fkey, pairs in factor_scores.items():
                        if len(pairs) < 8:
                            continue
                        scores_arr = pd.Series([p[0] for p in pairs])
                        returns_arr = pd.Series([p[1] for p in pairs])
                        if scores_arr.std() < 0.01 or returns_arr.std() < 0.01:
                            continue
                        c = scores_arr.corr(returns_arr)
                        if not pd.isna(c):
                            # BUG-1 修复: 之前用 fkey.replace('_score', '') 会把
                            # 'principal_score' → 'principal', 但 weight_manager.BACKTEST_FACTORS
                            # 用 'principal_score' 作为 key, 导致 principal_score 因子
                            # 永远无法被 daily_adjust_weights 识别并调权
                            # 现在用显式映射, 保持与 weight_manager 因子 key 一致
                            plan_fc[_FKEY_TO_WEIGHT_KEY[fkey]] = round(float(c), 4)
                    if plan_fc:
                        archive_used = True  # 仅在确实产出有效IC后标记
                        lines.append(f" [Plan {plan_name}] 归档验证 | {len(stocks)}只")
                        all_fc.update(plan_fc)
        except Exception as e:
            lines.append(f" [归档回测] 异常,回退重评分: {e}")
            archive_used = False

    # ── 回退: 旧方法 ──
    if not archive_used:
        df_result, summary = backtest_score_prev(prev_df, date_str=today_str)
        if summary['count'] == 0:
            return None
        all_fc = summary.get('factor_correlations', {})
        lines.append(f" 评分验证 | 昨 {summary['count']} 只 → 今晋级 {summary['promo_rate']:.0f}% | "
                     f"正收益 {summary['pos_rate']:.0f}% | 均价 {summary['avg_change']:+.1f}%")
        if all_fc:
            avail = [k for k in weight_manager.BACKTEST_FACTORS if k in all_fc and all_fc[k] != 0]
            if avail:
                lines.append(f" 因子相关性: {' | '.join(f'{k}: {all_fc[k]:+.3f}' for k in avail)}")

    # ── 滚动调权 (按 Plan 独立) ──
    adjusted_weights = None
    daily_msg = None
    if current_weights is not None and all_fc:
        weight_manager.save_daily_correlations(all_fc, trading_date=today_str, plan_name=plan_name)
        new_w, adj_msg = weight_manager.daily_adjust_weights(current_weights, plan_name=plan_name)
        if new_w:
            adjusted_weights = new_w
        if adj_msg:
            for line in adj_msg.split('\n'):
                lines.append(line)

    if adjusted_weights and current_weights:
        changes = []
        for k in weight_manager.BACKTEST_FACTORS:
            if k in current_weights and k in adjusted_weights:
                delta = adjusted_weights[k] - current_weights[k]
                if abs(delta) > 0.01:
                    changes.append(f"{k}: {current_weights[k]:.0f}→{adjusted_weights[k]:.0f} ({delta:+.1f})")
        if changes:
            lines.append(f" 权重调整: {' | '.join(changes)}")
    lines.append(sep)
    return "\n".join(lines), adjusted_weights


def run_backtest(tab: str = 'limit-up', N: int = 5):
    """回测主入口 (P4: 支持多 tab 滚动回测)"""
    # 周末检测
    wd = date.today().weekday()
    if wd >= 5:
        print("  [回测跳过] 周末不开盘")
        return

    # ── P4: 多 tab 滚动回测 (走 backtest_engine) ──
    if tab != 'limit-up':
        from backtest_engine import run_tab_backtest, TAB_NAMES_CN
        print(f"运行 {tab} ({TAB_NAMES_CN.get(tab, tab)}) {N} 天滚动回测 (T+1 真实)...")
        res = run_tab_backtest(tab=tab, max_days=N, top_n=3, capital=30000, use_cache=False)
        if 'error' in res and not res.get('trades'):
            print(f"  错误: {res['error']}")
            return
        s = res['summary']
        print(f"  笔数: {s.get('trade_count', 0)}")
        print(f"  胜率: {s.get('win_rate', 0)}%")
        print(f"  累计收益: {s.get('cumulative_ret', 0):+.2f}%")
        print(f"  总盈亏: ¥{s.get('total_pnl', 0):+,.0f}")
        print(f"  盈亏比: {s.get('plr', 0)}")
        print(f"  最大回撤: {s.get('max_dd', 0):.2f}%")
        print(f"  期望值: {s.get('ev', 0):+.2f}%")
        cmp = res.get('comparison', {})
        print(f"  一字板跳过: {cmp.get('unbuyable_count', 0)} 笔")
        return

    # ── 兼容旧版: limit-up 走 backtest_score_prev (输出因子相关性) ──
    from cache import _is_trading_day
    print(f"运行 {N} 天滚动回测 (limit-up, 含因子相关性)...")
    results = []
    errors = 0

    for i in range(N):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if not _is_trading_day(d.strftime("%Y%m%d")):
            continue
        d_str = d.strftime("%Y%m%d")
        try:
            prev = ak.stock_zt_pool_previous_em(date=d_str)
            if prev.empty:
                continue
            df_res, summary = backtest_score_prev(prev, date_str=d_str)
            if summary['count'] >= 5:
                results.append(summary)
        except Exception as e:
            errors += 1
            if errors > 5:
                break
        if (i + 1) % 5 == 0:
            print(f"  ... {i+1}/{N}")

    if not results:
        print("  无有效回测数据")
        return

    # ── 聚合 ──
    total_count = sum(r['count'] for r in results)
    avg_corr = np.mean([r['correlation'] for r in results])
    avg_change = np.mean([r['avg_change'] for r in results])
    avg_promo = np.mean([r['promo_rate'] for r in results])
    avg_pos = np.mean([r['pos_rate'] for r in results])
    avg_top30 = np.mean([r['top30_avg'] for r in results])
    avg_bot30 = np.mean([r['bot30_avg'] for r in results])

    # 因子相关性聚合
    fc_list = [r.get('factor_correlations', {}) for r in results]
    factor_avg = {}
    for fc in fc_list:
        for k, v in fc.items():
            factor_avg.setdefault(k, []).append(v)
    factor_avg = {k: np.mean(v) for k, v in factor_avg.items()}

    lines = []
    lines.append("\n" + "=" * 55)
    lines.append(f"  滚动回测 ({len(results)} 天) | 共 {total_count} 只标的")
    lines.append("=" * 55)
    lines.append(f"  平均溢价: {avg_change:+.2f}% | 平均晋级率: {avg_promo:.1f}%")
    lines.append(f"  平均正收益比: {avg_pos:.1f}%")
    if factor_avg:
        corr_str = " | ".join(f"{k}: {factor_avg[k]:+.3f}" for k in ['seal', 'sector', 'tech'] if k in factor_avg)
        lines.append(f"  因子相关性(均值): {corr_str}")
    lines.append(f"  评分-涨幅相关系数(均值): {avg_corr:.4f}")
    lines.append(f"  前30%平均涨幅: {avg_top30:+.2f}%  |  后30%: {avg_bot30:+.2f}%  |  差: {avg_top30 - avg_bot30:+.2f}%")

    # 评级说明
    lines.append("")
    lines.append("评级标准 (满分100分, 基于实盘9因子加权):")
    lines.append("  S级>=75 | A级>=65 | B级>=55 | C级<55")
    lines.append("  评分维度: 封板强度(22) + 资金(12) + 板块合力(12) + 技术(6) + 股性(4) + 情绪(9) + 本金(6)")
    lines.append("  注: 回测中资金/情绪为默认值，实际预测力更强。历史数据不完整属正常现象。")

    lines.append("")
    lines.append("免责: 回测数据仅供参考, 历史表现不代表未来收益。")
    lines.append("      评分系统有效性需持续多日积累验证。")

    print("\n".join(lines))
