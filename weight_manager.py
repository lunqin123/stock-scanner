#!/usr/bin/env python3
"""
评分权重管理器
每日根据回测结果自动调整评分权重，以 JSON 持久化到缓存目录。
学习率低(0.02)，基于近5日滚动相关性均值，避免单日波动过大。
"""
import json
import os
import sys
import threading

import pandas as pd
import numpy as np

DEFAULT_WEIGHTS = {
    'seal': 28.0,      # 封板强度（回测r=+0.126，唯一显著预测因子，seal20+黄金区均涨+5.9%）
    'tech': 8.0,       # 量价结构（简化为换手率评级，回测r=+0.027）
    'sector': 17.0,    # 板块合力（合并原sector_res+sector_mom，含ETF资金共振）
    'sentiment': 25.0, # DEPRECATED 占位: 大盘情绪（系数调节，不参与加权和）
    'sector_res': 0.0, # DEPRECATED: 已合并到sector
    'sector_mom': 0.0, # DEPRECATED: 已合并到sector
    'history': 6.0,    # 历史股性（回测中为默认值，降权）
    'money': 17.0,     # 资金驱动（阶梯式分级，回测不可验证但实盘关键）
    'buyability': 0.0, # DEPRECATED: 降为纯过滤器(can_buy_filter)，不参与加权
    'stock_sentiment': 13.0,  # 个股情绪（资金态度+确定性+板块领先度）
    'principal_score': 8.0,   # 本金适配（提权，增强低价小市值标的区分度）
    'north_flow': 5.0, # v2.0: 北向资金市场级因子（聪明钱方向，盘中实时可追踪）
}
TOTAL_WEIGHT = sum(DEFAULT_WEIGHTS.values())  # 105
# 注: 实际"加权和"是 102 (8 个非零因子: seal+money+sector+tech+history+stock_sentiment+principal_score+north_flow
# = 28+17+17+8+6+13+8+5 = 102; sentiment/buyability/sector_res/sector_mom 不参与加权 — sentiment 是
# 乘法系数 [×0.85~1.15], sector_res/mom 已合并到 sector, buyability 降为纯过滤器)。
# 历史: 22+6+12+12+4+9+6=71  →  v1.24.1: 31+8+17+17+6+13+8=100 →  v2.0: +north_flow=5, seal 28

# 回测中可调权的因子
BACKTEST_FACTORS = ['seal', 'tech', 'sector', 'history']

# Plan B 可调权因子 (所有16个因子都参与IC检验, 弱因子自动归零)
BACKTEST_FACTORS_B = [
    'seal', 'money', 'sector', 'tech', 'history',
    'stock_sentiment', 'principal',
    'seal_quality', 'sector_resonance', 'volume_ratio',
    'north_flow', 'margin_ratio', 'inst_rating', 'limit_reason',
]

# IC 阈值: |IC| < 此值 → 权重归零 (统计噪声)
IC_NOISE_THRESHOLD = 0.02

# delta 调权的最小缩放基准: 默认=0 的死因子也能以合理步长移动
# 太小(1)→钳制[-1,2]太窄, -1就触底; 5→[-5,10]给负权留出空间
MIN_DELTA_SCALE = 5

_WEIGHTS_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "weights.json"
)
_WEIGHTS_FILE_B = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "weights_b.json"
)


