"""推荐追踪系统

记录每个 tab（涨停/趋势/炸板/翘板/板块等）今日推荐的票，
次日自动拉取开盘价，统计各 tab 的次日胜率。

用法:
  from recommendation_tracker import save_recommendations, get_per_tab_stats
  save_recommendations('limit-up', items, today_str)   # 保存推荐
  stats = get_per_tab_stats()                           # 获取各 tab 汇总
"""
import json
import os
import sys
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recommendations")
PERF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tracker_perf.json")

# 定义各 tab 的中文名
TAB_NAMES = {
    'limit-up': '涨停扫描',
    'trend': '趋势扫描',
    'zhaban': '炸板反包',
    'dtqiaoban': '跌停翘板',
    'sector': '板块联动',
    'reversal': '涨停回调',
    'indicators': '龙虎榜',
    'community': '舆情监测',
}


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _date_path(date_str):
    return os.path.join(DATA_DIR, f"{date_str}.json")


def save_recommendations(tab: str, stocks: list, date_str: str = None):
    """保存某个 tab 今日的推荐列表

    tab: limit-up / trend / zhaban / dtqiaoban / sector / ...
    stocks: [{'code':..., 'name':..., 'score':...}, ...]
    date_str: YYYYMMDD, 默认今天
    """
    if not stocks or not tab:
        return
    if date_str is None:
        date_str = datetime.now().strftime('%Y%m%d')
    _ensure_dir()
    path = _date_path(date_str)
    data = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    # 只保留必要的字段
    entries = []
    for i, s in enumerate(stocks):
        code = str(s.get('code', '') or '').strip().zfill(6)
        name = str(s.get('name', '') or '')
        score = s.get('score', s.get('total_score', 0))
        if not code or not name:
            continue
        entries.append({'code': code, 'name': name, 'score': int(score) if score else 0, 'rank': i + 1})
    if entries:
        data[tab] = entries
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=None, separators=(',', ':'))
        return len(entries)
    return 0


def _calc_performance(code, buy_date_str):
    """计算单只股票在某日的开盘表现，返回 {win, change_pct, open, close}

    使用东方财富 API 拉取当日数据。
    如果买不到（一字涨停开盘），标记为 'unbuyable'。
    """
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_zh_a_hist(symbol=code, period='daily',
                                 start_date=buy_date_str, end_date=buy_date_str, adjust='')
        if df is None or df.empty:
            return None
        row = df.iloc[0]
        open_px = float(row['开盘'])
        close_px = float(row['收盘'])
        change_pct = float(row['涨跌幅'])
        prev_close = round(close_px / (1 + change_pct / 100), 2) if change_pct != 0 else open_px
        gap_pct = round((open_px / prev_close - 1) * 100, 1)
        return {
            'win': change_pct > 0,
            'win_open': open_px > prev_close,  # 开盘即涨
            'change_pct': round(change_pct, 2),
            'open': round(open_px, 2),
            'close': round(close_px, 2),
            'gap_open_pct': gap_pct,
            'buyable': gap_pct < 9.5,
        }
    except Exception as e:
        print(f"  [tracker perf] {code} {buy_date_str}: {e}", file=sys.stderr)
        return None


