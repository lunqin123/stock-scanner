"""多方案回测对比框架 — 基于 archive.db 真实 next_day_change

对每个候选策略:
  1. 加载 daily_stocks 池
  2. 按 trade_date 分组
  3. 每天: 应用硬过滤 + 排序 + 取 top_n
  4. 汇总: 胜率 / EV / PLR / 6月 / 7月 / Sharpe / 最大回撤

输出: 多方案对比表 (含基线对比)
"""
from __future__ import annotations
import sqlite3
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple
from strategy_filters_v2 import (
    load_daily_pool,
    default_hard_filters,
    strict_hard_filters,
    default_score_adjuster,
    conservative_score_adjuster,
    industry_tier,
    is_seal_afternoon,
    f_min_consecutive,
    row_to_dict,
)


# ═══════════════════════════════════════════
#  单策略回测
# ═══════════════════════════════════════════

def backtest_strategy(
    data: List[dict],
    hard_filters: Optional[List[Callable[[dict], bool]]] = None,
    hard_filters_union: Optional[List[List[Callable[[dict], bool]]]] = None,
    score_adjuster: Optional[Callable[[dict], float]] = None,
    top_n: int = 3,
    base_score_fn: Optional[Callable[[dict], float]] = None,
) -> Dict:
    """跑一次策略回测

    Args:
        data: 全部 daily_stocks (从 archive.db 加载)
        hard_filters: 硬过滤函数列表 (None = 不过滤, 基线)
        hard_filters_union: 多组过滤规则取并集 (S8 用: S4 ∪ S6)
        score_adjuster: 软加权系数函数 (返回系数, 默认 1.0)
        top_n: 每天取 top_n 票
        base_score_fn: 基础评分函数 (默认用 change_pct)

    Returns:
        {
          'trades': [(date, code, next_day_change, ...)],
          'summary': {n, win_rate, avg_ret, plr, ...},
          'by_month': {'202606': {...}, '202607': {...}},
          'sharpe_like': ...,
          'max_dd': ...,
        }
    """
    if hard_filters is None:
        hard_filters = []
    if score_adjuster is None:
        score_adjuster = lambda t: 1.0
    if base_score_fn is None:
        # 默认评分: change_pct 高的优先 (涨停幅度大)
        base_score_fn = lambda t: t.get('change_pct') or 0

    # 按日期分组
    by_date = defaultdict(list)
    for t in data:
        by_date[t['trade_date']].append(t)

    trades = []
    for date, pool in sorted(by_date.items()):
        # 1. 硬过滤 (单组 or 并集)
        if hard_filters_union:
            # 候选池 = 任一组规则通过即可
            candidates_set = set()
            for filt_group in hard_filters_union:
                for t in pool:
                    if all(f(t) for f in filt_group):
                        candidates_set.add(id(t))
            filtered = [t for t in pool if id(t) in candidates_set]
        else:
            filtered = pool
            for f in hard_filters:
                filtered = [t for t in filtered if f(t)]
        if not filtered:
            continue

        # 2. 计算 final_score = base_score * adjuster
        for t in filtered:
            t['_final_score'] = base_score_fn(t) * score_adjuster(t)

        # 3. 排序取 top_n
        filtered.sort(key=lambda t: t['_final_score'], reverse=True)
        picks = filtered[:top_n]

        # 4. 记录
        for t in picks:
            trades.append({
                'date': t['trade_date'],
                'code': t['code'],
                'name': t['name'],
                'next': t['next_day_change'],
                'next_open': t.get('next_day_open_change'),
                'industry': t.get('industry'),
                'cons': t.get('consecutive'),
                'turnover': t.get('turnover'),
                'seal_fund': t.get('seal_fund'),
                'seal_time': t.get('seal_time'),
            })

    # 汇总
    return _summarize(trades)


