#!/usr/bin/env python3
"""
增强指标分析模块
封成比、板块龙头、量比、K线位置、概念叠加、龙虎榜席位质量
"""
import sys
import pandas as pd
import numpy as np
from datetime import date, timedelta

try:
    import akshare as ak
except ImportError:
    ak = None

# ─── 已知的顶级游资和机构关键词 ───
# 用于识别席位质量（匹配营业部名称）
TOP_TOURIST_KEYWORDS = [
    '中关村', '赵老哥', '欢乐海岸', '章盟主', '方新侠',
    '炒股养家', '作手新一', '小鳄鱼', '湖州劳动路', '上塘路',
    '东北猛男', '桑田路', '西湖国贸', '宁波解放南', '上海溧阳',
    '国泰君安南京', '华鑫上海', '中信上海', '东方上海',
]
INSTITUTIONAL_KEYWORDS = [
    '机构专用', '深股通专用', '沪股通专用', '中信证券总部',
    '国泰君安总部', '华泰证券总部',
]
SUSPICIOUS_KEYWORDS = [
    '散户', '拉萨', '东财', '东方财富', '团结路', '东环路',
]


# ═══════════════════════════════════════════
#  快指标（只需现有DF，无需额外API）
# ═══════════════════════════════════════════

def calc_seal_ratio(df: pd.DataFrame) -> pd.Series:
    """封成比 = 封板资金 / 成交额，越高说明封板决心越强"""
    seal = df.get('封板资金', pd.Series(0, index=df.index)).fillna(0).astype(float)
    turnover = df.get('成交额', pd.Series(1, index=df.index)).fillna(0).astype(float)
    ratio = seal / turnover.replace(0, float('nan'))
    return ratio.fillna(0).round(2)


def calc_sector_leadership(df: pd.DataFrame) -> pd.Series:
    """板块内封板时间排序，1=板块最先封板（龙头）"""
    time_col = '首次封板时间'
    industry_col = '所属行业'
    if time_col not in df.columns or industry_col not in df.columns:
        return pd.Series(1, index=df.index)

    def _parse(t):
        try:
            t = str(t).strip()
            return int(t[:2]) * 60 + int(t[2:4])
        except (ValueError, IndexError):
            return 999

    times = df[time_col].apply(_parse)
    ranks = times.groupby(df[industry_col]).rank(method='min')
    return ranks.astype(int)


def calc_concept_count(df: pd.DataFrame) -> pd.Series:
    """概念叠加数：从所属行业和个股特征粗略估算"""
    # 基础：行业算1个，连板算辨识度
    # 更精确需调 concept API，此处先返回行业标识
    return pd.Series(1, index=df.index)


def calc_seal_quality_label(seal_ratio: pd.Series) -> pd.Series:
    """封成比等级标签（针对A股涨停板数据调整阈值）"""
    def _label(r):
        if r > 5:
            return '死封'
        if r > 1:
            return '强封'
        if r > 0.5:
            return '一般'
        if r > 0.2:
            return '偏弱'
        return '弱封'
    return seal_ratio.apply(_label)


def calc_sector_leader_label(ranks: pd.Series, df: pd.DataFrame) -> pd.Series:
    """板块内角色标签"""
    industry_col = '所属行业'
    sizes = df.groupby(industry_col).size() if industry_col in df.columns else pd.Series(1, index=df.index)

    def _label(idx):
        rank = ranks.get(idx, 99)
        total = sizes.get(df.loc[idx, industry_col], 1) if industry_col in df.columns else 1
        if rank == 1 and total >= 3:
            return '龙头'
        if rank == 1:
            return '领涨'
        if rank <= 2:
            return '前排'
        return '跟风'
    return pd.Series({idx: _label(idx) for idx in df.index})


# ═══════════════════════════════════════════
#  慢指标（需要额外 API 调用获取历史数据）
# ═══════════════════════════════════════════

from concurrent.futures import ThreadPoolExecutor, as_completed

# 模块级缓存，避免同一进程内重复请求同一只股票的历史数据
_HIST_CACHE = {}

def _fetch_stock_hist(code, start_s, end_s):
    """获取单只股票历史数据（供并行用）"""
    try:
        hist = ak.stock_zh_a_hist(symbol=code, period='daily',
                                   start_date=start_s, end_date=end_s,
                                   adjust='qfq')
        return code, hist
    except Exception:
        return code, None


def _batch_fetch_hist(df, top_n=10):
    """并行获取 TOP N 股票历史数据，共享缓存。返回 {code: hist_df}"""
    end = date.today()
    start = end - timedelta(days=60)
    end_s = end.strftime('%Y%m%d')
    start_s = start.strftime('%Y%m%d')

    codes = []
    for idx in df.head(top_n).index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        codes.append(code)

    # 只请求缓存未命中的
    uncached = [c for c in codes if c not in _HIST_CACHE]
    if uncached:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(_fetch_stock_hist, c, start_s, end_s): c for c in uncached}
            for f in as_completed(futures):
                try:
                    c, hist = f.result()
                    if hist is not None and len(hist) >= 10:
                        _HIST_CACHE[c] = hist
                except Exception:
                    pass

    return {c: _HIST_CACHE[c] for c in codes if c in _HIST_CACHE}


