"""硬过滤器 v2 — 基于真实 next_day_change 数据挖掘 (2026-07-03 复盘)

数据来源: archive.db 18 天 1445 笔 T+1 真实收益

6 月 (1224 笔) 基线 +1.92% / 7 月 (221 笔) +0.11% (市场风格切换)
6+7 月都为正的高胜率子集 (30 个, EV+2.37%~+4.60%):

  早盘+连板≥2+换手<5%+市值≥300亿        +4.60% 胜率 73.4%  94笔
  早盘+连板=2+换手<5%                    +4.46% 胜率 73.9%  69笔
  连板≥2+换手<5%                          +4.39% 胜率 72.4%  98笔
  行业=化学制药+早盘+1板+市值≥300亿       +3.03% 胜率 72.2%  18笔
  行业=化学制药 (无其他过滤)               +2.80% 胜率 71.8%  39笔

7 月灾难级子集 (必须排除):
  1板+turnover 15-30% (7月 -5.16% ~ -8.65%)
  尾盘封板 (h>=14, 胜率 44.6%)

过滤层 (硬规则, 与评分无关):
  1. turnover 区间: 排除 < 0.5% (流动性差) 和 > 15% (出货)
  2. seal_fund 区间: 排除 < 0.5亿 (封单弱) 和 > 10亿 (过度博弈)
  3. seal_time: 排除尾盘封板 (h >= 14)
  4. industry: 排除 bottom7 行业
  5. consecutive: 可选过滤 (>=2 显著好于 1 板)

加权层 (软规则, 在硬过滤后子集上):
  - 连板数: 1板×1.0, 2板×1.3, 3板×1.5, >=4板×1.4
  - 行业: top8 ×1.2, bottom7 ×0.5
  - 封板时间: 上午<11 ×1.1, 午盘11-14 ×1.0, 尾盘>=14 ×0.4
  - 换手: <5% ×1.05, 5-15% ×1.0, 15-30% ×0.4
"""
from __future__ import annotations
import sqlite3
from typing import Callable, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════
#  行业分层 (来自 6+7 月数据挖掘, n>=10)
# ═══════════════════════════════════════════

TOP_INDUSTRIES = {
    '化学制药', '半导体', '元件', '玻璃玻纤', '计算机设',
    '软件开发', '专业工程', '证券Ⅱ', '农产品加', '纺织制造',
}

BOTTOM_INDUSTRIES = {
    '航运港口', 'IT服务Ⅱ', '服装家纺', '电力', '燃气Ⅱ',
    '房地产开', '医疗器械', '家电零部', '环境治理', '汽车零部',
}


def industry_tier(industry: Optional[str]) -> str:
    """返回 'top' / 'bottom' / 'mid'"""
    if not industry:
        return 'mid'
    if any(ind in industry for ind in TOP_INDUSTRIES):
        return 'top'
    if any(ind in industry for ind in BOTTOM_INDUSTRIES):
        return 'bottom'
    return 'mid'


# ═══════════════════════════════════════════
#  单行特征提取
# ═══════════════════════════════════════════

def seal_hour(seal_time: Optional[str]) -> int:
    """'092502' → 9; '145030' → 14; None → -1"""
    if not seal_time:
        return -1
    s = seal_time.replace(':', '')
    try:
        return int(s[:2])
    except (ValueError, TypeError):
        return -1


def is_seal_morning(seal_time: Optional[str]) -> bool:
    h = seal_hour(seal_time)
    return 0 <= h < 11


def is_seal_noon(seal_time: Optional[str]) -> bool:
    h = seal_hour(seal_time)
    return 11 <= h < 14


def is_seal_afternoon(seal_time: Optional[str]) -> bool:
    h = seal_hour(seal_time)
    return h >= 14


# ═══════════════════════════════════════════
#  硬过滤函数 (返回 bool, 接受 dict)
# ═══════════════════════════════════════════

def f_turnover_range(t: dict, lo: float = 0.5, hi: float = 15.0) -> bool:
    """换手率区间过滤 (0.5% ~ 15%)

    数据依据:
      - <0.5%:  流动性差, 容易一字板买不到
      - >15%:   7 月 -2.74% (vs <5% 7月+0.44%), 高位换手出货
    """
    tr = t.get('turnover')
    if tr is None:
        return False
    return lo <= tr <= hi


def f_seal_fund_range(t: dict, lo: float = 0.5e8, hi: float = 10e8) -> bool:
    """封单金额区间过滤 (0.5 亿 ~ 10 亿)

    数据依据:
      - <0.5亿: 胜率 52.1% (易开板)
      - 0.5-2亿: 胜率 57.7% (稳健)
      - 2-10亿: 胜率 70.5% (胜率最高, 7月 -0.31% 略负)
      - >10亿:  过度博弈, 极端票
    """
    sf = t.get('seal_fund')
    if sf is None:
        return False
    return lo <= sf <= hi


