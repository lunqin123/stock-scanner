#!/usr/bin/env python3
"""前向验证权重优化器 (2026-08-01)

用途: 在修复后的回测引擎上, 用 walk-forward 方式优化各榜评分权重,
      避免在 30 天单窗口上过拟合。

方法:
  1. 取最近 30 个交易日, 前 20 天为训练集, 后 10 天为验证集
  2. 训练集上做 坐标上升 + 随机扰动 搜索 (约束: 权重>0, 和=100/默认和, 单因子变化<=50%)
  3. 用验证集检验最优权重; 仅当验证集 EV 优于基线时才保存

用法:
  python scripts/optimize_weights_walkforward.py --tab limit-up [--trials 24] [--min-score 65] [--save]

支持的 tab 与权重目标:
  limit-up  -> scoring.score_new.FACTOR_WEIGHTS (涨停榜实际排名分)
  trend     -> weight_manager.TREND_DEFAULT_WEIGHTS
  zhaban    -> weight_manager.ZB_DEFAULT_WEIGHTS
  dtqiaoban -> weight_manager.DT_DEFAULT_WEIGHTS
  reversal  -> weight_manager.REV_DEFAULT_WEIGHTS
"""
import argparse
import json
import multiprocessing as mp
import os
import socket
import sys
from datetime import datetime, timedelta

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

socket.setdefaulttimeout(15)


def _worker(q, tab, start, end, weights, min_score, top_n):
    """子进程执行单次回测: 隔离网络挂起, 由父进程硬超时杀掉。

    默认离线模式: 把所有 akshare 接口替换为快速失败, 回测只吃持久缓存
    + 本地归档, 避免当前网络下 akshare 长时间挂起。结果确定且可复现。
    """
    try:
        import backtest.backtest_engine as be
        # ── 离线: akshare 全接口快速失败 ──
        import backtest.backtest_pools as bp

        def _no_net(*a, **k):
            raise RuntimeError('offline-mode: akshare disabled')

        for _api in ('stock_zt_pool_em', 'stock_zt_pool_zbgc_em', 'stock_zt_pool_dtgc_em',
                     'stock_zt_pool_strong_em', 'stock_zt_pool_previous_em',
                     'stock_zh_a_hist', 'stock_zh_a_hist_tx', 'stock_zh_a_spot_em',
                     'stock_fund_flow_individual', 'stock_individual_fund_flow',
                     'stock_individual_fund_flow_rank'):
            if hasattr(bp.ak, _api):
                setattr(bp.ak, _api, _no_net)
        # ── 离线: OHLCV 只读缓存, 不触发网络重试 ──
        from cache import persistent_get as _pg, get as _cg

        def _cache_only_ohlcv(code, dates):
            res = {}
            for d in dates:
                key = f't1_ohlcv_{code}_{d}'
                v = _pg(key)
                if v == '__NONE__':
                    v = None
                if v is None:
                    v = _cg(key)
                    if v == '__NONE__':
                        v = None
                if v is not None:
                    res[d] = v
            return res

        be._get_ohlcv_batch = _cache_only_ohlcv
        _patch_target(tab, weights)
        res = be.run_tab_backtest(tab=tab, start_date=start, end_date=end, top_n=top_n,
                                  min_score=min_score, use_cache=False, capital=30000,
                                  buy_time='close' if tab == 'limit-up' else 'open')
        s = res.get('summary', {})
        q.put({
            'n': s.get('trade_count'), 'wr': s.get('win_rate'), 'ev': s.get('ev'),
            'cum': s.get('cumulative_ret'), 'dd': s.get('max_dd'),
            'error': res.get('error'),
        })
    except Exception as e:
        q.put({'error': str(e)[:150]})


class _Session:
    """断点续跑会话: 每个候选权重的结果都落盘, 中断后重启自动跳过已算过的。"""

    def __init__(self, tab):
        self.tab = tab
        self.progress = []
        self.seen = {}          # fingerprint -> result
        # 会话文件与最终报告分离: 会话含 seen 缓存, 报告只含摘要
        self.path = os.path.join(_PROJECT_ROOT, f'_opt_{tab}_session.json')
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding='utf-8') as f:
                    d = json.load(f)
                self.progress = d.get('progress', [])
                self.seen = {fp: r for fp, r in (d.get('seen') or {}).items()}
                print(f'[resume] 已缓存 {len(self.seen)} 个候选权重结果', flush=True)
            except Exception:
                pass

    def save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'tab': self.tab, 'progress': self.progress, 'seen': self.seen},
                      f, ensure_ascii=False)