def _summarize(trades: List[dict]) -> Dict:
    """计算汇总指标"""
    if not trades:
        return {
            'n': 0, 'win_rate': 0, 'avg_ret': 0, 'plr': 0,
            'sharpe_like': 0, 'max_dd': 0, 'cumulative_ret': 0,
            'by_month': {}, 'trades': trades,
        }

    n = len(trades)
    rets = [t['next'] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]

    win_rate = len(wins) / n * 100
    avg_ret = sum(rets) / n
    win_avg = sum(wins) / len(wins) if wins else 0
    loss_avg = sum(losses) / len(losses) if losses else 0
    plr = abs(win_avg / loss_avg) if loss_avg else float('inf')

    # Sharpe-like (avg / std) - 假设年化系数 1
    if n > 1:
        import statistics
        std = statistics.stdev(rets)
        sharpe = (avg_ret / std) if std > 0 else 0
    else:
        sharpe = 0

    # 最大回撤 (按日期累计)
    by_date = defaultdict(list)
    for t in trades:
        by_date[t['date']].append(t)

    cum = 0
    peak = 0
    max_dd = 0
    sorted_dates = sorted(by_date.keys())
    for d in sorted_dates:
        # 当日等权
        day_avg = sum(t['next'] for t in by_date[d]) / len(by_date[d])
        cum += day_avg
        peak = max(peak, cum)
        dd = cum - peak
        max_dd = min(max_dd, dd)

    # 累计收益 (等权日均)
    cumulative_ret = sum(rets) / n  # 简化为等权总收益 (不是真实复利)

    # 按月汇总
    by_month = defaultdict(list)
    for t in trades:
        ym = t['date'][:6]
        by_month[ym].append(t['next'])
    month_summary = {}
    for ym, rs in by_month.items():
        mn = len(rs)
        mw = sum(1 for r in rs if r > 0)
        month_summary[ym] = {
            'n': mn, 'win_rate': mw/mn*100, 'avg_ret': sum(rs)/mn,
            'wins': mw, 'losses': mn-mw,
        }

    return {
        'n': n,
        'win_rate': win_rate,
        'avg_ret': avg_ret,
        'plr': plr,
        'sharpe_like': sharpe,
        'max_dd': max_dd,
        'cumulative_ret': cumulative_ret,
        'by_month': month_summary,
        'trades': trades,
    }


# ═══════════════════════════════════════════
#  候选策略定义
# ═══════════════════════════════════════════

