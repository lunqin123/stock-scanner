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


def _get_warning_codes(today_str: str) -> dict:
    """获取有风险的股票代码及原因.

    检测项:
    1. 可转债溢价率<-10% 或 债现价=100 → 即将到期/强赎, 转股抛压
    2. 近5日可转债到期/强赎
    Returns: {code: reason}
    """
    warnings = {}
    try:
        import akshare as ak

        # 方法1: bond_zh_cov 检测异常信号
        try:
            cov_df = ak.bond_zh_cov()
            if cov_df is not None and not cov_df.empty:
                stock_col = None
                for c in cov_df.columns:
                    if '正股代码' in str(c):
                        stock_col = c
                        break
                prem_col = next((c for c in cov_df.columns if '溢价' in str(c)), None)
                price_col = next((c for c in cov_df.columns if '现价' in str(c) or '债现价' in str(c)), None)

                if stock_col:
                    for _, row in cov_df.iterrows():
                        code = str(row.get(stock_col, '')).strip().zfill(6)
                        if not code or len(code) != 6:
                            continue

                        reasons = []
                        # 转股溢价率 < -10%: 折价异常, 通常意味着即将到期
                        if prem_col:
                            prem = row.get(prem_col)
                            try:
                                if prem is not None and float(prem) < -10:
                                    reasons.append(f'转股溢价率{float(prem):.0f}%异常')
                            except (ValueError, TypeError):
                                pass
                        # 债现价 = 100: 回到面值, 可能是到期/强赎
                        if price_col:
                            price = row.get(price_col)
                            try:
                                if price is not None and abs(float(price) - 100) < 0.5:
                                    reasons.append('债价=100(到期面值)')
                            except (ValueError, TypeError):
                                pass
                        if reasons:
                            warnings[code] = '转债' + ','.join(reasons)
        except Exception:
            pass

        # 方法2: bond_cb_redeem_jsl 近期到期
        try:
            redeem_df = ak.bond_cb_redeem_jsl()
            if redeem_df is not None and not redeem_df.empty:
                date_col = None
                for c in redeem_df.columns:
                    if '到期' in str(c) or '最后' in str(c):
                        date_col = c
                        break
                stock_col = None
                for c in redeem_df.columns:
                    if '正股代码' in str(c):
                        stock_col = c
                        break

                if date_col and stock_col:
                    today = datetime.strptime(today_str, '%Y%m%d').date()
                    cutoff = today - timedelta(days=5)
                    for _, row in redeem_df.iterrows():
                        rd = row.get(date_col)
                        if rd is None or str(rd) in ('NaT', 'nan', ''):
                            continue
                        try:
                            rd_date = rd if hasattr(rd, 'date') else datetime.strptime(str(rd)[:10], '%Y-%m-%d').date()
                            if hasattr(rd_date, 'date'):
                                rd_date = rd_date.date()
                            if cutoff <= rd_date <= today:
                                code = str(row.get(stock_col, '')).strip().zfill(6)
                                if code and len(code) == 6:
                                    if code not in warnings:
                                        warnings[code] = f'转债{rd_date}到期'
                        except Exception:
                            continue
        except Exception:
            pass

    except Exception:
        pass
    return warnings