def f_exclude_afternoon_seal(t: dict) -> bool:
    """排除尾盘封板 (h >= 14)

    数据依据: 尾盘封板胜率仅 44.6%, EV -0.20%
    """
    return not is_seal_afternoon(t.get('seal_time'))


def f_exclude_bottom_industries(t: dict) -> bool:
    """排除 bottom 行业 (电力/航运/IT服务/服装家纺 等)"""
    return industry_tier(t.get('industry')) != 'bottom'


def f_min_consecutive(t: dict, min_cons: int) -> bool:
    """最小连板数过滤"""
    c = t.get('consecutive') or 0
    return c >= min_cons


def f_mcap_range(t: dict, lo: float = 0, hi: float = 1e9) -> bool:
    """市值区间过滤 (默认不限)"""
    mc = t.get('market_cap')
    if mc is None:
        return False
    return lo <= mc <= hi


# ═══════════════════════════════════════════
#  硬过滤预设
# ═══════════════════════════════════════════

def default_hard_filters() -> List[Callable[[dict], bool]]:
    """默认硬过滤组合 — 18 天数据验证: 显著优于无过滤"""
    return [
        lambda t: f_turnover_range(t, 0.5, 15.0),
        lambda t: f_seal_fund_range(t, 0.5e8, 10e8),
        f_exclude_afternoon_seal,
        f_exclude_bottom_industries,
    ]


def strict_hard_filters() -> List[Callable[[dict], bool]]:
    """严格硬过滤 — 进一步收紧到连板 ≥ 2"""
    fs = default_hard_filters()
    fs.append(lambda t: f_min_consecutive(t, 2))
    return fs


# ═══════════════════════════════════════════
#  软加权 (用于在硬过滤后子集上重排序)
# ═══════════════════════════════════════════

def default_score_adjuster(t: dict) -> float:
    """软加权 — 在硬过滤后的子集上对每行打调整系数

    实际使用时: final_score = base_score * coefficient

    加权维度 (基于 6+7 月数据):
      - 连板数: 1板×1.0, 2板×1.3, 3板×1.5, >=4板×1.4
      - 行业:   top ×1.2, bottom (硬过滤已排除) → 中性 1.0
      - 封板时间: 上午<11 ×1.1, 午盘11-14 ×1.0
      - 换手:   <5% ×1.05, 5-15% ×1.0
    """
    coef = 1.0

    # 连板数
    c = t.get('consecutive') or 0
    if c == 1:
        coef *= 1.0
    elif c == 2:
        coef *= 1.3
    elif c == 3:
        coef *= 1.5
    else:  # >= 4
        coef *= 1.4

    # 行业
    tier = industry_tier(t.get('industry'))
    if tier == 'top':
        coef *= 1.2
    # bottom 已被硬过滤排除

    # 封板时间
    h = seal_hour(t.get('seal_time'))
    if 0 <= h < 11:
        coef *= 1.1
    elif 11 <= h < 14:
        coef *= 1.0
    # >=14 已被硬过滤排除

    # 换手
    tr = t.get('turnover')
    if tr is not None:
        if tr < 5:
            coef *= 1.05
        # 5-15% 中性 1.0

    return coef


def conservative_score_adjuster(t: dict) -> float:
    """保守版加权 — sentiment 弱时使用, 进一步保守"""
    return default_score_adjuster(t) * 0.9


# ═══════════════════════════════════════════
#  工具: 把 archive.db 行转成 dict
# ═══════════════════════════════════════════

def row_to_dict(row) -> dict:
    """archive.db daily_stocks 行 → dict (供 filter/score 使用)"""
    if isinstance(row, dict):
        return row
    return {
        'trade_date': row[0],
        'code': row[1],
        'name': row[2],
        'stock_type': row[3],
        'change_pct': row[4],
        'price': row[5],
        'turnover': row[6],
        'seal_time': row[7],
        'seal_fund': row[8],
        'zhaban_times': row[9],
        'consecutive': row[10],
        'industry': row[11],
        'market_cap': row[12],
        'volume': row[13],
        'next_day_change': row[14],
        'next_day_open_change': row[15],
    }


def load_daily_pool(db_path: str) -> List[dict]:
    """从 archive.db 加载全部 daily_stocks (只取 limit_up + trend)"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT trade_date, code, name, stock_type, change_pct, price, turnover,
               seal_time, seal_fund, zhaban_times, consecutive, industry,
               market_cap, volume, next_day_change, next_day_open_change
        FROM daily_stocks
        WHERE next_day_change IS NOT NULL
    """).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


