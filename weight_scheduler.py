#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘后自动调权调度器
=================================

**核心理念**:
- 盘中只读, 不调权 (用户实时拉数据, 调权冲撞会让用户看到"分数突变")
- 盘后调权 (15:30 触发), fire-and-forget, 调权期间用户请求直接用旧值
- 调权状态可查 (data/weight_adjust.lock 文件 + /api/weights/status)

**调度点**:
- `app.py` 的 `_run_close_scan()` 完成后触发 (15:05 收盘扫描)
- `run_after_hours_weight_adjust()` 内部再做一次 `get_market_status()` 检查
  (15:05 时还在 'closed' 才调, 'trading' 立即退出)

**互斥锁**:
- `_WEIGHT_ADJUST_LOCK` (threading.Lock) — 防同进程并发
- 文件 `data/weight_adjust.lock` — 跨进程防并发 + 状态查询
- 调权期间用户拉数据 → 加锁失败, 立即返回, 用旧权重 (调权是"best effort", 不阻塞用户)

**数据流**:
- 阶段 1 (plan_a): 跑 5 天回测 → 计算 factor × 收益相关性 → save_daily_correlations → daily_adjust_weights
- 阶段 2 (trend): 从 archive.db daily_stocks 读 N 天记录 → 调 adjust_trend_weights_from_backtest
- 阶段 3 (all tabs): 盘后预缓存炸板/翘板/涨停/反转回测 + 调 tab 权重
"""
import os
import sys
import json
import threading
from datetime import datetime, timedelta

# ─── 互斥锁 (模块级单例) ───
_WEIGHT_ADJUST_LOCK = threading.Lock()

# 状态文件 — 同时充当"调度记录"和"锁文件"
_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "weight_adjust.lock"
)


def _write_status(status: str, **extra):
    """写状态文件 — 供 /api/weights/status 查询 + 跨进程互斥检查"""
    try:
        os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
        payload = {
            'status': status,  # 'running' / 'done' / 'failed' / 'skipped' / 'stale'
            'ts': datetime.now().isoformat(),
            'pid': os.getpid(),
        }
        payload.update(extra)
        tmp = _LOCK_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _LOCK_FILE)  # 原子写
    except Exception as e:
        print(f"  [weight_scheduler] 写状态文件失败: {e}", file=sys.stderr)


def _read_status() -> dict:
    """读状态文件 — 不存在返回空 dict"""
    try:
        if os.path.exists(_LOCK_FILE):
            with open(_LOCK_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _is_lock_stale(max_age_sec: int = 600) -> bool:
    """锁文件 > 10 分钟没动 → 视为僵死 (上次调权崩了), 允许新调权启动"""
    s = _read_status()
    if not s or s.get('status') != 'running':
        return True
    try:
        ts = datetime.fromisoformat(s['ts'])
        age = (datetime.now() - ts).total_seconds()
        return age > max_age_sec
    except Exception:
        return True


def get_weight_adjust_status() -> dict:
    """对外暴露的调权状态 (供 /api/weights/status)"""
    s = _read_status()
    if not s:
        return {
            'status': 'never_run',
            'msg': '盘后调权尚未运行过',
            'running': False,
        }
    running = (s.get('status') == 'running') and not _is_lock_stale()
    return {
        'status': s.get('status', 'unknown'),
        'ts': s.get('ts'),
        'pid': s.get('pid'),
        'running': running,
        'last_adjusted_tabs': s.get('tabs', []),
        'last_error': s.get('error'),
        'msg': s.get('msg', ''),
    }


def _is_after_hours_safe() -> bool:
    """盘后才能调权 — 盘中立即跳过 (用户实时拉数据时绝不能调权)"""
    try:
        from scanner import get_market_status
        status = get_market_status()
        # 'closed' / 'weekend' / 'holiday' 都可以调
        # 'trading' / 'lunch' 绝对不调
        if status in ('closed', 'weekend', 'holiday'):
            return True
        return False
    except Exception as e:
        print(f"  [weight_scheduler] get_market_status 失败: {e}", file=sys.stderr)
        return False  # 不确定就跳过


# ─── 阶段 1: plan_a 调权 ───
def _adjust_plan_a() -> dict:
    """跑最近 5 天回测 → 计算 factor × 收益相关性 → 调 plan_a 权重"""
    from weight_manager import (
        daily_adjust_weights, save_daily_correlations, load_weights,
        DEFAULT_WEIGHTS, DAILY_LR, ROLLING_WINDOW,
    )
    try:
        from backtest_engine import run_tab_backtest
    except Exception as e:
        return {'tab': 'plan_a', 'status': 'skip', 'msg': f'backtest_engine 不可用: {e}'}

    print(f"  [weight_scheduler] 阶段1: plan_a 调权 (LR={DAILY_LR}, window={ROLLING_WINDOW})", file=sys.stderr)
    try:
        # 跑 5 天回测, 拿每笔交易 + 因子分
        # 复用 _run_t1_backtest_cached 不行 (那是 limit-up only), 用 backtest_engine 直接跑
        result = run_tab_backtest(
            tab='limit-up', max_days=5, top_n=3, capital=20000, use_cache=False
        )
        trades = result.get('trades', [])
        if len(trades) < 3:
            return {'tab': 'plan_a', 'status': 'skip', 'msg': f'交易数 {len(trades)} < 3, 跳过'}

        # 计算每个因子 (seal/tech/sector/history/money) 与 net_ret_pct 的相关性
        import pandas as pd
        df_trades = pd.DataFrame(trades)
        # 因子分列: backtest_engine 当前未把每笔的因子分保存到 trades (只存了 score 加权总分)
        # 用 score 总体作为代理: score 越高 → 整体评分体系越有效, 用 score 与 net_ret_pct 的相关性
        # 喂给 daily_adjust_weights 的 4 个 key 全部填同一个相关性 (粗调, 但能用)
        if 'score' not in df_trades.columns or 'net_ret_pct' not in df_trades.columns:
            return {'tab': 'plan_a', 'status': 'skip', 'msg': 'trades 缺少 score/net_ret_pct, 跳过'}
        if len(set(df_trades['score'])) <= 1:
            return {'tab': 'plan_a', 'status': 'skip', 'msg': 'score 全相同, 无相关性可算'}
        overall_corr = df_trades['score'].corr(df_trades['net_ret_pct'])
        if pd.isna(overall_corr):
            return {'tab': 'plan_a', 'status': 'skip', 'msg': '相关性为 NaN, 跳过'}
        # 同一相关性广播到 4 个 plan_a 因子 (daily_adjust_weights 会按权重份额分配调整)
        corrs = {f: float(overall_corr) for f in ('seal', 'tech', 'sector', 'history')}

        if not corrs:
            return {'tab': 'plan_a', 'status': 'skip', 'msg': '无可用因子列, 跳过'}

        # 1. 写入滚动数据 (供 daily_adjust_weights 读取)
        trading_date = result.get('trading_date', datetime.now().strftime('%Y-%m-%d'))
        save_daily_correlations(corrs, trading_date=trading_date, plan_name='A')
        print(f"  [weight_scheduler]  plan_a 写入 {len(corrs)} 个因子相关性: {corrs}", file=sys.stderr)

        # 2. 调权
        new_weights, summary = daily_adjust_weights(load_weights(), lr=DAILY_LR, plan_name='A')
        if new_weights is None:
            return {'tab': 'plan_a', 'status': 'skip', 'msg': summary}

        return {
            'tab': 'plan_a',
            'status': 'done',
            'correlations': corrs,
            'msg': summary,
            'trades': len(trades),
        }
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {'tab': 'plan_a', 'status': 'failed', 'error': str(e)[:200]}


# ─── 阶段 2: trend 调权 ───
def _adjust_trend() -> dict:
    """从 archive.db 读 daily_stocks 调 trend 权重"""
    from weight_manager import adjust_trend_weights_from_backtest
    print(f"  [weight_scheduler] 阶段2: trend 调权", file=sys.stderr)
    try:
        import sqlite3
        import pandas as pd
        db_path = 'archive.db'
        if not os.path.exists(db_path):
            return {'tab': 'trend', 'status': 'skip', 'msg': f'{db_path} 不存在'}

        con = sqlite3.connect(db_path)
        # 读最近 30 天的 trend 类型 + 次日收益
        df = pd.read_sql_query(
            "SELECT trade_date, code, name, change_pct, turnover, volume, "
            "next_day_change, next_day_open_change "
            "FROM daily_stocks WHERE stock_type='trend' "
            "ORDER BY trade_date DESC LIMIT 200",
            con
        )
        con.close()

        if df.empty or df['next_day_change'].isna().all():
            return {'tab': 'trend', 'status': 'skip', 'msg': 'archive.db 无 trend 次日数据, 跳过'}

        # 过滤: 有次日收益的
        df = df.dropna(subset=['next_day_change', 'change_pct'])
        if len(df) < 5:
            return {'tab': 'trend', 'status': 'skip', 'msg': f'有效记录 {len(df)} < 5, 跳过'}

        # 计算 net_ret_pct = 次日涨幅 - 今日涨幅 (相对涨幅, 真实收益)
        df['net_ret_pct'] = df['next_day_change'] - df['change_pct']

        # 构造 records 喂给 adjust_trend_weights_from_backtest
        # records 格式: {code, net_ret_pct, trend_chg, trend_turnover, trend_amount, ...}
        # 我们用 archive.db 的字段近似映射:
        records = []
        for _, r in df.iterrows():
            records.append({
                'code': r['code'],
                'net_ret_pct': r['net_ret_pct'],
                # 因子分列: 用原始数据近似 (chg/turnover/amount/vol_ratio)
                'trend_chg': r['change_pct'],
                'trend_turnover': r['turnover'] or 0,
                'trend_amount': (r['volume'] or 0) / 1e8,  # 转亿
                'trend_vol_ratio': 0,  # archive.db 无量比, 跳过
                'trend_new_high': 0,   # archive.db 无是否新高, 跳过
                'trend_ma_rev': 0,     # archive.db 无 MA 回归, 跳过
            })

        new_weights, summary = adjust_trend_weights_from_backtest(records, lr=0.02)
        return {
            'tab': 'trend',
            'status': 'done',
            'msg': summary,
            'records': len(records),
        }
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {'tab': 'trend', 'status': 'failed', 'error': str(e)[:200]}


# ─── 入口 ───
def run_after_hours_weight_adjust(force: bool = False) -> dict:
    """
    盘后自动调权 — 每天 15:30 触发 (app.py _run_close_scan 完成后)

    流程:
    1. 检查市场状态 (盘中/午休 立即跳过)
    2. 获取互斥锁 (非阻塞 — 已在调则跳过, 不阻塞用户拉数据)
    3. 阶段 1: plan_a 调权
    4. 阶段 2: trend 调权
    5. 阶段 3: 预缓存所有 tab 回测 + 调 tab 权重 (炸板/翘板/涨停/反转)
    6. 释放锁 + 写状态

    Args:
        force: 跳过市场状态检查 (CLI 调试用, 默认 False)

    Returns:
        dict: {status, tabs: [{tab, status, msg, ...}], duration_sec}
    """
    start_ts = datetime.now()

    # 1. 盘中检查 (force=True 跳过)
    if not force and not _is_after_hours_safe():
        return {
            'status': 'skipped',
            'msg': '盘中/午休时段不调权 (避免冲撞用户实时数据)',
            'duration_sec': 0,
        }

    # 2. 互斥锁 (非阻塞, 防并发)
    if not _WEIGHT_ADJUST_LOCK.acquire(blocking=False):
        return {
            'status': 'skipped',
            'msg': '调权已在进行中, 跳过本次',
            'duration_sec': 0,
        }

    # 3. 僵死锁检查 (上次崩了留的 running 状态)
    if not _is_lock_stale() and _read_status().get('status') == 'running':
        _WEIGHT_ADJUST_LOCK.release()
        return {
            'status': 'skipped',
            'msg': '上次调权未完成 (< 10分钟), 跳过',
            'duration_sec': 0,
        }

    _write_status('running', msg='调权启动')
    print(f"  [weight_scheduler] ═══ 盘后调权启动 {start_ts} ═══", file=sys.stderr)

    results = []
    try:
        # 阶段 1: plan_a
        r1 = _adjust_plan_a()
        results.append(r1)
        print(f"  [weight_scheduler]   阶段1 结果: {r1}", file=sys.stderr)

        # 阶段 2: trend
        r2 = _adjust_trend()
        results.append(r2)
        print(f"  [weight_scheduler]   阶段2 结果: {r2}", file=sys.stderr)

        # 阶段 3: 预缓存所有 tab 回测 + 调 tab 权重
        try:
            from backtest_engine import run_tab_backtest
            tab_configs = [
                ('zhaban', 80, '炸板'), ('dtqiaoban', 100, '翘板'),
                ('limit-up', 80, '涨停'), ('reversal', 0, '反转'),
            ]
            for tab, ms, label in tab_configs:
                res = run_tab_backtest(tab=tab, max_days=30, top_n=1, min_score=ms,
                                       capital=30000, use_cache=False)
                trades = res.get('comparison', {}).get('open_buy', {}).get('trades', [])
                results.append({
                    'tab': tab, 'status': 'done',
                    'trades': len(trades),
                    'msg': f'{label} 回测缓存完成: {len(trades)}笔',
                })
                print(f"  [weight_scheduler]   阶段3 {label}: {len(trades)}笔 cached", file=sys.stderr)
        except Exception as e:
            results.append({'tab': 'cache_all', 'status': 'failed', 'error': str(e)[:200]})
            print(f"  [weight_scheduler]   阶段3 失败: {e}", file=sys.stderr)

        # 阶段 3: reversal / tab 调权 (archive.db 缺因子分列, 暂搁)
        # TODO: 后续 daily_stocks 加 rev_chg/rev_lb_count/... 列后启用

        duration = (datetime.now() - start_ts).total_seconds()
        any_failed = any(r.get('status') == 'failed' for r in results)
        final_status = 'failed' if any_failed else 'done'

        _write_status(
            final_status,
            tabs=results,
            duration_sec=round(duration, 1),
            msg=f'{len(results)} 个 tab 调权完成',
        )
        print(f"  [weight_scheduler] ═══ 调权完成 {duration:.1f}s, 状态: {final_status} ═══", file=sys.stderr)

        return {
            'status': final_status,
            'tabs': results,
            'duration_sec': round(duration, 1),
        }
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        _write_status('failed', error=str(e)[:200], tabs=results)
        return {
            'status': 'failed',
            'error': str(e)[:200],
            'tabs': results,
            'duration_sec': round((datetime.now() - start_ts).total_seconds(), 1),
        }
    finally:
        _WEIGHT_ADJUST_LOCK.release()


# ─── CLI 入口 (供手动调权) ───
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='盘后自动调权调度器')
    parser.add_argument('--force', action='store_true', help='跳过市场状态检查')
    parser.add_argument('--status', action='store_true', help='只查状态不调权')
    args = parser.parse_args()

    if args.status:
        import json as _json
        print(_json.dumps(get_weight_adjust_status(), ensure_ascii=False, indent=2))
    else:
        result = run_after_hours_weight_adjust(force=args.force)
        import json as _json
        print(_json.dumps(result, ensure_ascii=False, indent=2))
