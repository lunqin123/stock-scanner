"""快速验证 factors_v2 (momentum_consistency / pullback_depth) 在 archive.db 上的历史可得性

数据 pipeline 断档 (例如 ``scripts/backfill_archive.py`` 写死 v8 glob 漏跑) 时,
mc/pd 因子会降级为 5.0 (中性) — 所有票都拿不到历史, 评分 v2 失效。本脚本就是
这个问题的快速体检工具。

用法:
    python scripts/audit_v2_factors.py                          # 用 archive.db 最新交易日的趋势池
    python scripts/audit_v2_factors.py --date 20260704          # 指定日期
    python scripts/audit_v2_factors.py --type limit_up          # 指定 stock_type
    python scripts/audit_v2_factors.py --min-cover 0.7          # 命中率告警阈值 (默认 0.5)

退出码: 命中率 < 阈值 时返回 1 (CI/运维可挂载)
"""
import argparse, sqlite3, sys
import pandas as pd

PROJECT_ROOT = r'C:\Users\16689\Desktop\stock-scanner'
DB_PATH = r'C:\Users\16689\Desktop\stock-scanner\archive.db'
sys.path.insert(0, PROJECT_ROOT)

from plans.factors_v2 import compute_momentum_consistency, compute_pullback_depth


def latest_trade_date(conn, stock_type: str) -> str:
    row = conn.execute(
        "SELECT MAX(trade_date) FROM daily_stocks WHERE stock_type=?",
        (stock_type,)).fetchone()
    return row[0] if row and row[0] else '20260101'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='YYYYMMDD (默认: archive.db 最新交易日)')
    ap.add_argument('--type', dest='stock_type', default='trend',
                    choices=['trend', 'limit_up', 'prev_pool', 'zhaban', 'dtqiaoban'])
    ap.add_argument('--min-cover', type=float, default=0.5,
                    help='双因子均命中率低于此值则退出 1 (默认 0.5)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    trade_date = args.date or latest_trade_date(conn, args.stock_type)

    df = pd.read_sql("""
        SELECT code, name, stock_type, trade_date, change_pct, price
        FROM daily_stocks
        WHERE trade_date = ? AND stock_type = ?
        ORDER BY change_pct DESC
    """, conn, params=(trade_date, args.stock_type))

    if df.empty:
        print(f"  [WARN] {trade_date} {args.stock_type} 池在 archive.db 中无数据")
        conn.close()
        sys.exit(2)

    # factors_v2 用 akshare 列名
    df['代码'] = df['code']
    df['最新价'] = df['price']

    fmt = '%Y-%m-%d' if '-' in trade_date else '%Y%m%d'
    if fmt == '%Y%m%d':
        today_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    else:
        today_str = trade_date

    mc = compute_momentum_consistency(df, today_str)
    pd_ = compute_pullback_depth(df, today_str)

    n = len(df)
    mc_valid = (mc != 5.0).sum()
    pd_valid = (pd_ != 5.0).sum()
    both_valid = ((mc != 5.0) & (pd_ != 5.0)).sum()
    cover_pct = both_valid / n

    print(f"=== {trade_date} {args.stock_type} pool: {n} stocks ===")
    print()
    print(f"=== V2 因子覆盖率 ===")
    print(f"  momentum_consistency 命中 (非 5.0): {mc_valid}/{n} = {mc_valid/n*100:.1f}%")
    print(f"  pullback_depth        命中 (非 5.0): {pd_valid}/{n} = {pd_valid/n*100:.1f}%")
    print(f"  双因子均命中:                {both_valid}/{n} = {both_valid/n*100:.1f}%")
    print()

    out = df[['code', 'name']].copy()
    out['mc'] = mc.values
    out['pd'] = pd_.values
    out['mc_str'] = out['mc'].apply(lambda x: f"{x:.1f}" + (" OK" if x != 5.0 else ""))
    out['pd_str'] = out['pd'].apply(lambda x: f"{x:.1f}" + (" OK" if x != 5.0 else ""))
    print(f"=== mc/pd 数值 (前 20 只) ===")
    print(out[['code', 'name', 'mc_str', 'pd_str']].head(20).to_string(index=False))
    print()

    if cover_pct < args.min_cover:
        print(f"!!! 告警: 双因子均命中率 {cover_pct:.1%} < 阈值 {args.min_cover:.0%}")
        print(f"    可能原因: archive.db 断档 / backfill_archive 未跑 / cache 升级未生效")
        conn.close()
        sys.exit(1)
    else:
        print(f"OK: 命中率 {cover_pct:.1%} >= 阈值 {args.min_cover:.0%}")
        conn.close()
        sys.exit(0)


if __name__ == '__main__':
    main()
