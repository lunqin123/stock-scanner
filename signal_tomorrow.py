"""明日买入信号 — 基于回测数据挖掘的实盘决策引擎

使用盘前可知信息 (rank, score, 星期), 不含 gap 等未来数据。
复用回测引擎的取池和评分函数, 确保和回测口径一致。

用法:
    python signal_tomorrow.py            # CLI 输出明日信号
    python signal_tomorrow.py --json     # JSON 输出
"""

import sys
from datetime import datetime

sys.path.insert(0, '.')

from cache import _trading_date
from t1_real_backtest import _next_trading_date


def generate_signals(today_str: str = None) -> dict:
    if today_str is None:
        today_str = _trading_date().replace('-', '')

    next_td = _next_trading_date(today_str)
    if next_td is None:
        return {'date': today_str, 'next_trade_day': None,
                'signals': [], 'alerts': [], 'summary': '明日非交易日, 无信号'}

    w = datetime.strptime(today_str, '%Y%m%d').weekday()
    weekday_name = ['周一','周二','周三','周四','周五','周六','周日'][w]
    signals = []
    alerts = []

    # ── 涨停信号: 复用 backtest_engine 的 fetcher + scorer ──
    try:
        from backtest_engine import SIGNAL_POOL_FETCHERS, SCORE_FUNCS, SCORE_COLUMNS, TAB_LIMIT_UP
        fetcher = SIGNAL_POOL_FETCHERS[TAB_LIMIT_UP]
        score_fn = SCORE_FUNCS[TAB_LIMIT_UP]
        score_col = SCORE_COLUMNS[TAB_LIMIT_UP]

        pool = fetcher(today_str)
        if pool is not None and not (hasattr(pool, 'empty') and pool.empty):
            df = score_fn(pool, today_str)
            if df is not None and not (hasattr(df, 'empty') and df.empty):
                col = score_col if score_col in df.columns else df.columns[-1]
                df = df.sort_values(col, ascending=False)

                for rank, (_, row) in enumerate(df.iterrows(), 1):
                    if rank > 1: break
                    code = str(row.get('代码', '')).strip().zfill(6)
                    name = str(row.get('名称', ''))
                    score = float(row.get(col, 0))

                    if w not in [1, 4]:
                        alerts.append(f'涨停 {name}({code}) rank1 评分{score:.0f} — {weekday_name}非周二/五, 跳过')
                        break
                    if score > 74:
                        alerts.append(f'涨停 {name}({code}) rank1 评分{score:.0f} — Q4陷阱(>74)')
                        break
                    if score < 38:
                        alerts.append(f'涨停 {name}({code}) rank1 评分{score:.0f} — 评分偏低(<38)')
                        break

                    signals.append({
                        'tab': 'limit-up', 'tab_cn': '涨停板', 'strategy': 'A开盘买',
                        'code': code, 'name': name, 'score': round(score, 1), 'rank': rank,
                        'signal_date': today_str, 'buy_date': next_td,
                        'reason': '周二/五+rank1+评分38~74(历史EV+2.12%)',
                    })
    except Exception as e:
        alerts.append(f'涨停扫描: {e}')

    # ── 趋势信号 ──
    try:
        from backtest_engine import SIGNAL_POOL_FETCHERS, SCORE_FUNCS, SCORE_COLUMNS, TAB_TREND
        fetcher = SIGNAL_POOL_FETCHERS[TAB_TREND]
        score_fn = SCORE_FUNCS[TAB_TREND]
        score_col = SCORE_COLUMNS[TAB_TREND]

        pool = fetcher(today_str)
        if pool is not None and not (hasattr(pool, 'empty') and pool.empty):
            df = score_fn(pool, today_str)
            if df is not None and not (hasattr(df, 'empty') and df.empty):
                col = score_col if score_col in df.columns else df.columns[-1]
                df = df.sort_values(col, ascending=False)

                for rank, (_, row) in enumerate(df.iterrows(), 1):
                    if rank > 1: break
                    code = str(row.get('代码', '')).strip().zfill(6)
                    name = str(row.get('名称', ''))
                    score = float(row.get(col, 0))

                    if w not in [0, 1]:
                        alerts.append(f'趋势 {name}({code}) rank1 评分{score:.0f} — {weekday_name}非周一/二, 跳过')
                        break
                    if score > 69:
                        alerts.append(f'趋势 {name}({code}) rank1 评分{score:.0f} — Q4陷阱')
                        break

                    signals.append({
                        'tab': 'trend', 'tab_cn': '趋势股', 'strategy': 'A开盘买',
                        'code': code, 'name': name, 'score': round(score, 1), 'rank': rank,
                        'signal_date': today_str, 'buy_date': next_td,
                        'reason': '周一/二+rank1(历史EV+3.76%)',
                    })
    except Exception as e:
        alerts.append(f'趋势扫描: {e}')

    if signals:
        names = [s['name'] for s in signals]
        summary = f'{weekday_name}信号: {len(signals)}只 — 明日({next_td})开盘买入: {", ".join(names)}'
    else:
        summary = f'{weekday_name} — 今日无符合规则的买入信号'

    return {
        'date': today_str, 'weekday': weekday_name, 'next_trade_day': next_td,
        'signals': signals, 'alerts': alerts, 'summary': summary,
    }


# ═══════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import json as _json
    result = generate_signals()

    if '--json' in sys.argv:
        print(_json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('=' * 50)
        print(f'  明日买入信号 ({result["date"]} {result.get("weekday","")})')
        print('=' * 50)

        if result['alerts']:
            print('\n[跳过]')
            for a in result['alerts']:
                print(f'  ✗ {a}')

        if result['signals']:
            print('\n[买入]')
            for s in result['signals']:
                print(f'  ✅ {s["tab_cn"]} {s["name"]}({s["code"]})')
                print(f'     评分: {s["score"]} | 策略: {s["strategy"]}')
                print(f'     买入日: {s["buy_date"]} | {s["reason"]}')
        else:
            print('\n  (无信号)')

        print(f'\n{result["summary"]}')
