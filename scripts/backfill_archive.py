"""backfill archive.db daily_stocks 从引擎持久化缓存

读取 data/cache/ 下两类持久化文件, 把它们对应的池 (limit-up / trend / reversal /
zhaban / dtqiaoban) 数据补到 archive.db daily_stocks 表:

  1. ``engine_*_v<N>.pkl``           — 当下格式, 由 backtest_engine.py
       :func:`_pool_cache_put` 调用 ``cache.persistent_put('engine_xxx', df)`` 生成
       (例: ``engine_limit_up_20260615_v10.pkl``)。
  2. ``persistent_engine_*_v<N>.pkl`` — 历史格式, 由旧版
       ``scripts/backfill_engine_cache.py`` 复制生成 (已废弃, 仅兼容)。

两者都接受, 同 (pool_type, trade_date) 取最高版本 (高版本缓存覆盖低版本的事实)。
对 limit-up 类型同时尝试 backfill next_day_change (从 stock_daily 关联)。
对其他类型, next_day_change 留空 (后续 backfill 脚本可填)。

历史变更:
- 6/17 之后数据 pipeline 断档 — 修此脚本后已恢复。 见 docstring。
"""
import os, re, glob, pickle, sqlite3
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

# engine_* cache 文件名 → stock_type
# Tier2.E 扩展: 加入 reversal / zhaban / dtqiaoban
POOL_MAP = {
    'engine_limit_up_': 'limit_up',
    'engine_trend_': 'trend',
    'engine_reversal_': 'prev_pool',     # reversal 池 = prev_pool (上交易日涨停)
    'engine_zhaban_': 'zhaban',
    'engine_dtqiaoban_': 'dtqiaoban',
}

# 两种文件名格式的解析: ``persistent_engine_xxx_<date>_v<N>.pkl``
# 和 ``engine_xxx_<date>_v<N>.pkl`` 都接受, 抽 (pool_suffix, trade_date, version)。
# 历史版本 (v8 / v9 / v10) 数据格式相同 (老 DataFrame), 仅版本号含义不同
# (cache schema bump), 列名一致, 复用同一解析逻辑。
_FILENAME_RE = re.compile(
    r'^(?:(?P<prefix>persistent_))?'
    r'(?P<pool>engine_(?:limit_up|trend|reversal|zhaban|dtqiaoban))'
    r'_(?P<date>\d{8})_v(?P<ver>\d+)\.pkl$'
)

def parse_date(date_str: str) -> str:
    if len(date_str) == 8 and date_str.isdigit():
        return date_str
    return None

def discover_pool_files(cache_dir: str) -> list:
    """扫描 cache 目录, 抽出全部 (suffix, date_str, version, path)。

    同 (suffix, date_str) 可能因为版本更迭而存在多个文件 (例如同时存在
    v8 与 v10)。调用方负责按版本号取最新, 我们这里全部列出。
    返回按 (date, suffix) 排序, 方便断点续跑场景。
    """
    found = []
    # 既要匹配 ``engine_xxx_...`` 也要匹配 ``persistent_engine_xxx_...``
    # — 所以前面是 0 或多个任意字符。
    for f in glob.glob(os.path.join(cache_dir, '*engine_*_v*.pkl')):
        name = os.path.basename(f)
        # 兼容 ``engine_dtqiaoban_20260612_v8.pkl`` — 不带 persistent_ 前缀
        m = _FILENAME_RE.match(name)
        if not m:
            # 回退: 即使正则没匹配也试一种简化模式 (兼容老命名)
            m2 = re.match(r'^(persistent_)?(?P<pool>engine_(?:limit_up|trend|reversal|zhaban|dtqiaoban))_(?P<date>\d{8})_v(?P<ver>\d+)\.pkl$', name)
            if not m2:
                print(f'  [skip] 文件名无法解析: {name}')
                continue
            m = m2
        found.append({
            'prefix': m.group('prefix') or '',
            'pool': m.group('pool'),
            'date_str': m.group('date'),
            'version': int(m.group('ver')),
            'path': f,
        })
    found.sort(key=lambda x: (x['date_str'], x['pool'], x['version']))
    return found

def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    added = 0
    skipped = 0
    next_day_filled = 0

    # 1. 扫描全部候选文件
    candidates = discover_pool_files(CACHE)
    if not candidates:
        print('  [WARN] 没扫到任何候选文件, 检查 CACHE 路径')
        return

    # 2. 同 (pool, date) 取最高版本 (高版本覆盖低版本是 cache 版本号约定)
    latest_by_key = {}
    for cand in candidates:
        key = (cand['pool'], cand['date_str'])
        if key not in latest_by_key or cand['version'] > latest_by_key[key]['version']:
            latest_by_key[key] = cand
    dropped = len(candidates) - len(latest_by_key)
    print(f'  候选文件 {len(candidates)}, 去重后 {len(latest_by_key)}'
          f' (丢弃 {dropped} 个低版本副本)')

    for key, cand in sorted(latest_by_key.items(), key=lambda x: (x[0][1], x[0][0])):
        pool = cand['pool']                        # 例 ``engine_limit_up``
        date_str = cand['date_str']
        version = cand['version']
        f = cand['path']
        # POOL_MAP key 是 ``engine_xxx_`` 形式, 末尾带下划线; pool 是不带下划线的
        prefix = pool + '_'
        if prefix not in POOL_MAP:
            print(f'  [skip] 未知 pool 类型: {pool}')
            skipped += 1
            continue
        stock_type = POOL_MAP[prefix]
        print(f'  [{date_str} v{version}] {pool} → {stock_type}  ({os.path.basename(f)})')

        try:
            df = pickle.load(open(f, 'rb'))
        except Exception as e:
            print(f'  [err] {f}: {e}')
            continue
        # None marker / '__NONE__' 哨兵 / 空 DataFrame 都跳过
        if df is None:
            skipped += 1
            continue
        if isinstance(df, str):
            if df == '__NONE__':
                skipped += 1
                continue
            # 其它字符串 — 异常内容, 跳过
            print(f'  [skip] 非预期 str 内容: {os.path.basename(f)} → {df[:50]}')
            skipped += 1
            continue
        if hasattr(df, 'empty') and df.empty:
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
