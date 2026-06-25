#!/usr/bin/env python3
"""
超短线选股扫描器 (CLI 入口)
策略: 首板涨停 + 多因子综合评分
本金: 1万元
数据源: akshare (同花顺 + 东方财富)
依赖: akshare>=1.14.0, pandas, numpy

代码结构 (2026-06-21 refactor):
  scanner_utils.py     # 纯工具 (时间/格式化/缓存)
  scanner_filters.py   # 数据过滤
  scanner_data.py      # 数据获取 (涨停池/资金流)
  scanner_factors.py   # 评分因子 (15+ 个 score_* 函数)
  scanner_scoring.py   # 5 模式专用纯评分
  scanner_scans.py     # 5 模式扫描主流程
  scanner_backtest.py  # 回测系统
  scanner_format.py    # 文本输出格式化
  scanner.py           # CLI 入口 + 公共 API re-export

向后兼容: 所有 from scanner import xxx 调用维持原签名 (本文件 re-export)。
"""

import sys
import io
import os
import argparse
from datetime import date, datetime

# ─── 编码修复: 强制 stdout/stderr 使用 UTF-8，解决 PowerShell GBK 乱码 ───
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
elif sys.stdout.encoding and sys.stdout.encoding.upper() not in ('UTF-8', 'UTF8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    if sys.stderr.encoding and sys.stderr.encoding.upper() not in ('UTF-8', 'UTF8'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import pandas as pd

# ─── 配置常量 (原 scanner.py 顶部 from config import) ───
from config import (
    _CST, MAX_PRICE, MAX_MARKET_CAP, MAX_LATE_SEAL, TOP_N as _TOP_N_DEFAULT,
)

# ──────────────────────────────────────────────────────────────────
#  Re-export: 保持 `from scanner import xxx` 向后兼容
# ──────────────────────────────────────────────────────────────────

# 常量
TOP_N = _TOP_N_DEFAULT  # CLI --top 可改写此全局

# utils
from scanner_utils import (
    money_str, seal_time_score, get_today_str, get_market_status, get_default_mode,
    _vectorized_seal_time_score, _cache_get, _cache_put, _fund_flow_ttl,
)

# filters
from scanner_filters import (
    NON_MAIN_BOARD_PREFIXES, _get_non_main_prefixes, XR_XD_DR_PREFIXES,
    filter_non_main_board, filter_xr_xd_dr, pre_filter, filter_by_price,
)

# data
from scanner_data import fetch_limit_up_pool, fetch_fund_flow_data

# factors (15+ 评分函数 + 情绪/龙虎榜/历史股性)
from scanner_factors import (
    score_seal_strength, get_money_flow_scores,
    get_sector_score, get_sector_heat_scores, get_sector_resonance,
    score_tech_form, score_stock_sentiment, score_danger_signals,
    _dynamic_positions, score_by_principal,
    can_buy_filter, score_buyability,
    detect_market_sentiment, analyze_dragon_tiger, score_stock_history,
)

# scoring (5 模式专用评分)
from scanner_scoring import (
    score_zhaban_data, _score_reversal, _score_trend, _calc_ma_regression,
    score_sector_data, _get_sector_stocks, _score_sector, score_dtqiaoban_data,
)

# scans (5 模式扫描主流程)
from scanner_scans import (
    scan_zhaban, scan_reversal, scan_trend, scan_sector, scan_dtqiaoban,
)

# backtest
from scanner_backtest import (
    backtest_score_prev, _simulate_trades, auto_verify_backtest, run_backtest,
)

# format
from scanner_format import format_table_output, format_output


# ──────────────────────────────────────────────────────────────────
#  CLI 入口
# ──────────────────────────────────────────────────────────────────

def main():
    global TOP_N
    parser = argparse.ArgumentParser(description='超短线选股扫描器')
    parser.add_argument('--table', action='store_true', help='以表格格式输出（默认详细文本）')
    parser.add_argument('--backtest', action='store_true', help='运行回测系统（验证评分有效性）')
    parser.add_argument('--tab', type=str, default='limit-up',
                        choices=['limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal', 'sector'],
                        help='回测 tab (与 --backtest 一起用,默认 limit-up)')
    parser.add_argument('--days', type=int, default=5, help='回测天数 (默认 5,akshare 实际可用窗口限制)')
    parser.add_argument('--zhaban', action='store_true', help='炸板股反包潜力扫描')
    parser.add_argument('--trend', action='store_true', help='趋势动量股扫描')
    parser.add_argument('--sector', action='store_true', help='今日板块概览(信息面板)')
    parser.add_argument('--dtqiaoban', action='store_true', help='跌停翘板信号扫描')
    parser.add_argument('--reversal', action='store_true', help='涨停回调反转扫描(上交易日涨停今回调→明日反包)')
    parser.add_argument('--date', type=str, default='', help='指定日期 YYYYMMDD（默认: 今天）')
    parser.add_argument('--top', type=int, default=0, help=f'输出数量（默认: {TOP_N}）')
    args = parser.parse_args()

    # 日期处理
    today_raw = args.date if args.date else date.today().strftime("%Y%m%d")
    if args.top > 0:
        TOP_N = args.top

    if args.backtest:
        run_backtest(tab=args.tab, N=args.days)
        return
    table_mode = args.table

    # ── 模式分发 + 盘中/盘后自动检测 ──

    if args.zhaban:
        scan_zhaban(today_raw, table_mode, top_n=TOP_N)
        return
    if args.trend:
        scan_trend(today_raw, table_mode, top_n=TOP_N)
        return
    if args.sector:
        scan_sector(today_raw, table_mode, top_n=TOP_N)
        return
    if args.reversal:
        scan_reversal(today_raw, table_mode, top_n=TOP_N)
        return

    if args.dtqiaoban:
        scan_dtqiaoban(today_raw, table_mode, top_n=TOP_N)
        return

    # 无显式模式 → 自动检测盘中/盘后
    default_mode = get_default_mode()
    market_status = get_market_status()

    if default_mode == 'trend':
        # 盘中自动 → 趋势动量扫描（能买进）
        now = datetime.now(_CST)
        print(f"[自动检测] 当前盘中 ({now.strftime('%H:%M')}) → 默认趋势动量扫描", file=sys.stderr)
        scan_trend(today_raw, table_mode, top_n=TOP_N)
        return

    # 盘后 → 涨停多因子评分（次日预测）
    status_labels = {'closed': '盘后', 'lunch': '午休', 'weekend': '周末', 'holiday': '节假日', 'trading': '盘中'}
    status_cn = status_labels.get(market_status, market_status)
    print(f"[自动检测] 当前{status_cn} → 默认涨停多因子评分", file=sys.stderr)

    print("=" * 45, file=sys.stderr)
    today_display = today_raw[:4] + '-' + today_raw[4:6] + '-' + today_raw[6:8] if len(today_raw) == 8 else today_raw
    print(f"  超短线选股扫描器 | {today_display}", file=sys.stderr)
    print("=" * 45, file=sys.stderr)

    # 1. 获取涨停池（含非交易日检测）
    pool = fetch_limit_up_pool(date_str=today_raw)
    if pool.empty:
        print("not_trading_day")
        return

    # 2. 前置过滤 (不含价格，价格需要同花顺数据)
    filtered = pre_filter(pool)
    if filtered.empty:
        print(f"X 无符合条件的标的", file=sys.stderr)
        print("no_data")
        return

    # 3. 获取同花顺全市场数据 (一次调用，同时用于资金流评分 + 股价过滤)
    fund_df, fund_err = fetch_fund_flow_data()
    if fund_df is None:
        # 资金流不可用，降级输出
        print("  ! 同花顺数据不可用，降级为仅涨停强度+板块+量价评分", file=sys.stderr)
        money_scores = pd.Series(0.0, index=filtered.index)
        sector_raw = get_sector_heat_scores(filtered)
        tech_scores = score_tech_form(filtered)
        buyability_scores = score_buyability(filtered)
        sector_res_scores = get_sector_resonance(filtered)
        fmt = format_table_output if table_mode else format_output
        output = fmt(filtered, money_scores, sector_raw, score_seal_strength(filtered), tech_scores,
                      buyability_scores=buyability_scores,
                      sector_res_scores=sector_res_scores)
        print(output)
        return

    # 股价过滤
    filtered = filter_by_price(filtered, fund_df)
    if filtered.empty:
        print(f"X 无符合条件标的 (全部超过{MAX_PRICE}元)", file=sys.stderr)
        print("no_data")
        return

    print("[3/5] 评分: 涨停强度 + 资金面 + 板块热度 + 量价关系...", file=sys.stderr)
    seal_scores = score_seal_strength(filtered)
    money_scores, raw_money = get_money_flow_scores(filtered, fund_df=fund_df)
    sector_raw = get_sector_heat_scores(filtered, money_series=raw_money)
    tech_scores = score_tech_form(filtered)
    buyability_scores = score_buyability(filtered)
    sector_res_scores = get_sector_resonance(filtered)

    print("[4/5] 预测: 市场情绪 + 龙虎榜 + 历史股性 + 舆情...", file=sys.stderr)
    today_raw = date.today().strftime("%Y%m%d")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(detect_market_sentiment, today_raw): "sentiment",
            ex.submit(analyze_dragon_tiger, filtered, today_raw): "lhb",
            ex.submit(score_stock_history, filtered, today_raw): "history",
            ex.submit(lambda: __import__('stock_community').score_community(filtered)): "community",
        }
        res = {}
        for f in as_completed(futs):
            key = futs[f]
            try:
                res[key] = f.result()
            except Exception as e:
                print(f"  ! {key} 评分失败: {e}", file=sys.stderr)

    if res.get("sentiment"):
        sentiment_score, sentiment_level, sentiment_detail = res["sentiment"]
    else:
        sentiment_score, sentiment_level, sentiment_detail = 5.0, "未知", {}
    if res.get("lhb"):
        lhb_bonus, _ = res["lhb"]
    else:
        lhb_bonus, _ = pd.Series(0.0, index=filtered.index), {}
    if res.get("history"):
        history_scores, _ = res["history"]
    else:
        history_scores = pd.Series(2.5, index=filtered.index)
    if res.get("community"):
        community_scores = res["community"]
    else:
        community_scores = None

    # 龙虎榜加分并入资金质量（仅加分不扣分，满分20）
    money_scores = (money_scores + lhb_bonus).clip(upper=20.0)

    print("[5/5] 生成评分报告...", file=sys.stderr)
    import weight_manager
    weights = weight_manager.load_weights()
    fmt = format_table_output if table_mode else format_output
    output = fmt(filtered, money_scores, sector_raw, seal_scores, tech_scores,
                  raw_money=raw_money,
                  sentiment_score=sentiment_score,
                  sentiment_level=sentiment_level,
                  sentiment_detail=sentiment_detail,
                  history_scores=history_scores,
                  buyability_scores=buyability_scores,
                  sector_res_scores=sector_res_scores,
                  weights=weights)
    print(output)

    # 自动回测验证 + 权重调整
    verify_result = auto_verify_backtest(today_raw, table_mode, current_weights=weights)
    if verify_result:
        verify_output, new_weights = verify_result
        print(verify_output)
        if new_weights:
            weights = new_weights

    # ── 预计算总分 + TOP N 索引（供后续所有模块共享） ──
    s_history = history_scores if history_scores is not None else pd.Series(2.5, index=filtered.index)
    s_stock_sent = score_stock_sentiment(filtered, money_scores, buyability_scores)
    s_principal = score_by_principal(filtered, 20000)
    sector_merged_cli = (sector_res_scores + sector_raw) / 2.0

    s_north_flow = pd.Series(5.0, index=filtered.index)
    base_totals = weight_manager.apply_weights(seal_scores, money_scores, sector_merged_cli, tech_scores, s_history, sentiment_score=pd.Series(float(sentiment_score), index=filtered.index), stock_sentiment_scores=s_stock_sent, principal_scores=s_principal, weights=weights)
    total_scores = base_totals
    top_indices = list(total_scores.sort_values(ascending=False).head(TOP_N).index)
    filtered_top = filtered.loc[top_indices]

    # ── 社区舆情聚合（已按评分排序） ──
    try:
        import stock_community
        community_output, sentiment_data = stock_community.run(filtered_top, TOP_N)
        if community_output:
            print(community_output)
    except Exception as e:
        print(f"  [scanner.py] community failed: {e}", file=sys.stderr)

    # ── 增强指标分析 ──
    try:
        import stock_indicators
        enhanced_output, enhanced_data = stock_indicators.run_enhanced(
            filtered_top, today_raw, TOP_N,
            total_scores=total_scores
        )
        if enhanced_output:
            print(enhanced_output)
    except Exception as e:
        print(f"  [scanner.py] indicators failed: {e}", file=sys.stderr)

    # ── 数据持久化 + 每日总结 ──
    try:
        import stock_data_manager
        today_data = {
            'date': date.today().isoformat(),
            'sentiment_level': sentiment_level,
            'sentiment_score': sentiment_score,
            'stocks': []
        }
        for idx in top_indices:
            row = filtered.loc[idx]
            today_data['stocks'].append({
                'code': str(row.get('代码', '')).strip().zfill(6),
                'name': row.get('名称', ''),
                'total_score': round(float(total_scores.get(idx, 0)), 1),
                'seal_score': round(float(seal_scores.get(idx, 0)), 1),
                'money_score': round(float(money_scores.get(idx, 0)), 1),
                'sector_score': round(float(sector_raw.get(idx, 0)), 1),
                'tech_score': round(float(tech_scores.get(idx, 0)), 1),
                'community_score': round(float(community_scores.get(idx, 0)), 1) if community_scores is not None else 3.5,
                'industry': row.get('所属行业', ''),
                'turnover': float(row.get('换手率', 0)) if pd.notna(row.get('换手率')) else None,
                'seal_time': str(row.get('首次封板时间', '')),
            })
        dm_summary, dm_paths = stock_data_manager.run(today_data)
        print(dm_summary)
        paths_info = f"  数据: {os.path.basename(dm_paths['data_path'])} | 总结: daily_summary.md"
        print(paths_info)
    except Exception as e:
        print(f"  [数据持久化跳过] {e}", file=sys.stderr)

    # ── 各维度短榜 ──
    zhaban_res = scan_zhaban(today_raw, top_n=3)
    trend_res = scan_trend(today_raw, top_n=3)
    scan_sector(today_raw, top_n=3)
    dtqiaoban_res = scan_dtqiaoban(today_raw, top_n=3)

    # ── 情绪自适应策略建议 ──
    zhaban_rate = (sentiment_detail or {}).get('zhaban_rate', 0.5)

    if zhaban_rate < 0.30:
        emotion_zone = "活跃"
        strategy_advice = "主力策略：涨停超短线，可积极打首板/连板"
    elif zhaban_rate < 0.40:
        emotion_zone = "正常"
        strategy_advice = "涨停超短线为主，炸板反包辅助，控制仓位"
    elif zhaban_rate < 0.50:
        emotion_zone = "偏弱"
        strategy_advice = "提高选股标准，涨停+炸板反包双策略并重"
    else:
        emotion_zone = "低迷"
        strategy_advice = "建议炸板反包+趋势动量为主，涨停缩小仓位"

    # ── 全维度综合榜单 ──
    all_dimension_stocks = []
    # 主榜单 TOP10（用 base_totals 不含情绪系数，避免系统性偏差）
    for idx in top_indices:
        row = filtered.loc[idx]
        code = str(row.get('代码', '')).strip().zfill(6)
        name = row.get('名称', '')
        score = round(float(base_totals.get(idx, 0)), 1)
        all_dimension_stocks.append({'code': code, 'name': name, 'score': score, 'dimension': '涨停超短线'})
    # 炸板反包
    if zhaban_res:
        all_dimension_stocks.extend(zhaban_res)
    # 趋势动量
    if trend_res:
        all_dimension_stocks.extend(trend_res)
    # 跌停翘板
    if dtqiaoban_res:
        all_dimension_stocks.extend(dtqiaoban_res)

    # 去重（同只股票保留最高分 + 同分保留先出现的维度）
    seen = {}
    for item in all_dimension_stocks:
        key = item['code']
        if key not in seen or item['score'] > seen[key]['score']:
            seen[key] = item
    combined = sorted(seen.values(), key=lambda x: x['score'], reverse=True)

    print("\n" + "=" * 60)
    print("  市场情绪: {}  |  炸板率: {:.0f}%  |  {}".format(emotion_zone, zhaban_rate * 100, strategy_advice))
    print("=" * 60)
    print("  全维度综合榜单 | 涨停·炸板·趋势·翘板  TOP{}".format(min(len(combined), 10)))
    print("=" * 60)
    print("  {:<3s} {:<7s} {:<8s} {:>5s}  {}".format("#", "代码", "名称", "分", "维度"))
    print("  " + "-" * 50)
    for rank, item in enumerate(combined[:10], 1):
        print("  {:<3d} {:<7s} {:<8s} {:>5.0f}  {}".format(
            rank, item['code'], item['name'], item['score'], item['dimension']))
    print("=" * 60)
    print("  注：原始分，维度间评分体系不同，不可跨维度比较")
    print("  各维度原始分每日可纵向对比，回测不受影响")
    print("=" * 60)


if __name__ == "__main__":
    main()