def _refresh_performance():
    """扫描所有未计算表现的推荐日期，补充次日表现

    对每一条推荐记录，拉取次日开盘/收盘数据，计算盈亏。
    结果写入 tracker_perf.json。
    """
    _ensure_dir()
    # 加载已有表现
    perf = {}
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE, 'r', encoding='utf-8') as f:
                perf = json.load(f)
        except Exception:
            perf = {}

    today = datetime.now().strftime('%Y%m%d')
    updated = False

    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith('.json') or fname.startswith('.'):
            continue
        rec_date = fname.replace('.json', '')
        if not rec_date.isdigit() or len(rec_date) != 8:
            continue
        # 日期 key 已存在且已有表现数据 → 跳过
        if rec_date in perf:
            continue
        # 记录日期 >= 今天 → 还没到次日，跳过
        if rec_date >= today:
            continue

        try:
            with open(os.path.join(DATA_DIR, fname), 'r', encoding='utf-8') as f:
                day_data = json.load(f)
        except Exception:
            continue

        # 计算下一交易日
        next_day = _next_trading_day(rec_date)
        if next_day is None or next_day >= today:
            continue

        day_perf = {}
        for tab, stocks in day_data.items():
            tab_results = []
            for s in stocks:
                code = s.get('code', '')
                result = _calc_performance(code, next_day)
                if result is None:
                    continue
                tab_results.append({**s, **result})
            if tab_results:
                wins = sum(1 for r in tab_results if r['win'])
                win_open = sum(1 for r in tab_results if r['win_open'])
                buyable = sum(1 for r in tab_results if r['buyable'])
                day_perf[tab] = {
                    'count': len(tab_results),
                    'wins': wins,
                    'win_open': win_open,
                    'buyable': buyable,
                    'win_rate': round(wins / len(tab_results) * 100, 1),
                    'details': tab_results,
                }
        if day_perf:
            perf[rec_date] = day_perf
            updated = True

    if updated:
        with open(PERF_FILE, 'w', encoding='utf-8') as f:
            json.dump(perf, f, ensure_ascii=False, indent=2)

    return perf


def get_per_tab_stats():
    """读取所有已计算的追踪表现，按 tab 聚合

    返回 {tab: {count, wins, win_rate}, ...}
    """
    _refresh_performance()
    if not os.path.exists(PERF_FILE):
        return {}
    try:
        with open(PERF_FILE, 'r', encoding='utf-8') as f:
            perf = json.load(f)
    except Exception:
        return {}

    agg = {}
    days_per_tab = {}
    rank_agg = {}  # {tab: {rank: {count, wins}}}

    for date_str, day_data in perf.items():
        for tab, tab_data in day_data.items():
            if tab not in agg:
                agg[tab] = {'count': 0, 'wins': 0, 'win_open': 0, 'buyable': 0}
                days_per_tab[tab] = set()
            agg[tab]['count'] += tab_data['count']
            agg[tab]['wins'] += tab_data['wins']
            agg[tab]['win_open'] += tab_data['win_open']
            agg[tab]['buyable'] += tab_data['buyable']
            days_per_tab[tab].add(date_str)

            # 按排名聚合
            for detail in tab_data.get('details', []):
                r = detail.get('rank', 0)
                if r < 1 or r > 5:
                    continue
                if tab not in rank_agg:
                    rank_agg[tab] = {}
                if r not in rank_agg[tab]:
                    rank_agg[tab][r] = {'count': 0, 'wins': 0}
                rank_agg[tab][r]['count'] += 1
                if detail.get('win'):
                    rank_agg[tab][r]['wins'] += 1

    result = []
    for tab, data in sorted(agg.items()):
        result.append({
            'tab': tab,
            'label': TAB_NAMES.get(tab, tab),
            'count': data['count'],
            'wins': data['wins'],
            'win_rate': round(data['wins'] / data['count'] * 100, 1) if data['count'] > 0 else 0,
            'win_open_rate': round(data['win_open'] / data['count'] * 100, 1) if data['count'] > 0 else 0,
            'buyable': data['buyable'],
            'days_count': len(days_per_tab.get(tab, set())),
            'rank_stats': rank_agg.get(tab, {}),
        })
    # 按 win_rate 降序
    result.sort(key=lambda x: -x['win_rate'])
    return result


def get_daily_tracker(date_str: str = None):
    """获取某天的追踪详情

    返回 {tab: {count, wins, win_rate, details: [{code, name, score, win, change_pct}]}, ...}
    """
    if date_str is None:
        date_str = datetime.now().strftime('%Y%m%d')
    if not os.path.exists(PERF_FILE):
        return {}
    try:
        with open(PERF_FILE, 'r', encoding='utf-8') as f:
            perf = json.load(f)
    except Exception:
        return {}
    return perf.get(date_str, {})


def _next_trading_day(d_str):
    """d 的下一个交易日"""
    from cache import _is_trading_day
    cur = datetime.strptime(d_str, '%Y%m%d')
    for _ in range(10):
        cur += timedelta(days=1)
        c = cur.strftime('%Y%m%d')
        if _is_trading_day(c):
            return c
    return None
