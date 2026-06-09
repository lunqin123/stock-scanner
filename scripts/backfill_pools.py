#!/usr/bin/env python3
"""
一次性回填脚本 — 遍历最近 N 个交易日, 补拉各池数据并存为 pickle。

受 akshare ~7 天窗口限制, 首次回填只能补最近一周。
之后每天 archiver.py 自动运行, 本地数据持续积累。

用法:
    python scripts/backfill_pools.py           # 默认 7 天
    python scripts/backfill_pools.py --days 10 # 指定天数
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
from datetime import datetime, timedelta
from cache import _is_trading_day

# 导入 archiver 的 pickle 保存函数
from archiver import _save_pool_pickle, _ARCHIVE_POOL_DIR, _ensure_archive_dir

# 池类型 → (akshare 函数, 描述)
POOLS = {
    'limit_up':     (lambda d: ak.stock_zt_pool_em(date=d),           '涨停池'),
    'prev_pool':    (lambda d: ak.stock_zt_pool_previous_em(date=d), '上交易日涨停(反转)'),
    'zhaban':       (lambda d: ak.stock_zt_pool_zbgc_em(date=d),     '炸板池'),
    'dtqiaoban':    (lambda d: ak.stock_zt_pool_dtgc_em(date=d),     '跌停/翘板池'),
    'strong':       (lambda d: ak.stock_zt_pool_strong_em(date=d),   '强势池(趋势)'),
}


def main(days: int = 7):
    """遍历最近 days 个交易日, 拉取所有池数据并存 pickle"""
    print(f"[回填] 开始, 目标: 最近 {days} 个交易日")
    _ensure_archive_dir()

    # 生成最近 N 个交易日列表
    today = datetime.now().strftime('%Y%m%d')
    trading_dates = []
    cur = datetime.now()
    while len(trading_dates) < days:
        ds = cur.strftime('%Y%m%d')
        if _is_trading_day(ds):
            trading_dates.append(ds)
        cur -= timedelta(days=1)
        if len(trading_dates) > 60:  # safety
            break

    trading_dates.sort()
    print(f"  交易日: {trading_dates[0]} ~ {trading_dates[-1]} ({len(trading_dates)} 天)")

    total_saved = 0
    total_failed = 0

    for trade_date in trading_dates:
        for pool_type, (fetch_fn, desc) in POOLS.items():
            # 检查是否已有
            path = os.path.join(_ARCHIVE_POOL_DIR, f'{pool_type}_{trade_date}.pkl')
            if os.path.exists(path):
                print(f"  [跳过] {trade_date} {pool_type} ({desc}) — 已有")
                continue

            try:
                df = fetch_fn(trade_date)
                if df is not None and hasattr(df, 'empty') and not df.empty:
                    _save_pool_pickle(trade_date, pool_type, df)
                    print(f"  [OK]   {trade_date} {pool_type} ({desc}) — {len(df)} 条")
                    total_saved += 1
                else:
                    print(f"  [空]   {trade_date} {pool_type} ({desc}) — 无数据(akshare 窗口限制)")
            except Exception as e:
                print(f"  [FAIL] {trade_date} {pool_type} ({desc}) — {type(e).__name__}: {str(e)[:80]}")
                total_failed += 1
            time.sleep(0.3)  # 限流

    print(f"\n[回填] 完成: 保存 {total_saved}, 失败 {total_failed}")
    print(f"  归档目录: {_ARCHIVE_POOL_DIR}")

    # 列出已归档的文件
    if os.path.exists(_ARCHIVE_POOL_DIR):
        files = sorted(os.listdir(_ARCHIVE_POOL_DIR))
        print(f"  共 {len(files)} 个 pickle 文件:")
        for f in files:
            print(f"    {f}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='回填池数据到本地 pickle 归档')
    parser.add_argument('--days', type=int, default=7, help='回填天数 (默认 7)')
    args = parser.parse_args()
    main(days=args.days)
