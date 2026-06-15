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

from cache import _trading_date, _is_trading_day
from t1_real_backtest import _next_trading_date
from datetime import datetime, timedelta


def _get_recent_redeem_codes(today_str: str, lookback_days: int = 5) -> set:
    """获取近期可转债到期/强赎的正股代码集合."""
    try:
        import akshare as ak
        df = ak.bond_cb_redeem_jsl()
        if df is None or df.empty:
            return set()

        # 找赎回日列
        date_col = None
        for c in df.columns:
            if '赎回' in str(c) or '到期' in str(c) or '最后' in str(c):
                date_col = c
                break
        if date_col is None:
            return set()

        today = datetime.strptime(today_str, '%Y%m%d')
        cutoff = today - timedelta(days=lookback_days)
        codes = set()
        for _, row in df.iterrows():
            rd = row.get(date_col)
            if rd is None or str(rd) in ('NaT', 'nan', ''):
                continue
            try:
                if hasattr(rd, 'date'):
                    rd_date = rd.date() if hasattr(rd, 'date') else rd
                else:
                    rd_date = datetime.strptime(str(rd)[:10], '%Y-%m-%d').date()
                if cutoff.date() <= rd_date <= today.date():
                    # 取正股代码列
                    stock_code = None
                    for c in df.columns:
                        val = str(row.get(c, ''))
                        if len(val) == 6 and val.isdigit() and (val.startswith('0') or val.startswith('3') or val.startswith('6')):
                            stock_code = val.zfill(6)
                            break
                    if stock_code:
                        codes.add(stock_code)
            except Exception:
                continue
        return codes
    except Exception:
        return set()


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
        # 先拉炸板名单，趋势池中排除炸板股（历史 EV -0.77%）
        import akshare as ak
        try:
            zb_raw = ak.stock_zt_pool_zbgc_em(date=today_str)
            zb_codes = set()
            if zb_raw is not None and not zb_raw.empty:
                zb_col = next((c for c in zb_raw.columns if '代码' in str(c)), zb_raw.columns[1])
                zb_codes = set(zb_raw[zb_col].astype(str).str.strip().str.zfill(6))
        except Exception:
            zb_codes = set()

        from backtest_engine import SIGNAL_POOL_FETCHERS, SCORE_FUNCS, SCORE_COLUMNS, TAB_TREND
        fetcher = SIGNAL_POOL_FETCHERS[TAB_TREND]
        score_fn = SCORE_FUNCS[TAB_TREND]
        score_col = SCORE_COLUMNS[TAB_TREND]

        pool = fetcher(today_str)
        # 排除炸板股
        if pool is not None and not pool.empty and zb_codes:
            code_col_t = next((c for c in pool.columns if '代码' in str(c)), None)
            if code_col_t:
                pool = pool[~pool[code_col_t].astype(str).str.strip().str.zfill(6).isin(zb_codes)]
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

    # ── 可转债到期/强赎过滤 ──
    try:
        redeem_codes = _get_recent_redeem_codes(today_str)
        if redeem_codes:
            filtered = []
            for s in signals:
                if s['code'] in redeem_codes:
                    alerts.append(f'{s["tab_cn"]} {s["name"]}({s["code"]}) — 转债近期到期/强赎, 抛压风险, 排除')
                else:
                    filtered.append(s)
            signals = filtered
    except Exception as e:
        alerts.append(f'转债检查异常: {e}')

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
