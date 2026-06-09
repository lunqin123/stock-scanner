"""V0 实测脚本: 验证 akshare 各 API 历史 date 参数可用性

目标 API:
- stock_zt_pool_zbgc_em   (炸板)
- stock_zt_pool_dtgc_em   (跌停/翘板)
- stock_zt_pool_strong_em (强势/趋势)
- stock_zt_pool_em        (涨停池, 作为对照)

对每个 API 拉最近 10 个交易日的数据,统计:
- 成功率 (非空返回的比例)
- 数据量中位数
- 失败原因 (网络/限流/接口限制)
"""
import sys, time, traceback
sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")
import akshare as ak
from datetime import datetime, timedelta
from cache import _is_trading_day


def get_last_n_trading_days(n: int = 10) -> list:
    """最近 n 个交易日 (YYYYMMDD),倒序"""
    days = []
    cur = datetime.now()
    while len(days) < n:
        cur -= timedelta(days=1)
        d_str = cur.strftime("%Y%m%d")
        if _is_trading_day(d_str):
            days.append(d_str)
    return days


def test_api(name: str, api_fn, date_str: str, timeout: int = 15) -> dict:
    """调一次 API,返回 {ok, n_rows, err, elapsed}"""
    t0 = time.time()
    try:
        df = api_fn(date=date_str)
        elapsed = time.time() - t0
        if df is None:
            return {'ok': False, 'n_rows': 0, 'err': 'None', 'elapsed': elapsed}
        if hasattr(df, 'empty') and df.empty:
            return {'ok': False, 'n_rows': 0, 'err': 'empty', 'elapsed': elapsed}
        return {'ok': True, 'n_rows': len(df), 'err': None, 'elapsed': elapsed}
    except Exception as e:
        elapsed = time.time() - t0
        return {'ok': False, 'n_rows': 0, 'err': f'{type(e).__name__}: {str(e)[:80]}', 'elapsed': elapsed}


def main():
    days = get_last_n_trading_days(10)
    print(f"实测区间: {days[0]} ~ {days[-1]} ({len(days)} 个交易日)")
    print("=" * 80)

    apis = [
        ('涨停池(对照)',   ak.stock_zt_pool_em),
        ('炸板池',        ak.stock_zt_pool_zbgc_em),
        ('跌停/翘板池',    ak.stock_zt_pool_dtgc_em),
        ('强势/趋势池',    ak.stock_zt_pool_strong_em),
    ]

    results = {}
    for name, fn in apis:
        print(f"\n[{name}] {fn.__name__}")
        print("-" * 80)
        ok_count = 0
        rows_list = []
        for d in days:
            r = test_api(name, fn, d)
            mark = 'OK ' if r['ok'] else 'FAIL'
            rows_str = f"{r['n_rows']:>5} 只" if r['ok'] else '  -- '
            err_str = f"  [{r['err']}]" if not r['ok'] else ''
            print(f"  {d}  {mark}  {rows_str}  ({r['elapsed']:.1f}s){err_str}")
            if r['ok']:
                ok_count += 1
                rows_list.append(r['n_rows'])
            time.sleep(0.5)  # 防限流

        success_rate = ok_count / len(days) * 100
        med_rows = sorted(rows_list)[len(rows_list)//2] if rows_list else 0
        print(f"  → 成功率 {success_rate:.0f}% ({ok_count}/{len(days)}) | 数据量中位数 {med_rows}")
        results[name] = {
            'success_rate': success_rate,
            'ok_count': ok_count,
            'total': len(days),
            'median_rows': med_rows,
        }

    print("\n" + "=" * 80)
    print(" 汇总:")
    print("=" * 80)
    print(f"{'API':<20} {'成功率':<10} {'数据量中位数':<12} {'结论'}")
    for name, r in results.items():
        if r['success_rate'] >= 90:
            verdict = 'OK 可用于历史回测'
        elif r['success_rate'] >= 50:
            verdict = 'WARN 部分可用,需降级'
        else:
            verdict = 'FAIL 不可用,改用累积模式'
        print(f"{name:<20} {r['success_rate']:>5.0f}%    {r['median_rows']:>5} 只      {verdict}")


if __name__ == '__main__':
    main()