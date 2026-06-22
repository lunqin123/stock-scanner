"""backfill archive.db daily_stocks 从 2h 引擎缓存

读取 data/cache/persistent_engine_*.pkl (Tier1.A 升级的),
把所有池 (limit-up / trend / reversal / zhaban / dtqiaoban) 数据补到 archive.db daily_stocks 表。
对 limit-up 类型同时尝试 backfill next_day_change (从 stock_daily 关联)。
对其他类型, next_day_change 留空 (后续 backfill 脚本可填)。
"""
import os, glob, pickle, sqlite3
from datetime import datetime, timedelta

DB = r'C:\Users\16689\Desktop\stock-scanner\archive.db'
CACHE = r'C:\Users\16689\Desktop\stock-scanner\data\cache'

# 列名映射 (akshare 返回的中文列 → archive.db schema)
COL_MAP = {
    '代码': 'code', '名称': 'name',
    '涨跌幅': 'change_pct',
    '最新价': 'price', '换手率': 'turnover',
    '封板资金': 'seal_fund',
    '首次封板时间': 'seal_time',
    '炸板次数': 'zhaban_times',
    '连板数': 'consecutive',
    '所属行业': 'industry',
    '流通市值': 'market_cap',
    '成交量': 'volume',
}

# engine_* cache 文件 → stock_type
# Tier2.E 扩展: 加入 reversal / zhaban / dtqiaoban
POOL_MAP = {
    'engine_limit_up_': 'limit_up',
    'engine_trend_': 'trend',
    'engine_reversal_': 'prev_pool',     # reversal 池 = prev_pool (上交易日涨停)
    'engine_zhaban_': 'zhaban',
    'engine_dtqiaoban_': 'dtqiaoban',
}

def parse_date(date_str: str) -> str:
    if len(date_str) == 8 and date_str.isdigit():
        return date_str
    return None

def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    added = 0
    skipped = 0
    next_day_filled = 0
    files = sorted(glob.glob(os.path.join(CACHE, 'persistent_engine_*_v8.pkl')))
    for f in files:
        name = os.path.basename(f)
        # 解析 pool_type + date
        prefix = None
        for p in POOL_MAP:
            if name.startswith('persistent_' + p):
                prefix = p
                break
        if prefix is None:
            continue
        stock_type = POOL_MAP[prefix]
        # persistent_engine_limit_up_20260612_v8.pkl
        rest = name[len('persistent_' + prefix):-len('_v8.pkl')]
        date_str = parse_date(rest)
        if date_str is None:
            print(f'  [skip] 无法解析日期: {name}')
            skipped += 1
            continue

        try:
            df = pickle.load(open(f, 'rb'))
        except Exception as e:
            print(f'  [err] {name}: {e}')
            continue
        if df is None or (hasattr(df, 'empty') and df.empty):
            skipped += 1
            continue

        # 过滤 ST/科创/北交/创业板 (与 fetch_limit_up_pool 一致)
        try:
            import sys
            sys.path.insert(0, r'C:\Users\16689\Desktop\stock-scanner')
            from scanner import filter_non_main_board
            df = filter_non_main_board(df)
        except Exception:
            pass
        if df.empty:
            skipped += 1
            continue

        # 写 daily_stocks
        for _, row in df.iterrows():
            try:
                code = str(row.get('代码', '')).strip().zfill(6)
                if len(code) != 6: continue
                name = str(row.get('名称', ''))
                chg = float(row.get('涨跌幅', 0) or 0)
                price = float(row.get('最新价', 0) or 0)
                turnover = float(row.get('换手率', 0) or 0)
                seal_fund = float(row.get('封板资金', 0) or 0)
                seal_time = str(row.get('首次封板时间', '') or '')
                zhaban = int(float(row.get('炸板次数', 0) or 0))
                cons = int(float(row.get('连板数', 0) or 0))
                industry = str(row.get('所属行业', '') or '')
                mkt_cap = float(row.get('流通市值', 0) or 0)
                vol = float(row.get('成交量', 0) or 0)
            except Exception:
                continue

            # 已有则跳过
            cur.execute(
                "SELECT 1 FROM daily_stocks WHERE code=? AND trade_date=? AND stock_type=?",
                (code, date_str, stock_type)
            )
            if cur.fetchone():
                skipped += 1
                continue

            cur.execute(
                """INSERT INTO daily_stocks
                (trade_date, code, name, stock_type, change_pct, price, turnover,
                 seal_time, seal_fund, zhaban_times, consecutive, industry,
                 market_cap, volume, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'), datetime('now','localtime'))""",
                (date_str, code, name, stock_type, chg, price, turnover,
                 seal_time, seal_fund, zhaban, cons, industry, mkt_cap, vol)
            )
            added += 1

        # 对 limit_up: 关联 stock_daily 补 next_day_change
        if stock_type == 'limit_up':
            next_date = (datetime.strptime(date_str, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
            for _, row in df.iterrows():
                try:
                    code = str(row.get('代码', '')).strip().zfill(6)
                    if len(code) != 6: continue
                    cur.execute(
                        "SELECT chg_pct FROM stock_daily WHERE code=? AND trade_date=?",
                        (code, next_date)
                    )
                    sd = cur.fetchone()
                    if sd and sd[0] is not None:
                        cur.execute(
                            "UPDATE daily_stocks SET next_day_change=? "
                            "WHERE code=? AND trade_date=? AND stock_type='limit_up'",
                            (sd[0], code, date_str)
                        )
                        if cur.rowcount > 0:
                            next_day_filled += 1
                except Exception:
                    continue

    conn.commit()
    conn.close()
    print(f'\n=== backfill 完成 ===')
    print(f'  新增: {added} 行')
    print(f'  跳过: {skipped} (已存在或无日期)')
    print(f'  补 next_day_change: {next_day_filled} 行')

if __name__ == '__main__':
    main()