def _trade_dates(max_days=30):
    from cache import _trading_date
    from backtest.backtest_engine import _trading_dates_in_range
    end = _trading_date().replace('-', '')
    start = (datetime.strptime(end, '%Y%m%d') - timedelta(days=max_days * 2)).strftime('%Y%m%d')
    return _trading_dates_in_range(start, end, max_count=max_days)


def _defaults_for(tab):
    if tab == 'limit-up':
        from scoring.score_new import FACTOR_WEIGHTS
        return dict(FACTOR_WEIGHTS)
    import scoring.weight_manager as wm
    return dict(getattr(wm, {
        'trend': 'TREND_DEFAULT_WEIGHTS',
        'zhaban': 'ZB_DEFAULT_WEIGHTS',
        'dtqiaoban': 'DT_DEFAULT_WEIGHTS',
        'reversal': 'REV_DEFAULT_WEIGHTS',
    }[tab]))


def _patch_target(tab, weights):
    """把候选权重注入评分函数实际调用的加载入口 (进程内 monkey-patch)。"""
    if tab == 'limit-up':
        import scoring.score_new as sn
        sn.load_factor_weights = lambda: dict(weights)
    else:
        import scoring.weight_manager as wm
        _orig = wm.load_tab_weights

        def _patched(t):
            return dict(weights) if t == tab else _orig(t)

        wm.load_tab_weights = _patched
        # 兼容历史入口: 趋势走 load_trend_weights, 其它走 _load_tab_weights
        if tab == 'trend':
            wm.load_trend_weights = lambda: dict(weights)
        else:
            wm._load_tab_weights = lambda t: dict(weights) if t == tab else _orig(t)


def _fingerprint(weights, start=None, end=None, min_score=None):
    """候选唯一指纹: 权重 + 日期窗口 + 阈值 (缺一不可, 防跨窗口误命中缓存)"""
    return json.dumps({
        'w': {k: round(v, 1) for k, v in sorted(weights.items())},
        's': start, 'e': end, 'm': min_score,
    })


def _run(tab, start, end, weights, min_score, top_n=3, sess=None, timeout=480):
    fp = _fingerprint(weights, start, end, min_score)
    if sess is not None and fp in sess.seen:
        return dict(sess.seen[fp])
    ctx = mp.get_context('spawn')
    q = ctx.Queue()
    p = ctx.Process(target=_worker, args=(q, tab, start, end, weights, min_score, top_n))
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.kill()
        p.join(2)
        out = {'error': f'TIMEOUT>{timeout}s', 'n': 0}
    else:
        try:
            out = q.get(timeout=5)
        except Exception:
            out = {'error': 'NO_RESULT', 'n': 0}
    if sess is not None:
        sess.seen[fp] = out
        sess.save()
    return out


def _normalize(weights, defaults):
    """约束: 正权重; 单因子变化不超过默认值的 50%; 归一化回默认和。"""
    total = sum(defaults.values())
    out = {}
    for k, d in defaults.items():
        lo, hi = d * 0.5, d * 1.5
        out[k] = max(lo, min(hi, weights.get(k, d)))
    s = sum(out.values())
    if s <= 0:
        return dict(defaults)
    scale = total / s
    # 缩放后再钳制一次, 防止个别因子越界
    out = {k: max(0.0, min(v * scale, d * 1.5)) for k, v, d in
           ((k, v, defaults[k]) for k, v in out.items())}
    return {k: round(v, 1) for k, v in out.items()}


def _coord_ascent(tab, defaults, train_dates, test_dates, min_score, sess):
    best_w = dict(defaults)
    best = _run(tab, train_dates[0], train_dates[-1], best_w, min_score, sess=sess)
    sess.progress.append({'phase': 'baseline-train', 'weights': best_w, 'result': best})
    sess.save()
    print(f'[baseline-train] EV={best.get("ev")} n={best.get("n")}', flush=True)
    improved = True
    while improved:
        improved = False
        for k in defaults:
            for delta in (3.0, 5.0, -3.0, -5.0):
                cand = dict(best_w)
                cand[k] = best_w[k] + delta
                cand = _normalize(cand, defaults)
                if cand == best_w:
                    continue
                r = _run(tab, train_dates[0], train_dates[-1], cand, min_score, sess=sess)
                sess.progress.append({'phase': 'coord', 'weights': cand, 'result': r})
                sess.save()
                ok = (r.get('error') is None and r.get('n', 0) >= 12
                      and r.get('ev', -99) > best.get('ev', -99) + 0.05)
                if ok:
                    best_w, best = cand, r
                    improved = True
                    print(f'  [coord] {k}{delta:+.0f} -> EV={best.get("ev")} n={best.get("n")}', flush=True)
    return best_w, best