# ═══════════════════════════════════════════
#  akshare 实时池适配 (供 app.py 选股用)
# ═══════════════════════════════════════════

# akshare stock_zt_pool_em 实际列名
_AKSHARE_COL_MAP = {
    'code': '代码',
    'name': '名称',
    'industry': '所属行业',
    'turnover': '换手率',
    'seal_time': '首次封板时间',
    'seal_fund': '封板资金',
    'consecutive': '连板数',
    'market_cap': '流通市值',
}


def _akshare_row_to_dict(row) -> dict:
    """akshare stock_zt_pool_em 的一行 (pd.Series) → dict (供硬过滤用)

    兼容: 数字列可能 None, 字符串列可能空
    """
    def _g(key):
        col = _AKSHARE_COL_MAP[key]
        if col not in row.index:
            return None
        v = row[col]
        if v is None or (hasattr(v, '__class__') and v.__class__.__name__ == 'NaT'):
            return None
        return v

    def _fnum(v):
        if v is None: return None
        try: return float(v)
        except (ValueError, TypeError): return None

    return {
        'code': str(_g('code') or '').strip().zfill(6),
        'name': str(_g('name') or ''),
        'industry': str(_g('industry') or '') or None,
        'turnover': _fnum(_g('turnover')),
        'seal_time': str(_g('seal_time') or '') or None,
        'seal_fund': _fnum(_g('seal_fund')),
        'consecutive': int(_fnum(_g('consecutive')) or 0),
        'market_cap': _fnum(_g('market_cap')),
    }


# ═══════════════════════════════════════════
#  S9-prime 预定义 (生产推荐)
# ═══════════════════════════════════════════

# 6+7 月都为正的最优策略 — 部署到实盘的默认推荐
# 验证数据: 18 天 41 笔 胜率 80.5% EV +5.39% PLR 2.51 Sharpe 1.01 MaxDD -0.50%
# 条件: default_hard_filters() + 连板>=2 + 换手<5% + 行业=top×1.2 加权
SCHEME_PRESETS = {
    # S15-prime: 1板+换手<8% (放宽, 笔数充足)
    # 18天 N=54 胜率72.2% 6月+4.84% 7月-0.20% (略负)
    'S15-prime': {
        'hard_filters': default_hard_filters() + [
            lambda t: (t.get('consecutive') or 0) >= 2 or (t.get('turnover') is not None and t.get('turnover') < 8),
        ],
        'industry_boost': True,  # 软加权: top 行业 × 1.2
    },
    # S12-prime: 连板≥2 + 换手<8% + top 加权 (严苛高性能)
    # 18天 N=52 胜率73.1% 6月+4.92% 7月+1.19% MaxDD -0.50% — 6+7月双正
    # 风险: 7-3 这种连板少的日子只 1 票
    'S12-prime': {
        'hard_filters': default_hard_filters() + [
            lambda t: (t.get('consecutive') or 0) >= 2,
            lambda t: (t.get('turnover') is not None and t.get('turnover') < 8),
        ],
        'industry_boost': True,
    },
    # S12-no-boost: 不加权版
    'S12-no-boost': {
        'hard_filters': default_hard_filters() + [
            lambda t: (t.get('consecutive') or 0) >= 2,
            lambda t: (t.get('turnover') is not None and t.get('turnover') < 8),
        ],
        'industry_boost': False,
    },
    # S9-prime: 严格版 (笔数稀, 7月几乎0票 — 不推荐作为默认)
    'S9-prime': {
        'hard_filters': default_hard_filters() + [
            lambda t: (t.get('consecutive') or 0) >= 2,
            lambda t: (t.get('turnover') is not None and t.get('turnover') < 5),
        ],
        'industry_boost': True,
    },
    'S9-strict': {
        'hard_filters': default_hard_filters() + [
            lambda t: (t.get('consecutive') or 0) >= 2,
            lambda t: (t.get('turnover') is not None and t.get('turnover') < 5),
            lambda t: industry_tier(t.get('industry')) == 'top',
        ],
        'industry_boost': False,
    },
}


# ═══════════════════════════════════════════
#  双档 fallback: 严苛 → 放宽, 保证每天都有票
# ═══════════════════════════════════════════

# 多档 fallback 序列: 严苛 → 放宽 → 最后兜底 (无过滤)
FALLBACK_TIERS = [
    'S12-prime',   # 第一档: cons>=2 + 换手<8% + top 加权 (高性能)
    'S15-prime',   # 第二档: 含 1板换手<8% (笔数保证)
    'S12-no-boost',  # 第三档: 严苛但不加权 (兜底)
]


