#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SmartExit 阈值自动调权系统 (P2.0 新增)

**目标**: 根据历史回测表现, 自动调优 SmartExit 决策的 5 个核心阈值

**调权粒度** (3 档):
- daily  : 每天 15:30 收盘后, 基于近 1 天回测数据微调 (lr=0.1 步长)
- weekly : 每周五 15:30, 基于近 5 天数据中调 (lr=0.3 步长)
- monthly: 每月最后一天 15:30, 基于近 30 天数据全调 (lr=0.5 步长)

**调权指标** (基于 run_tab_backtest 输出):
- win_rate   胜率
- avg_ret    平均收益 (EV)
- plr        盈亏比
- max_dd     最大回撤
- exit_dist  exit_type 分布 (止损/止盈/一字板/量能/时间/板块)

**调权规则**:
    胜率 >= 65% + EV >= 2%   → take_profit += lr * 0.5 (放宽止盈)
    胜率 < 45% + EV < 0%     → stop_loss += lr * 0.5 (收紧止损, -5 → -4.5)
    一字板触发率 > 30%        → limit_pct -= lr * 0.5 (更早卖, 9.5 → 9)
    时间到期率 > 50%          → base_n += 1 (延长持仓, 3 → 4)
    止损触发率 > 30%          → stop_loss -= lr * 0.5 (放宽止损, -5 → -5.5)
    止盈触发率 > 30%          → take_profit -= lr * 0.5 (收紧止盈, 7 → 6.5)