def generate_signals(today_str: str = None, settings: dict = None) -> dict:
    """生成明日买入信号

    Args:
        today_str: YYYYMMDD, 默认今天
        settings: 可选参数覆盖, 格式:
            {'limit-up': {'top_n': 3, 'min_score': 50},
             'zhaban': {'top_n': 3, 'min_score': 50, 'sell_n': 5},
             'trend': {'top_n': 1, 'min_score': 55}}
            不传则用函数内部默认值
    """
    if today_str is None:
        today_str = _trading_date().replace('-', '')

    settings = settings or {}
    lu_set = settings.get('limit-up', {})
    zb_set = settings.get('zhaban', {})
    tr_set = settings.get('trend', {})
    lu_top_n = lu_set.get('top_n', 3)
    lu_min = lu_set.get('min_score', 38)
    zb_top_n = zb_set.get('top_n', 3)
    zb_min = zb_set.get('min_score', 50)
    zb_sell_n = zb_set.get('sell_n', 5)
    tr_top_n = tr_set.get('top_n', 1)
    tr_min = tr_set.get('min_score', 45)
    _seller = f'持{zb_sell_n}天'

    # ── 涨停信号: 周二/三/五, top-3, 评分38~72 ──
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

                if w not in [1, 2, 4]:  # 周二/三/五
                    alerts.append(f'涨停 — {weekday_name}非周二/三/五, 跳过')
                else:
                    for rank, (_, row) in enumerate(df.iterrows(), 1):
                        if rank > lu_top_n: break
                        code = str(row.get('代码', '')).strip().zfill(6)
                        name = str(row.get('名称', ''))
                        score = float(row.get(col, 0))

                        if score > 72:
                            alerts.append(f'涨停 {name}({code}) 评分{score:.0f} — Q4陷阱(>72), 跳过')
                            continue
                        if score < lu_min:
                            alerts.append(f'涨停 {name}({code}) 评分{score:.0f} — 偏低(<{lu_min:.0f}), 跳过')
                            continue

                        signals.append({
                            'tab': 'limit-up', 'tab_cn': '涨停板', 'strategy': 'A开盘买',
                            'code': code, 'name': name, 'score': round(score, 1), 'rank': rank,
                            'signal_date': today_str, 'buy_date': next_td,
                            'reason': f'周二/三/五+top{lu_top_n}+{lu_min:.0f}~72 (rank{rank})',
                        })
    except Exception as e:
        alerts.append(f'涨停扫描: {e}')

    # ── 趋势信号: 周一/二, top-1, 评分45~69 ──
    try:
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
        if pool is not None and not pool.empty and zb_codes:
            code_col_t = next((c for c in pool.columns if '代码' in str(c)), None)
            if code_col_t:
                pool = pool[~pool[code_col_t].astype(str).str.strip().str.zfill(6).isin(zb_codes)]
        if pool is not None and not (hasattr(pool, 'empty') and pool.empty):
            df = score_fn(pool, today_str)
            if df is not None and not (hasattr(df, 'empty') and df.empty):
                col = score_col if score_col in df.columns else df.columns[-1]
                df = df.sort_values(col, ascending=False)

                if w not in [0, 1]:  # 周一/二
                    alerts.append(f'趋势 — {weekday_name}非周一/二, 跳过')
                else:
                    for rank, (_, row) in enumerate(df.iterrows(), 1):
                        if rank > 1: break  # top-1
                        code = str(row.get('代码', '')).strip().zfill(6)
                        name = str(row.get('名称', ''))
                        score = float(row.get(col, 0))

                        if score > 69:
                            alerts.append(f'趋势 {name}({code}) 评分{score:.0f} — Q4陷阱(>69), 跳过')
                            break
                        if score < 45:
                            alerts.append(f'趋势 {name}({code}) 评分{score:.0f} — 偏低(<45), 跳过')
                            break

                        signals.append({
                            'tab': 'trend', 'tab_cn': '趋势股', 'strategy': 'A开盘买',
                            'code': code, 'name': name, 'score': round(score, 1), 'rank': rank,
                            'signal_date': today_str, 'buy_date': next_td,
                            'reason': '周一/二+rank1+45~69(历史EV+4.86%)',
                        })
    except Exception as e:
        alerts.append(f'趋势扫描: {e}')

    # ── P8 炸板信号: 任意交易日, top-3, 评分≥50, T+5持仓 ──
    try:
        from backtest_engine import SIGNAL_POOL_FETCHERS, SCORE_FUNCS, SCORE_COLUMNS, TAB_ZHABAN
        fetcher = SIGNAL_POOL_FETCHERS[TAB_ZHABAN]
        score_fn = SCORE_FUNCS[TAB_ZHABAN]
        score_col = SCORE_COLUMNS[TAB_ZHABAN]

        pool = fetcher(today_str)
        if pool is not None and not (hasattr(pool, 'empty') and pool.empty):
            df = score_fn(pool, today_str)
            if df is not None and not (hasattr(df, 'empty') and df.empty):
                col = score_col if score_col in df.columns else df.columns[-1]
                df = df.sort_values(col, ascending=False)

                for rank, (_, row) in enumerate(df.iterrows(), 1):
                    if rank > zb_top_n: break
                    code = str(row.get('代码', '')).strip().zfill(6)
                    name = str(row.get('名称', ''))
                    score = float(row.get(col, 0))

                    if score < zb_min:
                        alerts.append(f'炸板 {name}({code}) 评分{score:.0f} — 未达{zb_min:.0f}门槛, 跳过')
                        continue

                    signals.append({
                        'tab': 'zhaban', 'tab_cn': '炸板反包', 'strategy': f'A开盘买+{_seller}',
                        'code': code, 'name': name, 'score': round(score, 1), 'rank': rank,
                        'signal_date': today_str, 'buy_date': next_td,
                        'reason': f'min{zb_min:.0f}+top{rank} (回测56.2%胜率+15568)',
                    })
    except Exception as e:
        alerts.append(f'炸板扫描: {e}')

    # ── 可转债/风险事件过滤 ──
    try:
        warnings = _get_warning_codes(today_str)
        if warnings:
            filtered = []
            for s in signals:
                if s['code'] in warnings:
                    alerts.append(f'{s["tab_cn"]} {s["name"]}({s["code"]}) — {warnings[s["code"]]}, 抛压风险排除')
                else:
                    filtered.append(s)
            signals = filtered
    except Exception as e:
        alerts.append(f'风险检查异常: {e}')

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