def fetch_volume_ratio(df: pd.DataFrame, top_n=10) -> dict:
    """量比 = 当日成交量 / 5日均量（并行）"""
    if ak is None:
        return {}
    hists = _batch_fetch_hist(df, top_n)
    result = {}
    for code, hist in hists.items():
        try:
            if len(hist) >= 6:
                volumes = hist['成交量'].astype(float)
                today_v = volumes.iloc[-1]
                avg5 = volumes.iloc[-6:-1].mean()
                result[code] = round(today_v / avg5, 2) if avg5 > 0 else 1.0
        except Exception:
            pass
    return result


def fetch_position_type(df: pd.DataFrame, top_n=10) -> dict:
    """K线位置：底部首板 / 平台突破 / 高位加速（并行）"""
    if ak is None:
        return {}
    hists = _batch_fetch_hist(df, top_n)
    result = {}
    for code, hist in hists.items():
        try:
            closes = hist['收盘'].astype(float).values
            current = closes[-1]
            low_20 = closes[-20:].min() if len(closes) >= 20 else closes.min()
            high_20 = closes[-20:].max() if len(closes) >= 20 else closes.max()
            high_60 = closes.max()

            near_high_60 = current >= high_60 * 0.95
            near_high_20 = current >= high_20 * 0.95
            near_low_20 = current <= low_20 * 1.05
            pct_from_20low = (current - low_20) / low_20

            if near_high_60 and pct_from_20low > 0.3:
                result[code] = '高位加速'
            elif near_high_20 and pct_from_20low > 0.15:
                result[code] = '平台突破'
            elif near_low_20 or pct_from_20low < 0.05:
                result[code] = '底部首板'
            elif pct_from_20low > 0.2:
                result[code] = '趋势上行'
            else:
                result[code] = '震荡区间'
        except Exception:
            pass
    return result


# ═══════════════════════════════════════════
#  龙虎榜席位质量分析
# ═══════════════════════════════════════════

def grade_seat_name(name: str) -> str:
    """判断席位性质"""
    name = str(name)
    for kw in INSTITUTIONAL_KEYWORDS:
        if kw in name:
            return '机构'
    for kw in TOP_TOURIST_KEYWORDS:
        if kw in name:
            return '顶级游资'
    for kw in SUSPICIOUS_KEYWORDS:
        if kw in name:
            return '散户席位'
    return '普通游资'


def enhance_dragon_tiger(df_candidates: pd.DataFrame, today_str: str) -> dict:
    """增强龙虎榜分析：
    返回 {代码: {flag, detail, suspicion}} 字典
    flag: 机构主导/顶级游资/散户接盘/普通/未上榜
    """
    if ak is None:
        return {}
    result = {}
    candidate_codes = {}
    for idx in df_candidates.index:
        code = str(df_candidates.loc[idx, '代码']).strip().zfill(6)
        candidate_codes[code] = df_candidates.loc[idx, '名称']

    try:
        lhb = ak.stock_lhb_stock_statistic_em()
        if lhb is None or lhb.empty:
            return {}
    except Exception:
        return {}

    # 只取最新上榜日期的数据（避免混入历史数据）
    date_col = lhb.columns[3]
    latest_date = lhb[date_col].max()
    lhb = lhb[lhb[date_col] == latest_date]
    if lhb.empty:
        return {}

    for i in range(len(lhb)):
        code = str(lhb.iloc[i, 1]).strip().zfill(6)
        if code not in candidate_codes:
            continue

        buy_count = int(lhb.iloc[i, 11])    # 买方席位数
        sell_count = int(lhb.iloc[i, 12])   # 卖方席位数
        net_amount = float(lhb.iloc[i, 13]) # 净买入额
        total_buy = float(lhb.iloc[i, 14])  # 买入总额
        total_sell = float(lhb.iloc[i, 15]) # 卖出总额
        name = candidate_codes[code]

        entry = {'code': code, 'name': name}

        # ── 判断质量 ──
        if net_amount > 0 and buy_count <= 5:
            # 净买入且买方集中 → 可能是机构/游资主导
            if buy_count <= 3:
                entry['flag'] = '🏦 机构主导'
            else:
                entry['flag'] = '💰 游资主导'
            entry['detail'] = f'净买入{_fmt(net_amount)} 买方{buy_count}家'
            entry['quality'] = 'good'

        elif net_amount > 0 and buy_count > 10:
            # 净买入但买方极度分散 → 散户合力
            entry['flag'] = '⚠️ 散户合力'
            entry['detail'] = f'净买入{_fmt(net_amount)} 买方多达{buy_count}家(分散)'
            entry['quality'] = 'suspect'

        elif net_amount < -5e7:
            # 净卖出超过5000万 → 主力出货嫌疑
            entry['flag'] = '🚨 主力出货'
            entry['detail'] = f'净卖出{_fmt(abs(net_amount))} 卖方{sell_count}家'
            entry['quality'] = 'danger'

        elif net_amount < 0:
            entry['flag'] = '⚠️ 主力净卖'
            entry['detail'] = f'净卖出{_fmt(abs(net_amount))} 卖方{sell_count}家'
            entry['quality'] = 'warning'

        elif sell_count > buy_count * 2:
            # 卖方远多于买方 → 散货嫌疑
            entry['flag'] = '⚠️ 散货嫌疑'
            entry['detail'] = f'卖方{sell_count}家 vs 买方{buy_count}家'
            entry['quality'] = 'suspect'

        elif buy_count > 20:
            # 买方极度分散
            entry['flag'] = '⚠️ 散户接盘'
            entry['detail'] = f'买方多达{buy_count}家(高度分散)'
            entry['quality'] = 'suspect'

        else:
            entry['flag'] = '普通上榜'
            entry['detail'] = f'净买入{_fmt(net_amount)} 买{buy_count}/卖{sell_count}'
            entry['quality'] = 'neutral'

        result[code] = entry

    return result


