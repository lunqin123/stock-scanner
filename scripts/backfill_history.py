#!/usr/bin/env python3
"""
回填历史数据 — 利用腾讯OHLCV拉30天历史, 反推涨停池/强势池等。

akshare池API只返回~7天数据, 但腾讯OHLCV无此限制。
思路: 从已有池数据收集所有活跃股票 → 拉30天OHLCV → 按涨幅反推各池。

用法: python scripts/backfill_history.py [--days 30]
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
from cache import _is_trading_day, _load_trading_calendar
from archiver import (_ensure_archive_dir, _save_pool_pickle, _ARCHIVE_POOL_DIR,
                      list_archive_dates, _load_pool_pickle)


def main(days: int = 30):
    print(f"[历史回填] 目标: {days}天")

    # 1. 收集所有已归档池中出现过的股票代码
    all_codes = set()
    for pool_type in ['limit_up', 'prev_pool', 'zhaban', 'dtqiaoban', 'strong']:
        dates = list_archive_dates(pool_type)
        for d in dates[:10]:  # 最近10天
            df = _load_pool_pickle(d, pool_type)
            if df is not None and not df.empty:
                code_col = '代码' if '代码' in df.columns else (df.columns[1] if len(df.columns) > 1 else None)
                if code_col:
                    for _, row in df.iterrows():
                        code = str(row[code_col]).strip().zfill(6)
                        if len(code) == 6:
                            all_codes.add(code)

    codes = list(all_codes)[:100]  # 限100只, 太多会超时
    print(f"  活跃股票: {len(all_codes)}只 → 取前100只")

    if not codes:
        return

    # 2. 生成目标交易日列表(最近N个交易日)
    calendar = _load_trading_calendar()
    today = datetime.now()
    trading_dates = []
    cur = today
    while len(trading_dates) < days:
        ds = cur.strftime('%Y%m%d')
        if ds in calendar:
            trading_dates.append(ds)
        cur -= timedelta(days=1)
        if len(trading_dates) > days + 10:
            break
    trading_dates.sort()
    print(f"  交易日: {trading_dates[0]} ~ {trading_dates[-1]} ({len(trading_dates)}天)")

    # 3. 逐日拉取OHLCV → 反推各池
    saved_pools = {pt: set() for pt in ['limit_up', 'strong', 'zhaban', 'dtqiaoban']}
    total_fetched = 0
    codes_per_batch = 50

    for trade_date in trading_dates:
        date_fmt = f'{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}'
        print(f"  [{trade_date}] 拉取{len(codes)}只股票OHLCV...", end=' ', flush=True)

        # 检查是否已有该日数据
        existing = set()
        for pt in saved_pools:
            if _load_pool_pickle(trade_date, pt) is not None:
                existing.add(pt)
        if len(existing) == 4:
            print("全部已有, 跳过")
            continue

        # 并发拉取(每批30只, 8线程)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        day_data = {}

        def _fetch_one(code):
            prefix = 'sh' if code.startswith('6') else 'sz'
            try:
                df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}',
                                           start_date=date_fmt, end_date=date_fmt)
                if df is not None and not df.empty:
                    row = df.iloc[-1]
                    chg = (row['close'] / row['open'] - 1) * 100 if row['open'] > 0 else 0
                    return code, {'open': float(row['open']), 'close': float(row['close']),
                                  'high': float(row['high']), 'low': float(row['low']),
                                  'chg': round(chg, 2),
                                  'amount': float(row.get('amount', 0) or 0)}
            except Exception:
                pass
            return code, None

        for i in range(0, len(codes), 30):
            batch = codes[i:i+30]
            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(_fetch_one, c): c for c in batch}
                for f in as_completed(futures):
                    code, data = f.result()
                    if data is not None:
                        day_data[code] = data
            time.sleep(0.1)

        total_fetched += len(day_data)
        print(f"{len(day_data)}只有效", flush=True)

        if not day_data:
            continue

        # 反推涨停池: chg >= 9.5%
        limit_up = {c: d for c, d in day_data.items() if d['chg'] >= 9.5}
        if limit_up and 'limit_up' not in existing:
            _save_pool_pickle(trade_date, 'limit_up', _build_fake_pool_df(limit_up))
            saved_pools['limit_up'].add(trade_date)
            print(f"    涨停池: {len(limit_up)}只")

        # 反推强势池: chg 2.5~9.4%
        strong = {c: d for c, d in day_data.items() if 2.5 <= d['chg'] < 9.5}
        if strong and 'strong' not in existing:
            _save_pool_pickle(trade_date, 'strong', _build_fake_pool_df(strong))
            saved_pools['strong'].add(trade_date)
            print(f"    强势池: {len(strong)}只")

        # 反推跌停/翘板池: chg <= -9.5%
        dieting = {c: d for c, d in day_data.items() if d['chg'] <= -9.5}
        if dieting and 'dtqiaoban' not in existing:
            _save_pool_pickle(trade_date, 'dtqiaoban', _build_fake_pool_df(dieting))
            saved_pools['dtqiaoban'].add(trade_date)
            print(f"    翘板池: {len(dieting)}只")

        # 反推prev_pool: 用前一天涨停股+今日涨幅 (下一轮会生成)
        prev_date = _get_prev_trading_date(trade_date)
        if prev_date:
            prev_limit = {c: d for c, d in day_data.items() if c in _get_pool_codes(prev_date, 'limit_up')}
            if prev_limit and 'prev_pool' not in existing:
                _save_pool_pickle(trade_date, 'prev_pool', _build_fake_pool_df(prev_limit))
                saved_pools['prev_pool'].add(trade_date)

    print(f"\n[历史回填] 完成: OHLCV拉取{total_fetched}条, 池保存{sum(len(v) for v in saved_pools.values())}天")


def _get_prev_trading_date(date_str: str) -> str:
    """获取上一个交易日"""
    from cache import _last_trading_date
    return _last_trading_date(date_str)


def _get_pool_codes(date_str: str, pool_type: str) -> set:
    """读取已保存的池数据中的股票代码集合"""
    df = _load_pool_pickle(date_str, pool_type)
    if df is None or df.empty:
        return set()
    code_col = '代码' if '代码' in df.columns else (df.columns[1] if len(df.columns) > 1 else None)
    if not code_col:
        return set()
    return {str(row[code_col]).strip().zfill(6) for _, row in df.iterrows()}


def _build_fake_pool_df(data: dict) -> pd.DataFrame:
    """从OHLCV数据构造近似涨停池DataFrame(列名对齐akshare)"""
    rows = []
    for code, d in data.items():
        rows.append({
            '代码': code,
            '名称': code,  # 无名称, 用代码占位
            '涨跌幅': d['chg'],
            '最新价': d['close'],
            '成交额': d['amount'],
            '换手率': 0,
            '流通市值': 0,
            '所属行业': '',
            '首次封板时间': '',
            '封板资金': 0,
            '连板数': 1,
            '炸板次数': 0,
        })
    return pd.DataFrame(rows)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='回填历史池数据')
    parser.add_argument('--days', type=int, default=30, help='回填天数(默认30)')
    args = parser.parse_args()
    main(days=args.days)