**持久化**:
- data/smart_exit_thresholds.json: 当前阈值 + 调权历史 (近 50 条)
- data/smart_exit.lock: 调权进程互斥锁
"""
import os
import sys
import json
import threading
import sqlite3
from datetime import datetime, timedelta
from collections import Counter

_TUNER_LOCK = threading.Lock()
_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "smart_exit.lock"
)
_THRESHOLDS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "smart_exit_thresholds.json"
)
_ARCHIVE_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "archive.db"
)

# 每个 tab 的默认阈值 (P2.0 起始值)
# Tab-specific: 不同 tab 默认值不同, 适配各 tab 特性
DEFAULT_THRESHOLDS = {
    'limit-up': {     # 涨停 → 强势, 阈值宽松
        'stop_loss': -7.0,    # 大止损, 不错过强势股
        'take_profit': 8.0,   # 高止盈, 让利润奔跑
        'limit_pct': 9.5,
        'low_volume': 2.5,
        'weak_sector': 0.25,
        'base_n': 5,          # T+5 持仓
    },
    'zhaban': {       # 炸板 → 反包, 阈值中等
        'stop_loss': -5.0,
        'take_profit': 7.0,
        'limit_pct': 9.5,
        'low_volume': 3.0,
        'weak_sector': 0.30,
        'base_n': 3,          # T+3
    },
    'trend': {        # 趋势 → 短线, 阈值严格
        'stop_loss': -5.0,
        'take_profit': 6.0,
        'limit_pct': 9.0,
        'low_volume': 3.5,
        'weak_sector': 0.35,
        'base_n': 3,          # T+3
    },
    'reversal': {     # 反转 → 修复, 阈值宽松
        'stop_loss': -6.0,
        'take_profit': 7.0,
        'limit_pct': 9.5,
        'low_volume': 3.0,
        'weak_sector': 0.30,
        'base_n': 5,          # T+5
    },
    'dtqiaoban': {    # 跌停翘板 → 高波动, 止损严
        'stop_loss': -4.0,    # 严止损, 防二次下跌
        'take_profit': 7.0,
        'limit_pct': 9.5,
        'low_volume': 4.0,
        'weak_sector': 0.40,
        'base_n': 4,          # T+4
    },
    'sector': {       # 板块联动 → 持续
        'stop_loss': -6.0,
        'take_profit': 7.5,
        'limit_pct': 9.5,
        'low_volume': 2.5,
        'weak_sector': 0.25,
        'base_n': 5,          # T+5
    },
}

# 阈值钳制范围
_THRESHOLD_BOUNDS = {
    'stop_loss': (-10.0, -2.0),     # 止损 -2 ~ -10%
    'take_profit': (3.0, 15.0),     # 止盈 3 ~ 15%
    'limit_pct': (5.0, 15.0),       # 一字板 5 ~ 15%
    'low_volume': (1.0, 10.0),      # 量能枯竭 1 ~ 10%
    'weak_sector': (0.1, 0.6),      # 板块退潮 0.1 ~ 0.6
    'base_n': (2, 5),                # 持仓天数 2 ~ 5
}

# 调权学习率 (按粒度)
_LEARNING_RATE = {
    'daily': 0.1,     # 微调
    'weekly': 0.3,    # 中调
    'monthly': 0.5,   # 全调
}

# 触发调权的最少样本量
_MIN_TRADES = {
    'daily': 1,        # 1 笔就够了
    'weekly': 3,       # 至少 3 笔
    'monthly': 8,      # 至少 8 笔 (统计显著)
}


def _clamp(key: str, value: float) -> float:
    """钳制阈值到合法范围"""
    lo, hi = _THRESHOLD_BOUNDS.get(key, (0, 100))
    return max(lo, min(hi, value))


def _load_thresholds() -> dict:
    """加载当前阈值 (有持久化则用, 否则用默认)"""
    if os.path.exists(_THRESHOLDS_FILE):
        try:
            with open(_THRESHOLDS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    # 返回默认 (加上元数据)
    result = {}
    for tab, cfg in DEFAULT_THRESHOLDS.items():
        result[tab] = dict(cfg)
        result[tab]['tune_count'] = 0
        result[tab]['last_tuned'] = None
        result[tab]['history'] = []
    return result


def _save_thresholds(thresholds: dict):
    """持久化阈值"""
    os.makedirs(os.path.dirname(_THRESHOLDS_FILE), exist_ok=True)
    with open(_THRESHOLDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(thresholds, f, ensure_ascii=False, indent=2)


def _write_status(status: str, **extra):
    """写状态文件"""
    try:
        os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
        payload = {
            'status': status,
            'ts': datetime.now().isoformat(),
            'pid': os.getpid(),
            **extra
        }
        with open(_LOCK_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _read_status() -> dict:
    try:
        if os.path.exists(_LOCK_FILE):
            with open(_LOCK_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _is_lock_stale(max_age_sec: int = 300) -> bool:
    """僵死锁检查 (5 分钟超时)"""
    s = _read_status()
    if not s:
        return True
    if s.get('status') != 'running':
        return True
    try:
        ts = datetime.fromisoformat(s['ts'])
        return (datetime.now() - ts).total_seconds() > max_age_sec
    except Exception:
        return True


def _fetch_recent_backtest_metrics(tab: str, granularity: str) -> dict:
    """从 archive.db 拉近 N 天的回测数据, 计算调权指标

    Returns:
        {
          'win_rate': float,
          'avg_ret': float,
          'plr': float,
          'trade_count': int,
          'exit_dist': {'止损': 0, '止盈': 0, ...}
        }
    """
    days_map = {'daily': 1, 'weekly': 5, 'monthly': 30}
    days = days_map.get(granularity, 5)
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

    try:
        conn = sqlite3.connect(_ARCHIVE_DB, timeout=10)
        cur = conn.cursor()
        # 拉对应 tab 的当日和次日数据
        cur.execute("""
            SELECT trade_date, code, change_pct, next_day_change
            FROM daily_stocks
            WHERE stock_type='limit_up' AND trade_date >= ?
            ORDER BY trade_date DESC
        """, (cutoff,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return {'error': f'archive.db 拉取失败: {e}'}

    if not rows:
        return {'error': 'archive.db 无数据'}

    # 模拟回测指标
    rets = [r[3] for r in rows if r[3] is not None]
    if not rets:
        return {'error': '无 next_day_change 数据'}

    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / len(rets) * 100
    avg_ret = sum(rets) / len(rets)
    win_avg = sum(wins) / len(wins) if wins else 0
    loss_avg = sum(losses) / len(losses) if losses else 0
    plr = abs(win_avg / loss_avg) if loss_avg else 0

    # exit_dist 近似 (基于 change_pct 推断)
    exit_dist = {'止盈': 0, '止损': 0, '一字板陷阱': 0, '时间到期': 0, '量能枯竭': 0, '板块退潮': 0}
    for r in rows:
        chg = r[2] or 0
        nxt = r[3] or 0
        if chg is None or nxt is None:
            exit_dist['时间到期'] += 1
            continue
        if nxt <= -5:
            exit_dist['止损'] += 1
        elif nxt >= 7:
            exit_dist['止盈'] += 1
        elif nxt >= 9.5:
            exit_dist['一字板陷阱'] += 1
        else:
            exit_dist['时间到期'] += 1

    return {
        'win_rate': round(win_rate, 1),
        'avg_ret': round(avg_ret, 2),
        'plr': round(plr, 2),
        'trade_count': len(rets),
        'exit_dist': exit_dist,
    }


def _apply_rules(current: dict, metrics: dict, lr: float) -> dict:
    """根据指标动态调阈值"""
    n = dict(current)  # 复制
    win_rate = metrics.get('win_rate', 50)
    avg_ret = metrics.get('avg_ret', 0)
    exit_dist = metrics.get('exit_dist', {})
    total = sum(exit_dist.values()) or 1

    # 1. 胜率高 + EV 好 → 放宽止盈 (让利润奔跑)
    if win_rate >= 65 and avg_ret >= 2:
        n['take_profit'] = _clamp('take_profit', n['take_profit'] + lr * 0.5)

    # 2. 胜率高 + EV 差 (高胜低 EV) → 收紧止盈 (获利了结)
    if win_rate >= 60 and avg_ret < 0.5:
        n['take_profit'] = _clamp('take_profit', n['take_profit'] - lr * 0.5)

    # 3. 胜率低 + EV 负 → 收紧止损 (保护本金)
    if win_rate < 45 and avg_ret < 0:
        n['stop_loss'] = _clamp('stop_loss', n['stop_loss'] + lr * 0.5)  # -5 → -4.5 (收紧)

    # 4. 胜率低 + EV 好 (低胜高 EV) → 放宽止盈 + 延长 base_n
    if win_rate < 50 and avg_ret >= 3:
        n['take_profit'] = _clamp('take_profit', n['take_profit'] + lr * 0.5)
        n['base_n'] = int(_clamp('base_n', n['base_n'] + 1))

    # 5. 一字板触发率 > 30% → 降低一字板阈值 (更早卖)
    if exit_dist.get('一字板陷阱', 0) / total > 0.3:
        n['limit_pct'] = _clamp('limit_pct', n['limit_pct'] - lr * 0.5)

    # 6. 时间到期率 > 50% → 延长 base_n (持仓期太短没触发到决策)
    if exit_dist.get('时间到期', 0) / total > 0.5:
        n['base_n'] = int(_clamp('base_n', n['base_n'] + 1))

    # 7. 止损触发率 > 30% → 放宽止损 (避免太严, 错过反弹)
    if exit_dist.get('止损', 0) / total > 0.3:
        n['stop_loss'] = _clamp('stop_loss', n['stop_loss'] - lr * 0.5)  # -5 → -5.5 (放宽)

    # 8. 止盈触发率 > 30% → 收紧止盈 (锁利)
    if exit_dist.get('止盈', 0) / total > 0.3:
        n['take_profit'] = _clamp('take_profit', n['take_profit'] - lr * 0.5)

    return n


def _apply_tab_specific_overrides(tab: str, n: dict) -> dict:
    """tab-specific 阈值覆盖 (硬约束, 防止调权把涨停调成 dtqiaoban)"""
    n = dict(n)
    if tab == 'dtqiaoban':
        # 跌停翘板止损不能太宽 (防二次下跌)
        n['stop_loss'] = max(n['stop_loss'], -5.0)
    elif tab == 'limit-up':
        # 涨停止盈至少 5% (不能太紧, 强势股需要空间)
        n['take_profit'] = max(n['take_profit'], 5.0)
    return n


def tune_smart_exit_thresholds(granularity: str = 'daily', force: bool = False) -> dict:
    """SmartExit 阈值调权主入口

    Args:
        granularity: 'daily' | 'weekly' | 'monthly' (调权粒度)
        force: True 跳过盘中检查
    """
    start_ts = datetime.now()
    lr = _LEARNING_RATE.get(granularity, 0.3)
    min_trades = _MIN_TRADES.get(granularity, 3)

    # 1. 盘中检查
    if not force and granularity != 'daily':
        from cache import get_market_status
        status = get_market_status() if hasattr(__import__('cache'), 'get_market_status') else 'closed'
        if status in ('trading', 'lunch'):
            return {
                'status': 'skipped',
                'msg': f'盘中/午休时段不调权 (granularity={granularity})',
                'granularity': granularity,
            }

    # 2. 互斥锁
    if not _TUNER_LOCK.acquire(blocking=False):
        return {
            'status': 'skipped',
            'msg': '调权已在进行中',
            'granularity': granularity,
        }

    # 3. 僵死锁检查
    if not _is_lock_stale() and _read_status().get('status') == 'running':
        _TUNER_LOCK.release()
        return {
            'status': 'skipped',
            'msg': '上次调权未完成 (< 5 分钟)',
            'granularity': granularity,
        }

    _write_status('running', granularity=granularity, msg='调权启动')
    print(f"  [smart_exit_tuner] ═══ {granularity} 调权启动 {start_ts} ═══", file=sys.stderr)

    try:
        # 4. 加载当前阈值
        thresholds = _load_thresholds()

        # 5. 对每个 tab 调权
        results = []
        for tab in DEFAULT_THRESHOLDS:
            try:
                # 拉调权指标
                metrics = _fetch_recent_backtest_metrics(tab, granularity)
                if 'error' in metrics:
                    results.append({
                        'tab': tab, 'status': 'skipped',
                        'msg': metrics['error'][:100],
                    })
                    continue
                # 样本量检查
                if metrics.get('trade_count', 0) < min_trades:
                    results.append({
                        'tab': tab, 'status': 'skipped',
                        'msg': f'样本量 {metrics["trade_count"]} < {min_trades}',
                        'metrics': metrics,
                    })
                    continue

                # 应用调权规则
                old_th = dict(thresholds[tab])
                new_th = _apply_rules(old_th, metrics, lr)
                new_th = _apply_tab_specific_overrides(tab, new_th)

                # 钳制
                for k in _THRESHOLD_BOUNDS:
                    if k in new_th:
                        new_th[k] = _clamp(k, new_th[k])

                # 检查是否有变化
                changed = {k: v for k, v in new_th.items()
                           if k in old_th and v != old_th[k]
                           and k in _THRESHOLD_BOUNDS}
                if not changed:
                    results.append({
                        'tab': tab, 'status': 'no_change',
                        'msg': '指标未触发任何调权规则',
                        'metrics': metrics,
                    })
                    continue

                # 更新阈值
                new_th['tune_count'] = old_th.get('tune_count', 0) + 1
                new_th['last_tuned'] = datetime.now().isoformat()
                # 保留历史 (近 50 条)
                history = old_th.get('history', [])
                history.append({
                    'ts': datetime.now().isoformat(),
                    'granularity': granularity,
                    'old': {k: old_th.get(k) for k in changed},
                    'new': changed,
                    'metrics': metrics,
                    'reason': _explain_changes(changed, metrics),
                })
                new_th['history'] = history[-50:]

                thresholds[tab] = new_th
                results.append({
                    'tab': tab, 'status': 'tuned',
                    'changed': changed,
                    'metrics': metrics,
                })
                print(f"  [smart_exit_tuner]   {tab}: {changed}", file=sys.stderr)
            except Exception as e:
                results.append({
                    'tab': tab, 'status': 'failed',
                    'error': str(e)[:200],
                })
                print(f"  [smart_exit_tuner]   {tab} 失败: {e}", file=sys.stderr)

        # 6. 持久化
        _save_thresholds(thresholds)

        duration = (datetime.now() - start_ts).total_seconds()
        tuned_count = sum(1 for r in results if r.get('status') == 'tuned')
        _write_status(
            'done',
            granularity=granularity,
            tabs=results,
            duration_sec=round(duration, 1),
            msg=f'{tuned_count} 个 tab 调权',
        )
        print(f"  [smart_exit_tuner] ═══ 调权完成 {duration:.1f}s, {tuned_count} tab 已调 ═══",
              file=sys.stderr)

        return {
            'status': 'done',
            'granularity': granularity,
            'tabs': results,
            'tuned_count': tuned_count,
            'duration_sec': round(duration, 1),
        }
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        _write_status('failed', error=str(e)[:200])
        return {
            'status': 'failed',
            'error': str(e)[:200],
            'granularity': granularity,
        }
    finally:
        _TUNER_LOCK.release()


def _explain_changes(changed: dict, metrics: dict) -> str:
    """解释调权原因"""
    reasons = []
    if 'take_profit' in changed:
        if changed['take_profit'] > 0:
            reasons.append(f"胜率{metrics.get('win_rate', 0)}% EV{metrics.get('avg_ret', 0)}% 放宽止盈")
        else:
            reasons.append(f"高胜低 EV 收紧止盈")
    if 'stop_loss' in changed:
        if changed['stop_loss'] < 0:
            reasons.append("止损触发率高 放宽止损")
        else:
            reasons.append("低胜负 EV 收紧止损")
    if 'limit_pct' in changed:
        reasons.append("一字板触发率高 降低一字板阈值")
    if 'base_n' in changed:
        reasons.append("时间到期率高 延长持仓")
    return ' / '.join(reasons) if reasons else '指标触发调权'


def get_current_thresholds(tab: str = None) -> dict:
    """查当前阈值 (供回测引擎和前端展示)"""
    thresholds = _load_thresholds()
    if tab:
        return thresholds.get(tab, {})
    return thresholds


def get_tune_status() -> dict:
    """查调权状态 (前端轮询)"""
    return _read_status()


# ─── CLI 入口 ───
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='SmartExit 阈值调权')
    parser.add_argument('--granularity', choices=['daily', 'weekly', 'monthly'],
                        default='daily', help='调权粒度')
    parser.add_argument('--force', action='store_true', help='跳过盘中检查')
    parser.add_argument('--show', action='store_true', help='仅显示当前阈值')
    args = parser.parse_args()

    if args.show:
        import pprint
        pprint.pprint(get_current_thresholds())
    else:
        result = tune_smart_exit_thresholds(args.granularity, args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