def apply_v2_to_stocks(stocks: List[dict], df, scheme: str = 'S9-prime',
                        top_n: int = None) -> List[dict]:
    """对 plan_a/plan_b.score 返回的 stocks 列表应用 v2 硬过滤 + 软加权重排

    Args:
        stocks: 评分后的 stocks 列表 (每个 dict 包含 code/name/total_score 等)
        df: 原始 filtered DataFrame (akshare 拉过来的, 包含 industry/turnover/...)
        scheme: 'S12-prime' / 'S15-prime' / 'S9-prime' / 'S9-strict' / 'S12-no-boost'
        top_n: 截取前 N 名 (None = 不截)

    Returns:
        过滤+重排后的 stocks 列表
    """
    if not stocks:
        return stocks
    if scheme not in SCHEME_PRESETS:
        raise ValueError(f'未知 scheme: {scheme}, 可选: {list(SCHEME_PRESETS.keys())}')

    preset = SCHEME_PRESETS[scheme]
    hard_filters = preset['hard_filters']
    industry_boost = preset.get('industry_boost', False)

    # 1. 用 df 构造 code → 行 映射 (拿 v2 过滤需要的字段)
    code_to_row = {}
    if df is not None and len(df) > 0:
        for idx in df.index:
            row_dict = _akshare_row_to_dict(df.loc[idx])
            c = row_dict.get('code')
            if c:
                code_to_row[c] = row_dict

    # 2. 过滤
    filtered_stocks = []
    for s in stocks:
        c = str(s.get('code', '')).strip().zfill(6)
        row = code_to_row.get(c, {})
        # 合并 row 字段到 stock (让 filter 看得到)
        merged = {**row, **s}
        # 统一类型: turnover/seal_fund 强制转 float (stocks 里可能是字符串)
        for k in ('turnover', 'seal_fund', 'market_cap'):
            v = merged.get(k)
            if v is not None and not isinstance(v, (int, float)):
                try:
                    merged[k] = float(v)
                except (ValueError, TypeError):
                    merged[k] = None
        # seal_time 强制转字符串
        st = merged.get('seal_time')
        if st is not None and not isinstance(st, str):
            merged['seal_time'] = str(st)
        if all(f(merged) for f in hard_filters):
            filtered_stocks.append(merged)

    # 3. 软加权 (industry boost on total_score)
    if industry_boost:
        for s in filtered_stocks:
            if industry_tier(s.get('industry')) == 'top':
                s['total_score'] = round(s.get('total_score', 0) * 1.2, 1)

    # 4. 重排 (按 total_score 降序)
    filtered_stocks.sort(key=lambda x: x.get('total_score', 0), reverse=True)

    # 5. 重置 rank
    for rank, s in enumerate(filtered_stocks, 1):
        s['rank'] = rank

    # 6. 截 top_n
    if top_n is not None:
        filtered_stocks = filtered_stocks[:top_n]

    return filtered_stocks


def apply_v2_with_fallback(stocks: List[dict], df, top_n: int = 10,
                            tier_min: int = 3) -> Tuple[List[dict], str]:
    """多档 fallback: 严苛 → 放宽, 保证至少 tier_min 票

    Args:
        stocks: plan_a.score 返回的 stocks 列表
        df: 原始 filtered DataFrame
        top_n: 截取前 N 名
        tier_min: 至少输出 N 票 (不够时降级到下一档)

    Returns:
        (filtered_stocks, used_scheme) 元组

    档位 (FALLBACK_TIERS):
        1. S12-prime (cons>=2 + 换手<8% + top 加权, 高性能)
        2. S15-prime (含 1 板换手<8%, 笔数保证)
        3. S12-no-boost (严苛但不加权, 兜底)
    """
    if not stocks:
        return [], 'none'

    # 缓存 code → row 映射 (避免每次重算)
    code_to_row = {}
    if df is not None and len(df) > 0:
        for idx in df.index:
            row_dict = _akshare_row_to_dict(df.loc[idx])
            c = row_dict.get('code')
            if c:
                code_to_row[c] = row_dict

    for tier_idx, scheme in enumerate(FALLBACK_TIERS):
        result = apply_v2_to_stocks(stocks, df, scheme=scheme, top_n=None)
        if len(result) >= tier_min:
            # 够了, 截 top_n
            final = result[:top_n]
            return final, scheme

    # 三档都 < tier_min: 用最后一档的结果 (即使票数少)
    last = apply_v2_to_stocks(stocks, df, scheme=FALLBACK_TIERS[-1], top_n=top_n)
    return last, FALLBACK_TIERS[-1]