def _fmt(val):
    if abs(val) >= 1e8:
        return f'{val/1e8:.2f}亿'
    if abs(val) >= 1e4:
        return f'{val/1e4:.0f}万'
    return f'{val:.0f}'


# ═══════════════════════════════════════════
#  聚合输出
# ═══════════════════════════════════════════

def format_indicator_section(df: pd.DataFrame, seal_ratios: pd.Series,
                              leadership: pd.Series, leader_labels: pd.Series,
                              vol_ratios: dict, pos_types: dict,
                              lhb_data: dict, sentiment_multiplier: float,
                              top_n=10, sorted_indices=None) -> str:
    """生成增强指标报告段落"""
    lines = ['', '=' * 60, '  增强指标 | 封成比·板块角色·量价关系', '=' * 60]
    indices = sorted_indices if sorted_indices is not None else df.head(top_n).index

    # 先统计龙虎榜风险标的集中展示
    danger_stocks = []
    for idx in indices:
        row = df.loc[idx]
        code = str(row.get('代码', '')).strip().zfill(6)
        lhb = lhb_data.get(code)
        if lhb and lhb.get('quality') in ('danger', 'warning', 'suspect'):
            danger_stocks.append((lhb['name'], code, lhb['flag'], lhb['detail']))

    if danger_stocks:
        lines.append('\n🚨 龙虎榜预警:')
        for name, code, flag, detail in danger_stocks:
            lines.append(f'  {flag} {name}({code}) — {detail}')

    # 个股增强指标 - 每个一行
    lines.append('\n📊 个股明细:')
    for rank, idx in enumerate(indices, 1):
        row = df.loc[idx]
        code = str(row.get('代码', '')).strip().zfill(6)
        name = row.get('名称', '')
        sr = seal_ratios.get(idx, 0)
        sq = calc_seal_quality_label(pd.Series({idx: sr}))[idx]
        lr = leader_labels.get(idx, '')
        lh_rank = leadership.get(idx, '')

        parts = [f'{rank}. {name}({code})']

        # 封成比
        parts.append(f'封成比{sr:.1f}({sq})')

        # 板块角色
        parts.append(f'板块第{lh_rank}({lr})')

        # 量比
        vr = vol_ratios.get(code)
        if vr is not None:
            vr_label = '放量' if vr > 2 else ('缩量' if vr < 0.8 else '正常')
            parts.append(f'量比{vr:.1f}({vr_label})')

        # K线位置
        pt = pos_types.get(code)
        if pt:
            parts.append(f'{pt}')

        lines.append(f'  {" | ".join(parts)}')

    lines.append('')
    return '\n'.join(lines)


def run_enhanced(df: pd.DataFrame, today_str: str, top_n=10,
                  sentiment_multiplier=1.0, total_scores=None):
    """主入口：执行所有增强分析"""
    # 按总分重排序，确保所有 head(top_n) 操作取到正确标的
    if total_scores is not None:
        sorted_indices = list(total_scores.sort_values(ascending=False).head(top_n).index)
        df_sorted = df.loc[sorted_indices]
    else:
        df_sorted = df
        sorted_indices = list(df.head(top_n).index)

    # 快指标（用排序后的df）
    seal_ratios = calc_seal_ratio(df_sorted)
    leadership = calc_sector_leadership(df_sorted)
    leader_labels = calc_sector_leader_label(leadership, df_sorted)

    # 慢指标（仅TOP N，用排序后的df_sorted确保正确）
    vol_ratios = fetch_volume_ratio(df_sorted, top_n)
    pos_types = fetch_position_type(df_sorted, top_n)

    # 龙虎榜（全量筛选，与排序无关）
    lhb_data = enhance_dragon_tiger(df, today_str)

    output = format_indicator_section(
        df, seal_ratios, leadership, leader_labels,
        vol_ratios, pos_types, lhb_data,
        sentiment_multiplier, top_n,
        sorted_indices=sorted_indices
    )
    return output, {
        'seal_ratios': seal_ratios,
        'leadership': leader_labels,
        'vol_ratios': vol_ratios,
        'pos_types': pos_types,
        'lhb_data': lhb_data,
    }