def _atomic_write_json(path: str, data, indent=None, separators=(',', ':')):
    """原子写 JSON: 写 .tmp → os.replace, 避免写崩丢权重"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, separators=separators)
    os.replace(tmp, path)


def load_weights(plan_name: str = 'A') -> dict:
    """加载权重。Plan A 用 weights.json, Plan B 用 weights_b.json, 无文件返回默认值"""
    path = _WEIGHTS_FILE_B if plan_name.upper() == 'B' else _WEIGHTS_FILE
    defaults = DEFAULT_WEIGHTS
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            weights = dict(defaults)
            weights.update({k: v for k, v in data.items() if k in defaults})
            return weights
    except Exception as e:
        print(f"  [weight_manager L47] failed: {e}", file=sys.stderr)
    return dict(defaults)


def save_weights(weights: dict, plan_name: str = 'A'):
    """持久化权重。Plan A → weights.json, Plan B → weights_b.json。原子写防丢权重"""
    path = _WEIGHTS_FILE_B if plan_name.upper() == 'B' else _WEIGHTS_FILE
    try:
        _atomic_write_json(path, weights, indent=None, separators=(',', ':'))
    except Exception as e:
        print(f"  [WARN] 权重保存失败: {e}", file=sys.stderr)


# 各因子原始满分 (与 scoring 函数实际最大值一致)
_RAW_MAX = {'seal': 28.0, 'money': 20.0, 'sector': 15.0, 'sentiment': 10.0,
            'sector_res': 8.0, 'sector_mom': 12.0, 'tech': 10.0, 'history': 6.0,
            'stock_sentiment': 10.0, 'principal_score': 10.0, 'north_flow': 10.0}


def apply_weights(seal_scores, money_scores, sector_scores, tech_scores,
                  history_scores, sentiment_score,
                  stock_sentiment_scores=None, principal_scores=None,
                  north_flow_scores=None,  # v2.0: 北向资金因子
                  sector_res=None, sector_mom=None,  # DEPRECATED: 向后兼容
                  buyability_scores=None, weights=None):
    """
    8因子加权(v2.0) + 大盘情绪温和系数。
    sector_res/sector_mom 已合并为 sector_scores，旧参数仅向后兼容。
    大盘情绪(sentiment)温和系数调节(×0.85~×1.15)。
    北向资金(north_flow)市场级因子，所有标的统一分。
    """
    if stock_sentiment_scores is None:
        stock_sentiment_scores = pd.Series(5.0, index=seal_scores.index)
    if principal_scores is None:
        principal_scores = pd.Series(5.0, index=seal_scores.index)
    if north_flow_scores is None:
        north_flow_scores = pd.Series(5.0, index=seal_scores.index)
    # 向后兼容：如果传了sector_res/sector_mom但没传sector_scores，自动合并
    if sector_scores is None:
        if sector_res is not None and sector_mom is not None:
            sector_scores = (sector_res + sector_mom) / 2.0
        elif sector_res is not None:
            sector_scores = sector_res
        elif sector_mom is not None:
            sector_scores = sector_mom
        else:
            sector_scores = pd.Series(6.0, index=seal_scores.index)
    w = weights if weights else DEFAULT_WEIGHTS

    # 8因子加权 (v2.0: +north_flow)
    non_sentiment = ['seal', 'money', 'sector', 'tech', 'history',
                     'stock_sentiment', 'principal_score', 'north_flow']
    actual_sum = sum(w[k] for k in non_sentiment)
    weighted = (seal_scores * (w['seal'] / _RAW_MAX['seal']) +
                money_scores * (w['money'] / _RAW_MAX['money']) +
                sector_scores * (w['sector'] / _RAW_MAX['sector']) +
                tech_scores * (w['tech'] / _RAW_MAX['tech']) +
                history_scores * (w['history'] / _RAW_MAX['history']) +
                stock_sentiment_scores * (w['stock_sentiment'] / _RAW_MAX['stock_sentiment']) +
                principal_scores * (w['principal_score'] / _RAW_MAX['principal_score']) +
                north_flow_scores * (w['north_flow'] / _RAW_MAX['north_flow']))
    base_scores = weighted / max(1, actual_sum) * 100

    # 大盘情绪温和系数 (×0.85 ~ ×1.15, 缩小到±15%)
    if isinstance(sentiment_score, pd.Series):
        s_val = float(sentiment_score.iloc[0])
    else:
        s_val = float(sentiment_score)
    mult = np.clip(1.0 + (s_val - 5.0) * 0.03, 0.85, 1.15)

    return base_scores * mult


# ─── 滚动每日调权系统 ───
from datetime import date, timedelta

_ROLLING_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "claude_stock_cache", "rolling_correlations.json"
)

# 每日调权参数
DAILY_LR = 0.1        # 每日学习率（配合30天回测数据量, 加速IC收敛）
ROLLING_WINDOW = 5    # 滚动窗口：取最近N天相关性均值


_ROLLING_LOCK = threading.Lock()

def save_daily_correlations(correlations: dict, trading_date: str = None, plan_name: str = 'A'):
    """保存因子相关性到滚动缓存 (按 Plan 分组, 线程安全)。"""
    if not correlations:
        return
    if trading_date:
        s = trading_date.replace('-', '')
        if len(s) == 8 and s.isdigit():
            trading_date = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    today_str = trading_date if trading_date else date.today().isoformat()
    with _ROLLING_LOCK:
        try:
            data = _load_rolling_data()
            # 去重: 同日期+同Plan
            data = [d for d in data if not (d['date'] == today_str and d.get('plan', 'A') == plan_name)]
            data.append({'date': today_str, 'correlations': dict(correlations), 'plan': plan_name})
            data = data[-ROLLING_WINDOW * 6:]  # keep enough history
            _atomic_write_json(_ROLLING_FILE, data, indent=2)
        except Exception as e:
            print(f"  [weight_manager] save_daily_correlations failed: {e}", file=sys.stderr)


def _load_rolling_data() -> list:
    try:
        if os.path.exists(_ROLLING_FILE):
            with open(_ROLLING_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"  [weight_manager L221] failed: {e}", file=sys.stderr)
    return []


def get_rolling_progress(plan_name: str = 'A') -> str:
    """返回滚动窗口数据积累情况(按 Plan 分组, 口径与 daily_adjust_weights 一致)"""
    all_data = sorted(_load_rolling_data(), key=lambda d: d['date'])
    plan_data = [d for d in all_data if d.get('plan', 'A') == plan_name]
    recent = plan_data[-ROLLING_WINDOW:]
    label = f"Plan {plan_name}" if plan_name != 'A' else ""
    return f"回测数据 {label} {len(recent)}/{ROLLING_WINDOW} 天"


def daily_adjust_weights(current_weights: dict, lr: float = None, plan_name: str = 'A'):
    """
    IC/ICIR 驱动的每日调权。

    Plan B: ICIR 加权 + |IC| < 0.02 自动归零
    Plan A: 保持原有 delta-based 逻辑 (兼容)

    返回 (new_weights, summary_str)
    """
    if lr is None:
        lr = DAILY_LR

    all_data = sorted(_load_rolling_data(), key=lambda d: d['date'])
    all_data = [d for d in all_data if d.get('plan', 'A') == plan_name]
    if len(all_data) < 2:
        return None, f"  回测数据仅 {len(all_data)} 天，至少需要 2 天"

    recent = all_data[-ROLLING_WINDOW:]

    # 聚合各因子 IC 序列
    factor_vals = {}
    for entry in recent:
        for k, v in entry.get('correlations', {}).items():
            factor_vals.setdefault(k, []).append(v)

    # 计算 IC 均值 + ICIR
    ic_stats = {}
    for k, vals in factor_vals.items():
        if len(vals) < 2:
            continue
        ic_mean = float(np.mean(vals))
        ic_std = float(np.std(vals)) if len(vals) > 1 else 1.0
        icir = abs(ic_mean) / max(0.005, ic_std)  # cap at 20.0 for zero-variance factors
        icir = min(icir, 20.0)
        ic_stats[k] = {
            'ic_mean': round(ic_mean, 4),
            'ic_std': round(ic_std, 4),
            'icir': round(icir, 2),
        }

    if plan_name.upper() == 'B':
        # ── Plan B: ICIR 加权 + 噪声剔除 ──
        from plans.plan_b import PLAN_B_WEIGHTS as defaults_b
        factor_list = BACKTEST_FACTORS_B
        defaults = defaults_b
    else:
        # ── Plan A: 保持原有 delta-based ──
        factor_list = BACKTEST_FACTORS
        defaults = DEFAULT_WEIGHTS

    # ICIR 加权
    valid_factors = [f for f in factor_list if f in ic_stats]
    if len(valid_factors) < 2:
        return None, f"  有效因子仅 {len(valid_factors)} 个，至少需要 2 个"

    # 噪声剔除: |IC| < 阈值 → 权重归零
    active_factors = []
    dropped = []
    for f in valid_factors:
        if abs(ic_stats[f]['ic_mean']) >= IC_NOISE_THRESHOLD:
            active_factors.append(f)
        else:
            dropped.append(f)

    # ── 权重调整 ──
    new_weights = {}
    # 非回测因子保持当前值 (如 north_flow, stock_sentiment, principal_score 等)
    for k in defaults:
        if k in current_weights:
            new_weights[k] = current_weights[k]
        else:
            new_weights[k] = defaults[k]
    # 回测因子初始化为 0，等待 IC 驱动调整
    for f in factor_list:
        new_weights[f] = 0.0

    if plan_name.upper() == 'B':
        # Plan B: ICIR 按比例重分配 (14因子, 保持正权)
        total_icir = sum(ic_stats[f]['icir'] for f in active_factors) or 1.0
        for f in active_factors:
            new_weights[f] = round(defaults[f] * ic_stats[f]['icir'] / total_icir, 1)
        # 钳制 [0, 1.5×default]
        for f in active_factors:
            hi = defaults[f] * 1.5
            new_weights[f] = max(0.0, min(hi, new_weights[f]))
    else:
        # Plan A: Delta 驱动 (signed IC, 允许负权)
        for f in active_factors:
            d_scale = max(defaults[f], MIN_DELTA_SCALE)
            delta = ic_stats[f]['ic_mean'] * DAILY_LR * d_scale
            new_val = current_weights.get(f, defaults[f]) + delta
            lo = -d_scale          # 允许负权, 反向指标
            hi = d_scale * 1.5     # 上限 1.5×
            new_weights[f] = round(max(lo, min(hi, new_val)), 1)
        # 噪声因子: 保持当前值 (不归零, 也不回到默认)
        for f in dropped:
            if f in current_weights:
                new_weights[f] = current_weights[f]

    # 保存: 只保存回测可调权因子 (避免污染非回测因子如 money/stock_sentiment/principal_score/north_flow)
    save_data = {}
    for f in factor_list:
        if plan_name.upper() == 'B':
            if new_weights.get(f, 0) > 0:
                save_data[f] = new_weights[f]
        else:
            # Plan A: 保存非零值或当前权重中存在的值
            v = new_weights.get(f, 0)
            if v != 0 or f in current_weights:
                save_data[f] = v
    save_weights(save_data, plan_name=plan_name)

    # 摘要
    pname = f"Plan {plan_name}"
    adj_type = 'ICIR调权' if plan_name.upper() == 'B' else 'Delta调权'
    lines = [f"  {pname} {adj_type} ({len(recent)}天) | 有效{len(active_factors)}/总计{len(valid_factors)}"]
    for f in valid_factors:
        s = ic_stats[f]
        status = "+" if f in active_factors else "x"
        if plan_name.upper() == 'B':
            lines.append(f"    {status} {f}: IC={s['ic_mean']:+.3f} sigma={s['ic_std']:.3f} ICIR={s['icir']:.1f}")
        else:
            d_scale = max(defaults.get(f, 1), MIN_DELTA_SCALE)
            delta = s['ic_mean'] * DAILY_LR * d_scale
            cur = current_weights.get(f, defaults.get(f, 0))
            lines.append(f"    {status} {f}: IC={s['ic_mean']:+.3f} ICIR={s['icir']:.1f} cur={cur:.1f} delta={delta:+.3f}")
    if dropped:
        lines.append(f"  噪声剔除(|IC|<{IC_NOISE_THRESHOLD}): {', '.join(dropped)}")
    return new_weights, '\n'.join(lines)


# ═══════════════════════════════════════════
#  全 Tab 策略级权重 (基于回测胜率+EV)
# ═══════════════════════════════════════════

_TAB_PERF_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "stock_scanner_cache", "tab_performance.json"
)
_TAB_PERF_WINDOW = 5  # 滚动天数


def save_tab_performance(tab: str, summary: dict):
    """保存单次回测的 tab 表现数据"""
    try:
        os.makedirs(os.path.dirname(_TAB_PERF_FILE), exist_ok=True)
        data = {}
        if os.path.exists(_TAB_PERF_FILE):
            with open(_TAB_PERF_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        perf = data.setdefault(tab, [])
        entry = {
            'date': date.today().strftime('%Y%m%d'),
            'win_rate': summary.get('win_rate', 0),
            'ev': summary.get('ev', 0),
            'trades': summary.get('trade_count', 0),
            'cumulative_ret': summary.get('cumulative_ret', 0),
        }
        perf.append(entry)
        # 只保留最近 30 条
        if len(perf) > 30:
            perf = perf[-30:]
        data[tab] = perf
        _atomic_write_json(_TAB_PERF_FILE, data, indent=2)
    except Exception as e:
        print(f"  [tab_perf] 保存失败: {e}", file=sys.stderr)


def compute_tab_weights(force_refresh: bool = False):
    """基于全量历史回测胜率+EV 计算推荐仓位权重

    优先使用已保存的 tab 表现数据(累积30天);
    数据不足时自动触发全量历史回测(30天)来拟合权重。

    规则:
    - 胜率>=50% 且 EV>0 → 推荐重仓
    - 胜率>=40% 且 EV>0 → 中性仓位
    - EV>0 但胜率<40% → 轻仓试探
    - EV<0 → 建议观望

    Returns: [{tab, name_cn, weight, win_rate, ev, trades, label, color}]
    """
    # 读取已保存的 tab 表现
    try:
        with open(_TAB_PERF_FILE, 'r', encoding='utf-8') as f:
            all_data = json.load(f)
    except Exception:
        all_data = {}

    # tab → (中文名, 推荐TOP-N)
    tabs_cn = {
        'limit-up': ('涨停', 3),
        'trend': ('趋势', 1),
        'zhaban': ('炸板', 3),
        'dtqiaoban': ('翘板', 1),
        'reversal': ('反转', 1),
    }

    # 检查哪些 tab 数据不足, 需要拟合
    need_bootstrap = []
    for tab in tabs_cn:
        perf_list = all_data.get(tab, [])
        total_trades = sum(p.get('trades', 0) for p in perf_list[-_TAB_PERF_WINDOW:])
        if total_trades < 3 and force_refresh is False:
            need_bootstrap.append(tab)

    # 数据不足 → 跑全量历史回测拟合
    if need_bootstrap:
        print(f"  [tab权重] {len(need_bootstrap)}个tab数据不足, 跑30天回测拟合: {need_bootstrap}",
              file=sys.stderr)
        try:
            from backtest_engine import run_tab_backtest
            # 只对有缓存的日期跑一次(30天回测结果会被 daily cache 缓存)
            for tab in need_bootstrap:
                try:
                    top_n = tabs_cn[tab][1]  # 各 tab 的最优 TOP-N
                    r = run_tab_backtest(tab, max_days=30, top_n=top_n, use_cache=False)
                    s = r.get('summary', {})
                    if s.get('trade_count', 0) > 0:
                        save_tab_performance(tab, s)
                except Exception as e:
                    print(f"    {tab} 回测失败: {e}", file=sys.stderr)
            # 重新加载
            try:
                with open(_TAB_PERF_FILE, 'r', encoding='utf-8') as f:
                    all_data = json.load(f)
            except Exception:
                pass
        except Exception as e:
            print(f"  [tab权重] 拟合失败: {e}", file=sys.stderr)

    result = []
    for tab, (cn_name, _) in tabs_cn.items():
        perf_list = all_data.get(tab, [])
        # 取最近5条记录, 按交易笔数加权平均 (每笔记录可能是多天回测的汇总)
        recent = perf_list[-_TAB_PERF_WINDOW:] if perf_list else []
        if recent:
            total_trades = sum(p.get('trades', 0) for p in recent)
            if total_trades > 0:
                avg_wr = sum(p['win_rate'] * p.get('trades', 0) for p in recent) / total_trades
                avg_ev = sum(p['ev'] * p.get('trades', 0) for p in recent) / total_trades
            else:
                avg_wr = 0; avg_ev = 0
        else:
            avg_wr = 0; avg_ev = 0; total_trades = 0

        if total_trades == 0:
            weight = 0.5; label = '无交易'; color = '#94a3b8'
        elif avg_wr >= 50 and avg_ev > 0:
            weight = 1.2; label = '推荐重仓'; color = '#ef4444'
        elif avg_wr >= 40 and avg_ev > 0:
            weight = 1.0; label = '中性仓位'; color = '#f59e0b'
        elif avg_ev > 0:
            weight = 0.8; label = '轻仓试探'; color = '#fbbf24'
        else:
            weight = 0.5; label = '建议观望'; color = '#22c55e'

        result.append({
            'tab': tab, 'name_cn': cn_name,
            'weight': round(weight, 1),
            'win_rate': round(avg_wr, 1), 'ev': round(avg_ev, 2),
            'trades': total_trades, 'days': len(recent), 'total_trades': total_trades,
            'label': label, 'color': color,
        })

    # 归一化分配比例
    total = sum(r['weight'] for r in result)
    if total > 0:
        scale = len(result) / total
        for r in result:
            r['allocation_pct'] = round(r['weight'] * scale / len(result) * 100, 0)

    return result


# ═══════════════════════════════════════════
#  反转因子可调权 (P5: 4因子, ICIR驱动)
# ═══════════════════════════════════════════

_REV_WEIGHTS_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "stock_scanner_cache", "reversal_weights.json"
)

REV_DEFAULT_WEIGHTS = {
    'turnover': 40,     # 换手率
    'consecutive': 35,  # 连板位置
    'pullback': 15,     # 回调深度
    'sector': 10,       # 板块支撑
}

REV_FACTOR_NAMES = {
    'turnover': '换手', 'consecutive': '连板',
    'pullback': '回调', 'sector': '板块',
}


def load_reversal_weights() -> dict:
    try:
        if os.path.exists(_REV_WEIGHTS_FILE):
            with open(_REV_WEIGHTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            weights = dict(REV_DEFAULT_WEIGHTS)
            weights.update({k: v for k, v in data.items() if k in weights})
            return weights
    except Exception:
        pass
    return dict(REV_DEFAULT_WEIGHTS)


def save_reversal_weights(weights: dict):
    try:
        _atomic_write_json(_REV_WEIGHTS_FILE, weights, indent=2)
    except Exception as e:
        print(f"  [rev_weights] 保存失败: {e}", file=sys.stderr)


def adjust_reversal_weights_from_backtest(records: list, lr: float = 0.1):
    if len(records) < 5:
        return load_reversal_weights(), "数据不足(需≥5笔)"

    current = load_reversal_weights()
    factors = list(REV_DEFAULT_WEIGHTS.keys())
    factor_keys = [f'rev_{f}' for f in factors]

    sample = records[0]
    available = [fk for fk in factor_keys if fk in sample]
    if len(available) < 2:
        return current, "记录缺少因子分数"

    corrs = {}
    for fk in available:
        factor_name = fk.replace('rev_', '')
        scores = [r.get(fk, 0) for r in records]
        rets = [r.get('net_ret_pct', 0) for r in records]
        if len(set(scores)) <= 1:
            continue
        corr = pd.Series(scores).corr(pd.Series(rets))
        corrs[factor_name] = corr if not pd.isna(corr) else 0

    if not corrs:
        return current, "无有效相关性数据"

    new_weights = dict(current)
    lines = []
    for f, corr in corrs.items():
        delta = corr * lr * max(REV_DEFAULT_WEIGHTS[f], MIN_DELTA_SCALE)
        new_val = current[f] + delta
        lo = -max(REV_DEFAULT_WEIGHTS[f], MIN_DELTA_SCALE)
        hi = max(REV_DEFAULT_WEIGHTS[f], MIN_DELTA_SCALE) * 2.0
        new_weights[f] = round(max(lo, min(hi, new_val)), 1)
        arrow = '↑' if delta > 0 else '↓'
        lines.append(f"  {arrow} {REV_FACTOR_NAMES.get(f,f)}: {current[f]:.0f}→{new_weights[f]:.1f} (IC={corr:+.3f})")

    save_reversal_weights(new_weights)
    for f, corr in corrs.items():
        save_weight_history('reversal', f, current[f], new_weights[f], corr)
    return new_weights, '\n'.join(lines)


# ═══════════════════════════════════════════
#  炸板 + 翘板 因子权重 (P5)
# ═══════════════════════════════════════════

ZB_DEFAULT_WEIGHTS = {'seal': 20, 'money': 20, 'feature': 15, 'turnover': 10, 'sector': 12}
ZB_FACTOR_NAMES = {'seal': '封板', 'money': '资金', 'feature': '特征', 'turnover': '换手', 'sector': '板块'}

DT_DEFAULT_WEIGHTS = {'deal': 25, 'seal': 25, 'cont': 25, 'turnover': 15, 'time': 10}
DT_FACTOR_NAMES = {'deal': '放量', 'seal': '封单', 'cont': '连跌', 'turnover': '换手', 'time': '时间'}

# 涨停专用权重（板块热度为核心，封板强度降低）
DEFAULT_WEIGHTS_LIMIT_UP = {
    'seal': 20.0,       # 封板强度（降权，对T+3预测力弱）
    'sector': 25.0,     # 板块热度（涨停持续性的核心预测因子）
    'money': 15.0,      # 资金驱动（重新启用）
    'tech': 10.0,       # 量价结构
    'history': 10.0,    # 历史股性
    'stock_sentiment': 15.0,  # 个股情绪（板块龙头溢价）
    'principal_score': 5.0,   # 本金适配
}

# 反转专用权重（连板为王、换手率符号修正）
REV_DEFAULT_WEIGHTS = {'turnover': 25, 'consecutive': 30, 'pullback': 25, 'sector': 15, 'retention': 5}
REV_FACTOR_NAMES = {'turnover': '换手', 'consecutive': '连板', 'pullback': '回调', 'sector': '板块', 'retention': '留存'}

_WEIGHTS_FILES = {
    'zhaban': os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "stock_scanner_cache", "zhaban_weights.json"),
    'dtqiaoban': os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "stock_scanner_cache", "dtqiaoban_weights.json"),
    'limit-up': os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "stock_scanner_cache", "limit_up_weights.json"),
    'reversal': os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "stock_scanner_cache", "reversal_weights.json"),
}

DEFAULTS_MAP = {
    'zhaban': ZB_DEFAULT_WEIGHTS, 'dtqiaoban': DT_DEFAULT_WEIGHTS,
    'limit-up': DEFAULT_WEIGHTS_LIMIT_UP, 'reversal': REV_DEFAULT_WEIGHTS,
}
NAMES_MAP = {'zhaban': ZB_FACTOR_NAMES, 'dtqiaoban': DT_FACTOR_NAMES, 'reversal': REV_FACTOR_NAMES}
PREFIX_MAP = {'zhaban': 'zb', 'dtqiaoban': 'dt', 'reversal': 'rev'}


def _load_tab_weights(tab: str) -> dict:
    path = _WEIGHTS_FILES.get(tab)
    defaults = DEFAULTS_MAP.get(tab, {})
    if not path: return dict(defaults)
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            weights = dict(defaults)
            weights.update({k: v for k, v in data.items() if k in weights})
            return weights
    except Exception: pass
    return dict(defaults)


def _save_tab_weights(tab: str, weights: dict):
    path = _WEIGHTS_FILES.get(tab)
    if not path: return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write_json(path, weights, indent=2)
    except Exception as e:
        print(f"  [_save_tab_weights] {tab} 写入失败: {e}", file=sys.stderr)


def adjust_tab_weights_from_backtest(tab: str, records: list, lr: float = 0.1):
    if len(records) < 5:
        return _load_tab_weights(tab), "数据不足"
    current = _load_tab_weights(tab)
    defaults = DEFAULTS_MAP.get(tab, {})
    prefix = PREFIX_MAP.get(tab, '')
    names = NAMES_MAP.get(tab, {})
    factors = list(defaults.keys())
    factor_keys = [f'{prefix}_{f}' for f in factors]

    sample = records[0]
    available = [fk for fk in factor_keys if fk in sample]
    if len(available) < 2: return current, "缺因子列"

    corrs = {}
    for fk in available:
        fn = fk.replace(f'{prefix}_', '')
        scores = [r.get(fk, 0) for r in records]
        rets = [r.get('net_ret_pct', 0) for r in records]
        if len(set(scores)) <= 1: continue
        corr = pd.Series(scores).corr(pd.Series(rets))
        corrs[fn] = corr if not pd.isna(corr) else 0

    if not corrs: return current, "无有效IC"
    new_weights = dict(current)
    for f, corr in corrs.items():
        delta = corr * lr * max(defaults[f], MIN_DELTA_SCALE)
        new_val = current[f] + delta
        lo = -max(defaults[f], MIN_DELTA_SCALE); hi = max(defaults[f], MIN_DELTA_SCALE) * 2.0
        new_weights[f] = round(max(lo, min(hi, new_val)), 1)
    _save_tab_weights(tab, new_weights)
    for f, corr in corrs.items():
        save_weight_history(tab, f, current[f], new_weights[f], corr)
    return new_weights, ''


# ═══════════════════════════════════════════
#  趋势因子可调权 (P4: 5因子, ICIR驱动)
# ═══════════════════════════════════════════

_TREND_WEIGHTS_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
    "stock_scanner_cache", "trend_weights.json"
)

TREND_DEFAULT_WEIGHTS = {
    'chg': 40,       # 涨幅分
    'turnover': 30,  # 换手分
    'amount': 30,    # 成交额分
    'vol_ratio': 5,  # 量比加分
    'new_high': 3,   # 新高加分
    'ma_rev': 0,     # MA回归分 (IC无效, 暂关闭)
}

TREND_FACTOR_NAMES = {
    'chg': '涨幅', 'turnover': '换手', 'amount': '成交额',
    'vol_ratio': '量比', 'new_high': '新高', 'ma_rev': '均线',
}


def load_trend_weights() -> dict:
    """加载趋势因子权重, 无文件返回默认值"""
    try:
        if os.path.exists(_TREND_WEIGHTS_FILE):
            with open(_TREND_WEIGHTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            weights = dict(TREND_DEFAULT_WEIGHTS)
            weights.update({k: v for k, v in data.items() if k in weights})
            return weights
    except Exception:
        pass
    return dict(TREND_DEFAULT_WEIGHTS)


def save_trend_weights(weights: dict):
    """持久化趋势因子权重。原子写防丢权重"""
    try:
        _atomic_write_json(_TREND_WEIGHTS_FILE, weights, indent=2)
    except Exception as e:
        print(f"  [trend_weights] 保存失败: {e}", file=sys.stderr)


def adjust_trend_weights_from_backtest(records: list, lr: float = 0.1):
    """基于回测交易记录调整趋势因子权重

    对每笔交易的因子分与收益做相关性分析, 正相关因子加权重, 负相关降权重。
    records: [{code, net_ret_pct, trend_chg, trend_turnover, trend_amount, ...}]
    lr: 学习率
    """
    if len(records) < 5:
        return load_trend_weights(), "数据不足(需≥5笔)"

    current = load_trend_weights()
    factors = list(TREND_DEFAULT_WEIGHTS.keys())
    factor_keys = [f'trend_{f}' for f in factors]

    # 检查记录是否含因子分列
    sample = records[0]
    available = [fk for fk in factor_keys if fk in sample]
    if len(available) < 3:
        return current, "记录缺少因子分数, 保持默认权重"

    # 计算每个因子与收益的相关性
    corrs = {}
    for fk in available:
        factor_name = fk.replace('trend_', '')
        scores = [r.get(fk, 0) for r in records]
        rets = [r.get('net_ret_pct', 0) for r in records]
        if len(set(scores)) <= 1:
            continue
        corr = pd.Series(scores).corr(pd.Series(rets))
        corrs[factor_name] = corr if not pd.isna(corr) else 0

    if not corrs:
        return current, "无有效相关性数据"

    # 调权: 相关性>0 → 加权重; <0 → 降权重
    new_weights = dict(current)
    lines = []
    for f, corr in corrs.items():
        delta = corr * lr * max(TREND_DEFAULT_WEIGHTS[f], MIN_DELTA_SCALE)
        new_val = current[f] + delta
        # 钳制: -max(d,1) ~ 2*max(d,1) (允许负权,反向指标, 死因子复活)
        lo = -max(TREND_DEFAULT_WEIGHTS[f], MIN_DELTA_SCALE)
        hi = max(TREND_DEFAULT_WEIGHTS[f], MIN_DELTA_SCALE) * 2.0
        new_weights[f] = round(max(lo, min(hi, new_val)), 1)
        arrow = '↑' if delta > 0 else '↓'
        lines.append(f"  {arrow} {TREND_FACTOR_NAMES.get(f,f)}: {current[f]:.0f}→{new_weights[f]:.1f} (IC={corr:+.3f})")

    save_trend_weights(new_weights)
    for f, corr in corrs.items():
        save_weight_history('trend', f, current[f], new_weights[f], corr)
    return new_weights, '\n'.join(lines)


def get_trend_weight_summary() -> dict:
    """返回趋势因子权重摘要(前端展示)"""
    weights = load_trend_weights()
    return {
        'factors': [
            {'key': k, 'name': TREND_FACTOR_NAMES.get(k, k),
             'current': weights[k],
             'default': TREND_DEFAULT_WEIGHTS[k],
             'delta': round(weights[k] - TREND_DEFAULT_WEIGHTS[k], 1)}
            for k in TREND_DEFAULT_WEIGHTS
        ],
        'total': sum(weights.values()),
    }


# ═══════════════════════════════════════════
#  统一权重查询 + 调权历史 (P6: 回测UI重构)
# ═══════════════════════════════════════════

_WEIGHT_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "weight_history.jsonl"
)


def save_weight_history(tab: str, factor: str, old_val: float, new_val: float, corr: float):
    """每次调权记录一条 JSONL"""
    try:
        os.makedirs(os.path.dirname(_WEIGHT_HISTORY_FILE), exist_ok=True)
        entry = {
            'date': date.today().strftime('%Y-%m-%d'),
            'tab': tab, 'factor': factor,
            'old': round(old_val, 2), 'new': round(new_val, 2),
            'delta': round(new_val - old_val, 2),
            'corr': round(corr, 3),
            'arrow': '↑' if new_val > old_val else ('↓' if new_val < old_val else '→'),
        }
        with open(_WEIGHT_HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass


def get_weight_history(tab: str, days: int = 30) -> list:
    """读取近 N 天该 tab 的调权历史"""
    if not os.path.exists(_WEIGHT_HISTORY_FILE):
        return []
    cutoff = (date.today() - timedelta(days=days)).strftime('%Y-%m-%d')
    out = []
    try:
        with open(_WEIGHT_HISTORY_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get('tab') == tab and e.get('date', '') >= cutoff:
                        out.append(e)
                except Exception:
                    pass
    except Exception:
        pass
    return out


# plan_a 因子名映射(用真实 DEFAULT_WEIGHTS key,而不是 v2 重构时硬编码的旧 key 列表)
_PLAN_A_FACTOR_NAMES = {
    'seal': '封板', 'tech': '技术', 'sector': '板块',
    'sentiment': '大盘情绪', 'history': '历史', 'money': '资金',
    'stock_sentiment': '个股情绪', 'principal_score': '本金',
}


def _plan_a_summary() -> dict:
    """limit-up tab 因子权重摘要(走 plan_a 9 因子)

    关键修复(v2 重构时硬编码了已 DEPRECATED 的 key 列表):
    - DEFAULT_WEIGHTS 里 'sector_res' / 'sector_mom' / 'buyability' 都 = 0(已合并/降级)
    - DEFAULT_WEIGHTS 实际生效的 key 是: seal/tech/sector/sentiment/history/money/stock_sentiment/principal_score
    - weights.json 里存的是 'principal_score',不是 'principal'
    """
    w = load_weights()           # 运行时权重(已 merge default)
    default = DEFAULT_WEIGHTS    # 原始默认值(用于显示 default 和计算 delta)
    # 选 default > 0 的 key(过滤掉 DEPRECATED 零值)
    active_keys = [k for k in default.keys() if default[k] > 0]
    factors = []
    for k in active_keys:
        cur = w.get(k, default[k])
        factors.append({
            'key': k,
            'name': _PLAN_A_FACTOR_NAMES.get(k, k),
            'current': round(float(cur), 1),
            'default': round(float(default[k]), 1),
            'delta': round(float(cur) - default[k], 1),
            # plan_a 是 daily_adjust_weights 改运行副本(不写文件),
            # 调权方向字段填 '—',UI 不显示箭头
            'arrow': '—',
        })
    return {
        'factors': factors,
        'total': round(sum(f['current'] for f in factors), 1),
        'note': 'plan_a 实时调权,无持久化历史,delta/arrow 不展示',
    }


def _tab_factor_summary(tab: str) -> dict:
    """zhaban/dtqiaoban 的因子权重摘要"""
    defaults = DEFAULTS_MAP.get(tab, {})
    names = NAMES_MAP.get(tab, {})
    current = _load_tab_weights(tab)
    return {
        'factors': [
            {'key': k, 'name': names.get(k, k),
             'current': round(current.get(k, defaults[k]), 1),
             'default': round(defaults[k], 1),
             'delta': round(current.get(k, defaults[k]) - defaults[k], 1)}
            for k in defaults
        ],
        'total': round(sum(current.values()), 1),
    }


def _reversal_summary() -> dict:
    """反转因子权重摘要"""
    defaults = REV_DEFAULT_WEIGHTS
    names = REV_FACTOR_NAMES
    current = load_reversal_weights()
    return {
        'factors': [
            {'key': k, 'name': names.get(k, k),
             'current': round(current.get(k, defaults[k]), 1),
             'default': round(defaults[k], 1),
             'delta': round(current.get(k, defaults[k]) - defaults[k], 1)}
            for k in defaults
        ],
        'total': round(sum(current.values()), 1),
    }


def _tab_position_summary() -> dict:
    """sector tab 仓位权重摘要 — 走 tab_performance.json 的仓位系数 (0.5-1.2)

    注: sector 是板块联动,没有"因子权重"概念,只有"仓位权重"。
    把所有 tab 的仓位权重展平成单一列表,前端可平铺展示。
    """
    try:
        all_weights = compute_tab_weights()
    except Exception as e:
        return {'factors': [], 'total': 0, 'error': f'compute_tab_weights 失败: {e}'}
    if not all_weights:
        return {'factors': [], 'total': 0, 'error': 'tab_performance.json 暂无数据'}
    factors = []
    for w in all_weights:
        factors.append({
            'key': w['tab'],
            'name': w.get('name_cn', w['tab']),
            'current': w['weight'],
            'default': 1.0,
            'delta': round(w['weight'] - 1.0, 2),
            'arrow': '↑' if w['weight'] > 1.0 else ('↓' if w['weight'] < 1.0 else '→'),
            'extra': f"{w.get('label','')} (WR {w.get('win_rate',0):.1f}% / EV {w.get('ev',0):.2f}%)"
        })
    return {
        'factors': factors,
        'total': round(sum(f['current'] for f in factors), 1),
        'note': 'sector tab 走的是 tab 仓位权重,非因子权重',
    }


def get_tab_weight_summary(tab: str) -> dict:
    """统一入口: 返回各tab的因子权重摘要 + 调权历史"""
    if tab == 'trend':
        s = get_trend_weight_summary()
        s['factors'] = [{'key': f['key'], 'name': f['name'],
                         'current': f['current'], 'default': f['default'],
                         'delta': f['delta']} for f in s['factors']]
    elif tab == 'reversal':
        s = _reversal_summary()
    elif tab in ('zhaban', 'dtqiaoban'):
        s = _tab_factor_summary(tab)
    elif tab == 'limit-up':
        s = _plan_a_summary()
    elif tab == 'sector':
        s = _tab_position_summary()
    else:
        return {'factors': [], 'total': 0, 'error': f'未知 tab: {tab}'}
    s['history'] = get_weight_history(tab)
    return s