def make_candidates(data: List[dict], top_n: int = 3) -> List[Tuple[str, Dict]]:
    """生成 8 个候选策略

    返回 [(name, {hard_filters, score_adjuster, ...}), ...]
    """
    candidates = []

    # S0: 基线 (无硬过滤, 无加权, 按 change_pct 排序)
    candidates.append(('S0 基线 (无过滤)', {
        'hard_filters': [],
        'score_adjuster': lambda t: 1.0,
        'top_n': top_n,
        'desc': '所有涨停池, change_pct 排序取 top_n',
    }))

    # S1: 硬过滤 (4 条硬规则)
    candidates.append(('S1 硬过滤', {
        'hard_filters': default_hard_filters(),
        'score_adjuster': lambda t: 1.0,
        'top_n': top_n,
        'desc': '换手 0.5-15% + 封单 0.5-10亿 + 非尾盘 + 非bottom行业',
    }))

    # S2: 硬过滤 + 连板加权
    candidates.append(('S2 硬过滤+连板加权', {
        'hard_filters': default_hard_filters(),
        'score_adjuster': default_score_adjuster,
        'top_n': top_n,
        'desc': 'S1 + 连板/行业/封板时间/换手 软加权',
    }))

    # S3: 严格硬过滤 (含连板>=2)
    candidates.append(('S3 严格(连板>=2)', {
        'hard_filters': strict_hard_filters(),
        'score_adjuster': lambda t: 1.0,
        'top_n': top_n,
        'desc': 'S1 + 连板 >= 2',
    }))

    # S4: S1 + 只取行业=化学制药 (信号源)
    candidates.append(('S4 行业=top8', {
        'hard_filters': default_hard_filters() + [
            lambda t: industry_tier(t.get('industry')) == 'top',
        ],
        'score_adjuster': lambda t: 1.0,
        'top_n': top_n,
        'desc': 'S1 + 只保留 top8 行业',
    }))

    # S5: S1 + 软加权 (连板+行业+时间+换手)
    candidates.append(('S5 S1+全维度软加权', {
        'hard_filters': default_hard_filters(),
        'score_adjuster': default_score_adjuster,
        'top_n': top_n,
        'desc': 'S1 + 连板×1.3/1.5 + 行业×1.2 + 上午×1.1 + 低换手×1.05',
    }))

    # S6: 极简 — 硬过滤 + 上午封板 + 连板>=2 + 换手<5%
    candidates.append(('S6 极简硬过滤', {
        'hard_filters': default_hard_filters() + [
            lambda t: (t.get('consecutive') or 0) >= 2,
            lambda t: (t.get('turnover') or 100) < 5,
        ],
        'score_adjuster': lambda t: 1.0,
        'top_n': top_n,
        'desc': 'S1 + 连板≥2 + 换手<5% (即"早盘+连板≥2+换手<5%")',
    }))

    # S7: 7月自动切保守版 (sentiment 状态开关模拟)
    # 7月数据平均 sentiment 偏弱, 用更严的过滤
    candidates.append(('S7 7月保守切换', {
        'hard_filters': default_hard_filters() + [
            lambda t: (t.get('consecutive') or 0) >= 2,
        ],
        'score_adjuster': conservative_score_adjuster,
        'top_n': top_n,
        'desc': 'S1 + 连板≥2 + 保守加权 (模拟 sentiment 弱时切换)',
    }))

    # S8: S4 + S6 组合 (候选池合并去重, 取 top_n)
    s4_filters = default_hard_filters() + [
        lambda t: industry_tier(t.get('industry')) == 'top',
    ]
    s6_filters = default_hard_filters() + [
        lambda t: (t.get('consecutive') or 0) >= 2,
        lambda t: (t.get('turnover') or 100) < 5,
    ]
    candidates.append(('S8 S4+S6 组合', {
        'hard_filters_union': [s4_filters, s6_filters],  # 特殊: 多组规则取并集
        'score_adjuster': default_score_adjuster,
        'top_n': top_n,
        'desc': 'S4 ∪ S6 候选池合并 (任一通过即可)',
    }))

    # S9: S6 + 行业 top 加权 (在 S6 子集上, 行业额外加分)
    def s9_adjuster(t: dict) -> float:
        base = default_score_adjuster(t)
        if industry_tier(t.get('industry')) == 'top':
            base *= 1.2
        return base
    candidates.append(('S9 S6+行业加权', {
        'hard_filters': s6_filters,
        'score_adjuster': s9_adjuster,
        'top_n': top_n,
        'desc': 'S6 + 行业=top × 1.2 额外加权',
    }))

    # S10: S6 改良 — 换手放宽到 < 8% (笔数与稳健的折中)
    s10_filters = default_hard_filters() + [
        lambda t: (t.get('consecutive') or 0) >= 2,
        lambda t: (t.get('turnover') or 100) < 8,
    ]
    candidates.append(('S10 S6换手<8%', {
        'hard_filters': s10_filters,
        'score_adjuster': default_score_adjuster,
        'top_n': top_n,
        'desc': 'S6 + 换手阈值放宽到 8% (找稳健/笔数平衡)',
    }))

    # S11: S9 + 行业加权 同样应用到 S8 候选池 (实盘兜底版)
    s4_filters = default_hard_filters() + [
        lambda t: industry_tier(t.get('industry')) == 'top',
    ]
    s6_filters = default_hard_filters() + [
        lambda t: (t.get('consecutive') or 0) >= 2,
        lambda t: (t.get('turnover') or 100) < 5,
    ]
    def s11_adjuster(t: dict) -> float:
        base = default_score_adjuster(t)
        if industry_tier(t.get('industry')) == 'top':
            base *= 1.2
        return base
    candidates.append(('S11 S8+行业加权(兜底版)', {
        'hard_filters_union': [s4_filters, s6_filters],  # S4 ∪ S6
        'score_adjuster': s11_adjuster,
        'top_n': top_n,
        'desc': 'S4 ∪ S6 候选池 + 行业=top×1.2 加权 (S9 票少时用 S4 兜底)',
    }))

    return candidates


