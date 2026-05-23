#!/usr/bin/env python3
"""
数据持久化 + 每日/周总结模块
"""
import json
import os
from datetime import date, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SUMMARY_FILE = os.path.join(DATA_DIR, "daily_summary.md")

os.makedirs(DATA_DIR, exist_ok=True)

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _today_path():
    return os.path.join(DATA_DIR, f"{date.today().isoformat()}.json")


def save_daily_data(data: dict) -> str:
    """保存今日数据到 YYYY-MM-DD.json"""
    ensure_data_dir()
    path = _today_path()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=None, separators=(',', ':'))
    return path


def load_previous_days(n_days=5) -> list:
    """加载最近 N 天历史数据"""
    today = date.today()
    history = []
    for i in range(1, n_days + 1):
        d = today - timedelta(days=i)
        path = os.path.join(DATA_DIR, f"{d.isoformat()}.json")
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                data['_date'] = d.isoformat()
                history.append(data)
    return history


def build_weekly_aggregate() -> str:
    """构建并写入本周聚合文件"""
    ensure_data_dir()
    today = date.today()
    iso = today.isocalendar()
    week_key = f"{iso[0]}_W{iso[1]:02d}"
    weekly_path = os.path.join(DATA_DIR, f"weekly_{week_key}.json")
    week_start = today - timedelta(days=today.weekday())
    week_data = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        p = os.path.join(DATA_DIR, f"{d.isoformat()}.json")
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                week_data.append(json.load(f))
    aggregate = {
        'week': week_key,
        'generated_at': today.isoformat(),
        'days': len(week_data),
        'daily': week_data,
    }
    with open(weekly_path, 'w', encoding='utf-8') as f:
        json.dump(aggregate, f, ensure_ascii=False, indent=2)
    return weekly_path


def _grade_from_stock(s):
    """按原始评分标准定级（参考 SKILL.md 分级规则）"""
    score = s.get('total_score')
    money = s.get('money_score') or 0
    sector = s.get('sector_score') or 0
    if score is None:
        return 'C'
    if score >= 70 and money >= 20 and sector >= 15:
        return 'S'
    if score >= 55 and money + sector >= 20:
        return 'A'
    if score >= 45:
        return 'B+'
    return 'C'


def generate_summary(today_data: dict, history: list) -> str:
    """生成每日总结文本"""
    stocks = today_data.get('stocks', [])
    if not stocks:
        return f"# 每日总结 — {date.today().isoformat()}\n\n今日无数据\n"

    s_c = sum(1 for s in stocks if _grade_from_stock(s) == 'S')
    a_c = sum(1 for s in stocks if _grade_from_stock(s) == 'A')
    bp_c = sum(1 for s in stocks if _grade_from_stock(s) == 'B+')
    top5 = [s.get('name', '') for s in stocks[:5]]

    lines = [
        f"# 每日总结 — {date.today().isoformat()}",
        "",
        f"**概览**: {len(stocks)} 只标的 | S级 {s_c} | A级 {a_c} | B+级 {bp_c}",
        f"**TOP5**: {' / '.join(top5)}",
    ]

    if history:
        # 连续上榜
        prev_codes = set()
        for h in history:
            for s in h.get('stocks', []):
                if s.get('code'):
                    prev_codes.add(s['code'])
        cur_codes = {s.get('code', '') for s in stocks}
        repeat = cur_codes & prev_codes
        if repeat:
            lines.append("\n### 连续上榜")
            for code in sorted(repeat)[:5]:
                s = next((x for x in stocks if x.get('code') == code), None)
                if s:
                    lines.append(f"- {s.get('name','')}({code}) 总分 {s.get('total_score','?')}")

        # 评分趋势
        score_series = []
        for h in history[-3:]:
            scores = [
                s.get('total_score', 0) for s in h.get('stocks', [])
                if s.get('total_score') is not None
            ]
            score_series.append(round(sum(scores) / len(scores), 1) if scores else 0)
        if len(score_series) >= 2:
            trend = "上升 ↗" if score_series[-1] > score_series[0] \
                    else "下降 ↘" if score_series[-1] < score_series[0] \
                    else "持平 →"
            series_str = " → ".join(str(s) for s in score_series)
            lines.append(f"\n**评分趋势**: {series_str}，{trend}")

    lines.append("")
    return "\n".join(lines)


def write_summary(text: str) -> str:
    ensure_data_dir()
    with open(SUMMARY_FILE, 'w', encoding='utf-8') as f:
        f.write(text)
    return SUMMARY_FILE


def run(today_data: dict) -> tuple:
    """主入口：保存+总结+周聚合，返回(总结文本, 路径信息)"""
    ensure_data_dir()
    sp = save_daily_data(today_data)
    hist = load_previous_days(n_days=5)
    summary = generate_summary(today_data, hist)
    smp = write_summary(summary)
    wp = build_weekly_aggregate()
    return summary, {
        'data_path': sp,
        'summary_path': smp,
        'weekly_path': wp,
    }