def _random_search(tab, defaults, train_dates, min_score, trials, sess, best_w, best):
    import random
    rng = random.Random(20260801)
    for i in range(trials):
        cand = {}
        for k, d in defaults.items():
            cand[k] = d * (1.0 + rng.uniform(-0.45, 0.45))
        cand = _normalize(cand, defaults)
        r = _run(tab, train_dates[0], train_dates[-1], cand, min_score, sess=sess)
        sess.progress.append({'phase': 'random', 'weights': cand, 'result': r})
        sess.save()
        ok = (r.get('error') is None and r.get('n', 0) >= 12
              and r.get('ev', -99) > best.get('ev', -99) + 0.05)
        if ok:
            best_w, best = cand, r
            print(f'  [random {i}] EV={best.get("ev")} n={best.get("n")}', flush=True)
    return best_w, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tab', default='limit-up',
                    choices=['limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal'])
    ap.add_argument('--trials', type=int, default=24)
    ap.add_argument('--min-score', type=float, default=50.0)
    ap.add_argument('--save', action='store_true')
    ap.add_argument('--train-days', type=int, default=20)
    args = ap.parse_args()

    dates = _trade_dates(30)
    if len(dates) < args.train_days + 5:
        print(f'交易日不足: {len(dates)}'); return
    train_dates = dates[:args.train_days]
    test_dates = dates[args.train_days:]
    print(f'tab={args.tab} 窗口 {dates[0]}~{dates[-1]} '
          f'train={train_dates[0]}~{train_dates[-1]} ({len(train_dates)}天) '
          f'test={test_dates[0]}~{test_dates[-1]} ({len(test_dates)}天)', flush=True)

    defaults = _defaults_for(args.tab)
    sess = _Session(args.tab)

    base_train = _run(args.tab, train_dates[0], train_dates[-1], defaults, args.min_score, sess=sess)
    base_test = _run(args.tab, test_dates[0], test_dates[-1], defaults, args.min_score, sess=sess)
    print(f'[基线] train EV={base_train.get("ev")} n={base_train.get("n")} | '
          f'test EV={base_test.get("ev")} n={base_test.get("n")}', flush=True)

    best_w, best = _coord_ascent(args.tab, defaults, train_dates, test_dates,
                                 args.min_score, sess)
    best_w, best = _random_search(args.tab, defaults, train_dates, args.min_score,
                                  args.trials, sess, best_w, best)

    best_train = _run(args.tab, train_dates[0], train_dates[-1], best_w, args.min_score, sess=sess)
    best_test = _run(args.tab, test_dates[0], test_dates[-1], best_w, args.min_score, sess=sess)
    print(f'\n[最优] train EV={best_train.get("ev")} n={best_train.get("n")} | '
          f'test EV={best_test.get("ev")} n={best_test.get("n")}', flush=True)
    print('weights:', json.dumps(best_w, ensure_ascii=False), flush=True)

    test_gain = (best_test.get('ev') or -99) - (base_test.get('ev') or -99)
    print(f'test EV 提升: {test_gain:+.2f} pp', flush=True)

    # 会话(含 seen 缓存)单独落盘, 供 --save 重跑复用
    sess.save()
    out = {
        'tab': args.tab, 'dates': dates,
        'baseline': {'train': base_train, 'test': base_test},
        'best': {'train': best_train, 'test': best_test, 'weights': best_w},
        'test_ev_gain': round(test_gain, 2),
        'seen_count': len(sess.seen),
    }
    out_path = os.path.join(_PROJECT_ROOT, f'_opt_{args.tab}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f'progress saved: {out_path}', flush=True)

    if args.save and test_gain > 0 and (best_test.get('error') is None):
        if args.tab == 'limit-up':
            from scoring.score_new import save_factor_weights
            save_factor_weights(best_w)
        else:
            import scoring.weight_manager as wm
            if args.tab == 'trend':
                wm.save_trend_weights(best_w)
            else:
                wm._save_tab_weights(args.tab, best_w)
        print(f'[已保存] {args.tab} 新权重 (验证集 EV 提升 {test_gain:+.2f} pp)', flush=True)
    else:
        print('[未保存] 验证集未提升, 保持默认权重', flush=True)


if __name__ == '__main__':
    main()
