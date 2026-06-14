"""明日买入信号 — 基于回测数据挖掘的实盘决策引擎

使用盘前可知信息 (rank, score, 星期), 不含 gap 等未来数据。

用法:
    python signal_tomorrow.py            # CLI 输出明日信号
    python signal_tomorrow.py --json     # JSON 输出
"""

import sys
from datetime import datetime, timedelta

sys.path.insert(0, '.')

from cache import _trading_date, _is_trading_day, _next_trading_date_impl

# ═══════════════════════════════════════════
#  信号规则
# ═══════════════════════════════════════════

def _next_td(d):
    """次日交易日."""
    cur = datetime.strptime(d, '%Y%m%d')
    for _ in range(10):
        cur += timedelta(days=1)
        s = cur.strftime('%Y%m%d')
        if _is_trading_day(s):
            return s
    return None


def check_weekday(signal_date: str, allowed_days: set) -> bool:
    """检查信号日是否在允许的星期."""
    if len(signal_date) != 8:
        return True  # 无法判断时放行
    w = datetime.strptime(signal_date, '%Y%m%d').weekday()
    return w in allowed_days


def check_score(signal: dict, tab: str) -> tuple:
    """检查评分区间, 返回 (通过, 原因)."""
    score = signal.get('score', 0)
    if tab == 'limit-up':
        if score > 74:
            return False, f'Q4陷阱(评分{score:.0f}>74,历史EV-0.88%)'
        if score < 38:
            return False, f'评分偏低({score:.0f}<38)'
        return True, ''
    elif tab == 'trend':
        if score > 69:
            return False, f'Q4陷阱(评分{score:.0f}>69)'
        if score < 7:
            return False, f'评分过低({score:.0f}<7)'
        return True, ''
    return True, ''


def generate_signals(today_str: str = None) -> dict:
    """生成明日买入信号.

    Returns:
        { 'date': str, 'next_trade_day': str, 'signals': [...], 'summary': str }
    """
    if today_str is None:
        today_str = _trading_date().replace('-', '')

    next_td = _next_td(today_str)

    # 明天是否交易日
    if next_td is None:
        return {'date': today_str, 'next_trade_day': None,
                'signals': [], 'summary': '明日非交易日, 无信号'}

    # 信号日星期
    w = datetime.strptime(today_str, '%Y%m%d').weekday()
    weekday_name = ['周一','周二','周三','周四','周五','周六','周日'][w]

    signals = []
    alerts = []

    # ── 涨停板信号 ──
    try:
        from scanner import _scan_limit_up_data
        pool, scoring_base = _scan_limit_up_data(today_str)
        if pool is not None and not (hasattr(pool, 'empty') and pool.empty):
            from plans.plan_a import apply_scores
            df = apply_scores(pool, today_str)
            if df is not None and not df.empty:
                # 按评分排序
                score_col = None
                for c in ['综合分', '总分', '评分']:
                    if c in df.columns:
                        score_col = c
                        break
                if score_col:
                    df = df.sort_values(score_col, ascending=False)

                for rank, (_, row) in enumerate(df.iterrows(), 1):
                    code = str(row.get('代码', '')).strip().zfill(6)
                    name = str(row.get('名称', ''))
                    score = float(row.get(score_col, 0))

                    # 规则1: rank=1 only
                    if rank > 1:
                        break

                    # 规则2: 星期过滤 (周二/五)
                    if w not in [1, 4]:
                        alerts.append(f'涨停 {name}({code}) rank=1 评分{score:.0f} — 今日{weekday_name}, 非周二/五, 跳过')
                        break

                    # 规则3: 评分区间
                    passed, reason = check_score({'score': score}, 'limit-up')
                    if not passed:
                        alerts.append(f'涨停 {name}({code}) rank=1 评分{score:.0f} — {reason}')
                        break

                    # 通过!
                    signals.append({
                        'tab': 'limit-up',
                        'tab_cn': '涨停板',
                        'strategy': 'A开盘买',
                        'code': code,
                        'name': name,
                        'score': round(score, 1),
                        'rank': rank,
                        'signal_date': today_str,
                        'buy_date': next_td,
                        'reason': f'周二/五+rank1+评分甜点(历史EV+2.12%)',
                    })
    except Exception as e:
        alerts.append(f'涨停扫描异常: {e}')

    # ── 趋势信号 ──
    try:
        from scanner import _score_trend
        # 趋势扫描需要特殊处理 — 直接调评分函数
        import akshare as ak
        strong_df = ak.stock_zt_pool_strong_em(date=today_str)
        if strong_df is not None and not strong_df.empty:
            from scanner import filter_non_main_board
            strong_df = filter_non_main_board(strong_df)
            if not strong_df.empty:
                scored = _score_trend(strong_df, today_str)
                if scored is not None and not scored.empty:
                    score_col_t = '动量评分'
                    if score_col_t in scored.columns:
                        scored = scored.sort_values(score_col_t, ascending=False)

                    for rank, (_, row) in enumerate(scored.iterrows(), 1):
                        code = str(row.get('代码', '')).strip().zfill(6)
                        name = str(row.get('名称', ''))
                        score = float(row.get(score_col_t, 0))

                        if rank > 1:
                            break

                        # 星期过滤 (周一/二)
                        if w not in [0, 1]:
                            alerts.append(f'趋势 {name}({code}) rank=1 评分{score:.0f} — 今日{weekday_name}, 非周一/二, 跳过')
                            break

                        passed, reason = check_score({'score': score}, 'trend')
                        if not passed:
                            alerts.append(f'趋势 {name}({code}) rank=1 评分{score:.0f} — {reason}')
                            break

                        signals.append({
                            'tab': 'trend',
                            'tab_cn': '趋势股',
                            'strategy': 'A开盘买',
                            'code': code,
                            'name': name,
                            'score': round(score, 1),
                            'rank': rank,
                            'signal_date': today_str,
                            'buy_date': next_td,
                            'reason': f'周一/二+rank1(历史EV+3.76%)',
                        })
    except Exception as e:
        alerts.append(f'趋势扫描异常: {e}')

    # ── 汇总 ──
    if signals:
        names = [s['name'] for s in signals]
        summary = f'{weekday_name}信号: {len(signals)}只 — 明日({next_td})开盘买入: {", ".join(names)}'
    else:
        summary = f'{weekday_name} — 今日无符合规则的买入信号'

    return {
        'date': today_str,
        'weekday': weekday_name,
        'next_trade_day': next_td,
        'signals': signals,
        'alerts': alerts,
        'summary': summary,
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