# ═══════════════════════════════════════════
#  对比主入口
# ═══════════════════════════════════════════

def run_comparison(db_path: str, top_n: int = 3) -> List[Tuple[str, Dict, Dict]]:
    """跑所有候选策略, 返回 [(name, config, result), ...]"""
    data = load_daily_pool(db_path)
    print(f'加载 {len(data)} 笔历史样本 (从 {db_path})')

    candidates = make_candidates(data, top_n=top_n)
    results = []
    for name, cfg in candidates:
        kwargs = dict(
            data=data,
            score_adjuster=cfg['score_adjuster'],
            top_n=cfg['top_n'],
        )
        if 'hard_filters_union' in cfg:
            kwargs['hard_filters_union'] = cfg['hard_filters_union']
        else:
            kwargs['hard_filters'] = cfg['hard_filters']
        result = backtest_strategy(**kwargs)
        results.append((name, cfg, result))
    return results


def print_comparison_table(results: List[Tuple[str, Dict, Dict]]):
    """打印对比表"""
    print('\n' + '='*120)
    print(f'{"方案":<25} {"N":>4} {"胜率":>6} {"EV":>7} {"PLR":>5} {"Sharpe":>7} {"MaxDD":>7} {"6月":>7} {"7月":>7} {"稳健":>4}')
    print('='*120)
    for name, cfg, r in results:
        june = r['by_month'].get('202606', {}).get('avg_ret', 0)
        july = r['by_month'].get('202607', {}).get('avg_ret', 0)
        stable = '✓' if (june > 0 and july > 0 and r['win_rate'] >= 60 and r['n'] >= 30) else ''
        print(f"{name:<25} {r['n']:>4} {r['win_rate']:>5.1f}% {r['avg_ret']:>+6.2f}% "
              f"{r['plr']:>5.2f} {r['sharpe_like']:>6.2f} {r['max_dd']:>+6.2f}% "
              f"{june:>+6.2f}% {july:>+6.2f}% {stable:>4}")
    print('='*120)
    print('说明: 稳健 = 6月+7月都为正 + 胜率 >= 60% + N >= 30')


def export_trades_csv(results: List[Tuple[str, Dict, Dict]], out_path: str):
    """导出每个策略的具体交易到 CSV (供人工检查)"""
    import csv
    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['strategy', 'date', 'code', 'name', 'industry', 'cons', 'turnover',
                    'seal_time', 'seal_fund', 'next_change', 'next_open_change'])
        for name, cfg, r in results:
            for t in r['trades']:
                w.writerow([name, t['date'], t['code'], t['name'], t.get('industry', ''),
                            t.get('cons', ''), t.get('turnover', ''), t.get('seal_time', ''),
                            t.get('seal_fund', ''), t.get('next', ''), t.get('next_open', '')])


# ═══════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\16689\Desktop\stock-scanner\data\archive.db'
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    results = run_comparison(db_path, top_n=top_n)
    print_comparison_table(results)

    # 导出详细交易
    out = r'C:\Users\16689\Desktop\stock-scanner\data\strategy_comparison.csv'
    export_trades_csv(results, out)
    print(f'\n详细交易已导出: {out}')
