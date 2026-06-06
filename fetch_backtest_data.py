#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取过去30个交易日的涨停数据及次日表现，保存为回测CSV。
用法: python fetch_backtest_data.py
"""
import sys
import os
from datetime import datetime, timedelta
import time

import pandas as pd
import numpy as np
import akshare as ak

# ─── 导入 scanner 中的评分函数 ───
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scanner import (
    score_seal_strength,
    score_tech_form,
    get_sector_heat_scores,
    get_sector_resonance,
    filter_non_main_board,
)
from cache import _load_trading_calendar

OUTPUT = r'C:\Users\16689\Desktop\stock-scanner\backtest_30days.csv'
REQUIRED_DAYS = 30

# API 调用间隔（秒）
API_SLEEP = 0.8


def get_last_n_trading_days_fast(n: int) -> list[str]:
    """
    使用 cache 模块的交易日历快速获取最近 N 个交易日。
    返回 YYYYMMDD 格式字符串列表（从近到远）。
    """
    calendar = _load_trading_calendar()
    today = datetime.now()
    candidates = []
    d = today
    # 往回找足够的天数
    for _ in range(365):
        if len(candidates) >= n:
            break
        ds = d.strftime("%Y%m%d")
        if ds in calendar:
            candidates.append(ds)
        d -= timedelta(days=1)
    return candidates[:n]


def _col_index(df, names, fallback_idx=None):
    """按名称列表查找列索引，找不到时返回 fallback_idx。"""
    for name in names:
        if name in df.columns:
            return name
    if fallback_idx is not None and len(df.columns) > fallback_idx:
        return df.columns[fallback_idx]
    return None


def main():
    print("=" * 60, file=sys.stderr)
    print("  30日涨停数据回测采集", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # ── 1. 获取交易日 ──
    trading_days = get_last_n_trading_days_fast(REQUIRED_DAYS)
    print(f"交易日历加载完成，获取最近 {len(trading_days)} 个交易日", file=sys.stderr)

    # 按时间升序（从远到近）以便查找下一个交易日
    days_asc = list(reversed(trading_days))
    day_to_next = {}
    for i, d in enumerate(days_asc):
        if i + 1 < len(days_asc):
            day_to_next[d] = days_asc[i + 1]

    all_records: list[dict] = []
    failed_days: list[str] = []
    processed_days = 0

    # ── 2. 逐日处理 ──
    for idx, day in enumerate(trading_days):
        next_day = day_to_next.get(day)
        if next_day is None:
            print(f"  [{idx+1}/{len(trading_days)}] {day} → 无后续交易日数据，跳过", file=sys.stderr)
            continue

        print(f"\n  [{idx+1}/{len(trading_days)}] {day} → 次日 {next_day}", file=sys.stderr)

        try:
            # ── 2a. 获取当日涨停池 ──
            print(f"    fetching 涨停池...", file=sys.stderr)
            time.sleep(API_SLEEP)
            pool = ak.stock_zt_pool_em(date=day)

            if pool is None or pool.empty:
                print(f"    → 当日无涨停数据（非交易日或休市）", file=sys.stderr)
                continue

            total_raw = len(pool)
            print(f"    → 涨停 {total_raw} 只", file=sys.stderr)

            # ── 2b. 非主板过滤（ST / 科创板 / 北交所 / 创业板） ──
            pool = filter_non_main_board(pool)
            if pool.empty:
                print(f"    → 过滤后无数据（均被排除）", file=sys.stderr)
                continue
            print(f"    → 过滤后 {len(pool)} 只（排除 {total_raw - len(pool)} 只非主板）", file=sys.stderr)

            # ── 2c. 计算评分因子 ──
            seal_scores = score_seal_strength(pool)
            tech_scores = score_tech_form(pool)
            # sector 评分需要资金流，但回测历史数据没有实时资金流，传 None
            sector_mom = get_sector_heat_scores(pool, money_series=None)
            sector_res = get_sector_resonance(pool)

            # ── 2d. 获取次日涨跌幅数据 ──
            print(f"    fetching 次日涨跌幅...", file=sys.stderr)
            time.sleep(API_SLEEP)
            prev_pool = ak.stock_zt_pool_previous_em(date=next_day)

            # 构建次日涨跌幅查询表 {code: change_percent}
            next_change_map: dict[str, float] = {}
            if prev_pool is not None and not prev_pool.empty:
                # stock_zt_pool_previous_em 列结构:
                # [0]=序号, [1]=代码, [2]=名称, [3]=涨跌幅, ...
                code_col = prev_pool.columns[1]
                change_col = prev_pool.columns[3]
                for _, r in prev_pool.iterrows():
                    c = str(r[code_col]).strip().zfill(6)
                    try:
                        chg = float(r[change_col])
                        next_change_map[c] = chg
                    except (ValueError, TypeError) as e:
                        print(f"  [fetch_backtest L137] failed: {e}", file=sys.stderr)
            print(f"    → 获取到 {len(next_change_map)} 只次日涨跌幅数据", file=sys.stderr)

            # ── 2e. 组装记录 ──
            code_col_pool = _col_index(pool, ['代码'])
            name_col_pool = _col_index(pool, ['名称', '股票名称'])

            if code_col_pool is None:
                print(f"    ! 无法找到代码列，跳过该日", file=sys.stderr)
                continue

            day_records = 0
            for r_idx in pool.index:
                code = str(pool.loc[r_idx, code_col_pool]).strip().zfill(6)
                name = ""
                if name_col_pool:
                    name = str(pool.loc[r_idx, name_col_pool])

                nxt_chg = next_change_map.get(code, None)
                nxt_up = bool(nxt_chg > 0) if nxt_chg is not None else None
                nxt_limit = bool(nxt_chg >= 9.5) if nxt_chg is not None else None

                all_records.append({
                    'date': day,
                    'code': code,
                    'name': name,
                    'seal_score': round(float(seal_scores[r_idx]), 2),
                    'tech_score': round(float(tech_scores[r_idx]), 2),
                    'sector_mom': round(float(sector_mom[r_idx]), 2),
                    'sector_res': round(float(sector_res[r_idx]), 2),
                    'next_day_change': round(nxt_chg, 2) if nxt_chg is not None else None,
                    'next_day_up': nxt_up,
                    'next_day_limit': nxt_limit,
                })
                day_records += 1

            processed_days += 1
            print(f"    → 本日添加 {day_records} 条，累计 {len(all_records)} 条", file=sys.stderr)

        except Exception as e:
            print(f"    ! 失败: {e}", file=sys.stderr)
            failed_days.append(day)
            import traceback
            traceback.print_exc()

    # ── 3. 保存 CSV ──
    print(f"\n{'=' * 60}", file=sys.stderr)
    if all_records:
        df_out = pd.DataFrame(all_records)
        df_out.to_csv(OUTPUT, index=False, encoding='utf-8-sig')
        print(f"  ✅ 成功保存 {len(df_out)} 条记录", file=sys.stderr)
    else:
        print(f"  ⚠️ 无数据生成，跳过保存", file=sys.stderr)

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"  报告摘要", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    print(f"  请求交易日数:    {len(trading_days)}", file=sys.stderr)
    print(f"  实际处理天数:    {processed_days}", file=sys.stderr)
    print(f"  总记录数:        {len(all_records)}", file=sys.stderr)
    print(f"  输出路径:        {OUTPUT}", file=sys.stderr)
    if failed_days:
        print(f"  失败天数:        {failed_days}", file=sys.stderr)

    # 输出总结（标准输出，供调用者解析）
    print(f"\nSUMMARY: days_requested={len(trading_days)} processed={processed_days} records={len(all_records)} failed={len(failed_days)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
