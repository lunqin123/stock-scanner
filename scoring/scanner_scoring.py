"""scanner_scoring.py - 5 种扫描模式的专用纯评分函数

职责: 每个 scan_xxx 对应一个 score_xxx_data (无 IO 的纯函数),
      把"模式特定评分逻辑"集中在此层,便于回测和单元测试。
约束: 不依赖 scanner_scans / scanner_backtest;
      可依赖 utils / filters / data / factors / akshare / pandas。
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
import numpy as np
import pandas as pd

from scanner_utils import seal_time_score, _CST, TOP_N
from scanner_factors import get_sector_heat_scores, get_money_flow_scores
from scanner_data import fetch_fund_flow_data


# ═══════════════════════════════════════════
#  炸板反包评分 (5 因子可调权)
# ═══════════════════════════════════════════

def score_zhaban_data(df: pd.DataFrame, today_str: str, weights: dict = None,
                      fund_df: pd.DataFrame = None) -> pd.DataFrame:
    """炸板反包评分 (P5: 5因子可调权, v3.3d 优化)。

    支持传入历史存档的 fund_df（回测引擎从 archive 加载），
    避免回测时使用实时资金流数据产生未来偏差。

    v3.3d 优化: 权重从 IC-extreme (feature=35/turnover=35) 回退到平衡版,
    因为 IC 极端权重在实盘回测中恶化 (亏损加大)。新权重基于逻辑推理:
      - seal (封板质量): 25 — 封得好的票反包概率大 (炸板是意外, 不是本质弱)
      - money (资金承接): 20 — 资金流入的票有承接盘
      - feature (炸板特征): 20 — 中等换手+无极端 = 健康分歧
      - turnover (换手评分): 20 — 适中换手有利反包
      - sector (板块热度): 15 — 板块热+封板率高 → 板块支撑反包
    v3.3d 新增: v2 position_factor (过滤一日游, 偏好持续活跃的票)
    """
    df = df.copy()

    # v3.3d 平衡权重 (total=100, 直接映射)
    defaults = {'seal': 25, 'money': 20, 'feature': 20, 'turnover': 20, 'sector': 15}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})
    max_raw = sum(w.values())  # 用实际权重和归一化，防止权重膨胀导致分数溢出

    seal_time_col = '首次封板时间' if '首次封板时间' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)
    seal_fund_col = '封板资金' if '封板资金' in df.columns else (df.columns[14] if len(df.columns) > 14 else None)
    zhaban_count_col = '炸板次数' if '炸板次数' in df.columns else (df.columns[12] if len(df.columns) > 12 else None)
    turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    industry_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)

    # 1. 封板质量 (0-20)
    seal_scores = pd.Series(0.0, index=df.index)
    seal_scores += df[seal_time_col].apply(seal_time_score)
    fund_vals = df[seal_fund_col].fillna(0).astype(float)
    max_fund = fund_vals.max()
    if max_fund > 0: seal_scores += (fund_vals / max_fund) * 5
    else: seal_scores += 3
    zb_times = df[zhaban_count_col].fillna(0).astype(float)
    seal_scores += np.clip(1.0 - zb_times / 8.0, 0, 1) * 5
    f_seal = (seal_scores / 20).clip(0, 1)

    # 2. 资金承接 (0-20)
    # 优先用传入的历史 fund_df（回测时由引擎加载存档），避免未来数据偏差
    if fund_df is not None:
        fund_df_zb = fund_df
    else:
        fund_df_zb, _ = fetch_fund_flow_data()
    raw_money = pd.Series(0.0, index=df.index)
    if fund_df_zb is not None:
        money_scores, raw_money = get_money_flow_scores(df, fund_df=fund_df_zb)
    else:
        money_scores = np.clip(fund_vals / (fund_vals.max() + 1), 0, 1) * 10
        raw_money = fund_vals
    f_money = (money_scores / 20).clip(0, 1)

    # 3. 炸板特征 (0-15)
    turnover_vals = df[turnover_col].fillna(0).astype(float)
    feature = pd.Series(7.5, index=df.index)
    feature = feature + ((turnover_vals >= 10) & (turnover_vals <= 25)) * 5 + \
        (((turnover_vals >= 5) & (turnover_vals <= 30)) & ~((turnover_vals >= 10) & (turnover_vals <= 25))) * 2 - \
        (turnover_vals > 40) * 3
    f_feature = (feature.clip(0, 15) / 15)

    # 4. 换手率 (0-10)
    turn_scores = pd.Series(5.0, index=df.index)
    turn_scores = np.where((turnover_vals >= 8) & (turnover_vals <= 20), 10.0,
        np.where((turnover_vals >= 5) & (turnover_vals <= 30), 7.0,
        np.where(turnover_vals <= 3, 3.0, np.where(turnover_vals > 40, 2.0, 5.0))))
    f_turn = (turn_scores / 10).clip(0, 1)

    # 5. 板块热度 (0-12)
    try:
        limit_pool = ak.stock_zt_pool_em(date=today_str)
        if not limit_pool.empty:
            ind_col_l = '所属行业' if '所属行业' in limit_pool.columns else limit_pool.columns[15]
            counts = limit_pool[ind_col_l].value_counts()
            industries = df[industry_col] if industry_col in df.columns else df.iloc[:, 15]
            industry_counts = industries.map(counts).fillna(0)
            sector_raw = (4 + industry_counts * 2).clip(upper=12)
        else:
            sector_raw = get_sector_heat_scores(df, money_series=raw_money)
    except Exception:
        sector_raw = get_sector_heat_scores(df, money_series=raw_money)
    f_sector = (sector_raw / 12).clip(0, 1)

    total = (f_seal * w['seal'] + f_money * w['money'] + f_feature * w['feature'] +
             f_turn * w['turnover'] + f_sector * w['sector'])
    base_score = (total / max_raw * 100).clip(lower=0)

    # v3.3d: v2 position_factor 乘性调节 (持续性+回撤位置, 过滤一日游炸板)
    position_factor = pd.Series(1.0, index=df.index)
    mc_series = pd.Series(5.0, index=df.index)
    pd_series = pd.Series(5.0, index=df.index)
    if today_str is not None:
        try:
            from plans.factors_v2 import compute_v2_factors as _compute_v2
            v2 = _compute_v2(df, today_str)
            mc_series = v2['momentum_consistency']
            pd_series = v2['pullback_depth']
            mc_factor = 0.85 + mc_series / 50.0
            pd_factor = 0.90 + pd_series / 50.0
            position_factor = (mc_factor * pd_factor).clip(0.75, 1.20)
        except Exception:
            pass

    df['总分'] = (base_score * position_factor).clip(lower=0).round(1)
    df['zb_seal'] = (f_seal * w['seal']).round(1)
    df['封板质量'] = seal_scores.round(1)
    df['zb_money'] = (f_money * w['money']).round(1)
    df['zb_feature'] = (f_feature * w['feature']).round(1)
    df['zb_turnover'] = (f_turn * w['turnover']).round(1)
    df['zb_sector'] = (f_sector * w['sector']).round(1)
    df['资金承接'] = money_scores.round(1)
    df['炸板特征'] = feature.round(1)
    df['换手评分'] = turn_scores.round(1)
    df['板块热度'] = sector_raw.round(1)
    df['净流入'] = raw_money
    df['f_v2_mc'] = mc_series.round(1)
    df['f_v2_pd'] = pd_series.round(1)

    return df.sort_values('总分', ascending=False).head(TOP_N)


# ═══════════════════════════════════════════
#  反转评分 (4 因子可调权)
# ═══════════════════════════════════════════

def _score_reversal(pullback: pd.DataFrame, today_str: str = None, weights: dict = None) -> pd.DataFrame:
    """纯函数: 对"上交易日涨停今日下跌"的票做反转评分

    P5: 4因子可调权, 输出因子分列供IC分析。

    因子:
    - 连板位置 (0-30) — 连板越高，反包势能越强
    - 换手率 (0-25) — 低换手=惜售=筹码锁定好，高换手=抛压大
    - 回调深度 (0-25) — 深度回调才有反包空间
    - 板块支撑 (0-15) — 按板块涨停股数连续分档
    - 封板留存率 (0-5) — 板块涨停股留存率

    返回带因子分列 + '反转评分' 总分的 DataFrame。
    """
    if pullback is None or pullback.empty:
        return pullback

    # 原始权重 (回退 IC 优化)
    defaults = {'turnover': 25, 'consecutive': 30, 'pullback': 25, 'sector': 15, 'retention': 5}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})

    turnover_col = pullback.columns[9] if len(pullback.columns) > 9 else None
    seal_stat_col = pullback.columns[14] if len(pullback.columns) > 14 else None
    ind_col = pullback.columns[15] if len(pullback.columns) > 15 else None

    if '今日涨幅' not in pullback.columns:
        chg_col = pullback.columns[3]
        pullback['今日涨幅'] = pullback[chg_col].astype(float)

    # 因子原始分 (0-1 归一化)
    f_to = pd.Series(0.0, index=pullback.index)
    f_lb = pd.Series(0.0, index=pullback.index)
    f_chg = pd.Series(0.0, index=pullback.index)
    f_sector = pd.Series(0.0, index=pullback.index)
    f_retention = pd.Series(0.5, index=pullback.index)

    # 1. 换手率（符号修正：低换手=惜售=好，高换手=派发=差）
    if turnover_col:
        for idx in pullback.index:
            t = float(pullback.loc[idx, turnover_col]) if pd.notna(pullback.loc[idx, turnover_col]) else 0
            if t < 5:                f_to[idx] = 1.0   # 惜售，筹码锁定
            elif 5 <= t < 8:         f_to[idx] = 0.8
            elif 8 <= t < 15:        f_to[idx] = 0.5
            elif 15 <= t < 25:       f_to[idx] = 0.2
            else:                    f_to[idx] = 0.0   # >25%巨量换手=出货

    # 2. 连板位置（单调递增：连板越高分越高）
    if seal_stat_col:
        for idx in pullback.index:
            raw = str(pullback.loc[idx, seal_stat_col]) if pd.notna(pullback.loc[idx, seal_stat_col]) else ''
            consecutive = 0
            if '/' in raw:
                try:
                    consecutive = int(raw.split('/')[1])
                except Exception:  # BUG-5 修复
                    pass
            if consecutive >= 4:     f_lb[idx] = 1.0   # 高度连板龙头，反包势能最强
            elif consecutive == 3:   f_lb[idx] = 0.85
            elif consecutive == 2:   f_lb[idx] = 0.65
            elif consecutive == 1:   f_lb[idx] = 0.40
            else:                    f_lb[idx] = 0.20

    # 3. 回调深度 (v3.3d修复: 浅回调=洗盘=好, 深回调=出货=差)
    # 逻辑: -2%~-4%浅回调是健康的获利回吐, 筹码锁定好, 次日反包概率高
    #       -5%~-7%中等回调分歧加大, >-7%深度回调大概率是出货, 不应参与
    for idx in pullback.index:
        chg_val = pullback.loc[idx, '今日涨幅']
        if -4 <= chg_val <= -2:      f_chg[idx] = 1.0   # 浅回调洗盘: 最优
        elif -2 < chg_val <= 0:      f_chg[idx] = 0.85  # 几乎没跌: 强势整理
        elif -5 <= chg_val < -4:     f_chg[idx] = 0.7   # 中等回调: 可接受
        elif -6 <= chg_val < -5:     f_chg[idx] = 0.5   # 偏深: 谨慎
        elif -7 <= chg_val < -6:     f_chg[idx] = 0.3   # 深度回调: 风险大
        else:                        f_chg[idx] = 0.1   # < -7%: 大概率出货

    # 4. 板块支撑（按板块涨停股数连续分档）
    industry_counts = {}
    if today_str and ind_col:
        try:
            lt_today = ak.stock_zt_pool_em(date=today_str)
            if lt_today is not None and not lt_today.empty:
                lt_ind_col = '所属行业' if '所属行业' in lt_today.columns else (lt_today.columns[15] if len(lt_today.columns) > 15 else None)
                if lt_ind_col:
                    industry_counts = lt_today[lt_ind_col].value_counts().to_dict()
        except Exception:
            pass

    for idx in pullback.index:
        ind = str(pullback.loc[idx, ind_col]) if ind_col and pd.notna(pullback.loc[idx, ind_col]) else ''
        cnt = industry_counts.get(ind, 0) if industry_counts else 0
        if cnt >= 5:       f_sector[idx] = 1.0
        elif cnt >= 3:     f_sector[idx] = 0.8
        elif cnt >= 2:     f_sector[idx] = 0.6
        elif cnt >= 1:     f_sector[idx] = 0.4
        else:              f_sector[idx] = 0.2

    # 5. 封板留存率（板块涨停股留存率=持续性）
    if today_str and ind_col and industry_counts:
        try:
            prev_pool = ak.stock_zt_pool_previous_em(date=today_str)
            if prev_pool is not None and not prev_pool.empty:
                prev_ind_col = '所属行业' if '所属行业' in prev_pool.columns else (prev_pool.columns[15] if len(prev_pool.columns) > 15 else None)
                if prev_ind_col:
                    prev_counts = prev_pool[prev_ind_col].value_counts()
                    for idx in pullback.index:
                        ind = str(pullback.loc[idx, ind_col]) if pd.notna(pullback.loc[idx, ind_col]) else ''
                        today_c = industry_counts.get(ind, 0)
                        prev_c = prev_counts.get(ind, 0) if prev_counts is not None else 0
                        if prev_c > 0:
                            retention = today_c / prev_c
                            if retention >= 0.8:    f_retention[idx] = 1.0
                            elif retention >= 0.6:  f_retention[idx] = 0.8
                            elif retention >= 0.4:  f_retention[idx] = 0.6
                            elif retention >= 0.2:  f_retention[idx] = 0.4
                            else:                   f_retention[idx] = 0.2
                        else:
                            f_retention[idx] = 0.3  # 新板块，留存率未知
        except Exception:
            pass

    # 加权总分 (归一化到0-100)
    total = (f_to * w['turnover'] + f_lb * w['consecutive'] +
             f_chg * w['pullback'] + f_sector * w['sector'] +
             f_retention * w['retention'])
    weight_sum = sum(abs(v) for v in w.values())
    normalized = (total / max(weight_sum, 1) * 100) if weight_sum != 0 else total

    # v3.3d: v2 position_factor 乘性调节 (持续性+回撤位置, 过滤一日游反抽)
    position_factor = pd.Series(1.0, index=pullback.index)
    mc_series = pd.Series(5.0, index=pullback.index)
    pd_series = pd.Series(5.0, index=pullback.index)
    if today_str is not None:
        try:
            from plans.factors_v2 import compute_v2_factors as _compute_v2
            v2 = _compute_v2(pullback, today_str)
            mc_series = v2['momentum_consistency']
            pd_series = v2['pullback_depth']
            mc_factor = 0.85 + mc_series / 50.0
            pd_factor = 0.90 + pd_series / 50.0
            position_factor = (mc_factor * pd_factor).clip(0.75, 1.20)
        except Exception:
            pass

    pullback = pullback.copy()
    pullback['反转评分'] = (normalized * position_factor).clip(lower=0).round(1)
    pullback['rev_turnover'] = (f_to * w['turnover']).round(1)
    pullback['rev_consecutive'] = (f_lb * w['consecutive']).round(1)
    pullback['rev_pullback'] = (f_chg * w['pullback']).round(1)
    pullback['rev_sector'] = (f_sector * w['sector']).round(1)
    pullback['rev_retention'] = (f_retention * w['retention']).round(1)
    pullback['f_v2_mc'] = mc_series.round(1)
    pullback['f_v2_pd'] = pd_series.round(1)
    return pullback


# ═══════════════════════════════════════════
#  趋势动量评分 (5 因子可调权 + MA 回归)
# ═══════════════════════════════════════════

def _score_trend(df: pd.DataFrame, weights: dict = None, today_str: str = None) -> pd.DataFrame:
    """P2.2 抽出的纯函数: 趋势动量评分 (v3.3d 优化)

    6 因子 (0-100):
    - 涨幅分 (0-35): 3-5% 甜蜜区 (涨幅适中,趋势确认+还有空间)
    - 换手活跃分 (0-30): 8-15% 甜蜜区
    - 成交额分 (0-25): 越大关注度越高
    - 量比加分 (0-5): 强势池特有
    - 新高加分 (0-5): 创新高是强趋势信号
    - 均线回归 (0-0): 已关闭

    v3.3d: 涨幅甜蜜区从6-8%改为3-5% (高涨幅次日回调风险大, 适中涨幅趋势延续性强)
    v3.3d: 新高加分3→5, 新高是强趋势确认信号
    v3.3d: 新增 v2 position_factor (持续性+回撤位置, 过滤一日游)

    输入: 已过滤 (涨幅 2.5-8.5% + 非 ST + 市值<MAX_MARKET_CAP) 的 DataFrame
    输出: 加因子分列 + '动量评分' 总分的 DataFrame
    """
    if df is None or df.empty:
        return df

    # 默认权重 (v3.3d 优化: chg降权40→30, vol_ratio提权5→10, 量价配合确认趋势)
    defaults = {'chg': 30, 'turnover': 30, 'amount': 25, 'vol_ratio': 10, 'new_high': 5, 'ma_rev': 0}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})

    # 列识别 (强势池列名规范,做防御)
    change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
    turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
    vol_ratio_col = '量比' if '量比' in df.columns else None
    volume_col = '成交额' if '成交额' in df.columns else df.columns[6]
    new_high_col = '是否新高' if '是否新高' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)

    # 因子原始分 (0-1 归一化后再乘权重)
    f_chg = pd.Series(0.0, index=df.index)
    f_turnover = pd.Series(0.0, index=df.index)
    f_amount = pd.Series(0.0, index=df.index)
    f_vr = pd.Series(0.0, index=df.index)
    f_nh = pd.Series(0.0, index=df.index)

    # 1. 涨幅分 (v3.3d: 甜蜜区改为3-5%, 趋势确认+仍有空间)
    changes = df[change_col].astype(float)
    for idx in df.index:
        chg = float(changes[idx])
        if 3 <= chg <= 5:       f_chg[idx] = 1.0   # 甜蜜区: 趋势确认, 还有空间
        elif 5 < chg <= 6:      f_chg[idx] = 0.85
        elif 2.5 <= chg < 3:    f_chg[idx] = 0.75  # 刚启动
        elif 6 < chg <= 7:      f_chg[idx] = 0.65  # 偏高但可接受
        elif 7 < chg <= 8.5:    f_chg[idx] = 0.45  # 已大涨, 回调风险
        else:                   f_chg[idx] = 0.30

    # 2. 换手分
    turnovers = df[turnover_col].astype(float)
    for idx in df.index:
        t = float(turnovers[idx])
        if 8 <= t <= 15:        f_turnover[idx] = 1.0
        elif 5 <= t < 8:        f_turnover[idx] = 0.833
        elif 15 < t <= 20:      f_turnover[idx] = 0.667
        elif 3 <= t < 5:        f_turnover[idx] = 0.5
        elif 20 < t <= 25:      f_turnover[idx] = 0.333
        else:                   f_turnover[idx] = 0.167

    # 3. 成交额分 (percentile 归一化, p90=1.0 / p10=0.0)
    #   旧逻辑用 max 归一化 → 单只头部票(40亿成交)直接压制其他所有票, 30 张里 22 张 amount 分 < 0.2
    #   新逻辑用 [p10, p90] 区间 → 线性拉伸, 大多数票得到合理区分度
    volumes = df[volume_col].astype(float)
    p10 = volumes.quantile(0.10)
    p90 = volumes.quantile(0.90)
    if p90 > p10:
        f_amount = ((volumes - p10) / (p90 - p10)).clip(0, 1)
    else:
        f_amount = pd.Series(0.5, index=df.index)

    # 4. 量比加分
    if vol_ratio_col and vol_ratio_col in df.columns:
        vol_ratios = df[vol_ratio_col].astype(float)
        for idx in df.index:
            vr = float(vol_ratios[idx])
            if vr > 3:          f_vr[idx] = 1.0
            elif vr > 2:        f_vr[idx] = 0.6
            elif vr > 1.2:      f_vr[idx] = 0.2

    # 5. 新高加分
    if new_high_col and new_high_col in df.columns:
        for idx in df.index:
            if str(df.loc[idx, new_high_col]) == '是':
                f_nh[idx] = 1.0

    # 加权总分
    total = (f_chg * w['chg'] + f_turnover * w['turnover'] +
             f_amount * w['amount'] + f_vr * w['vol_ratio'] +
             f_nh * w['new_high'])

    # 写入因子列 (供回测相关性分析)
    df = df.copy()
    # 6. MA回归因子 (0-10): 偏离均线越远越容易回调
    f_ma = pd.Series(5.0, index=df.index)
    if w.get('ma_rev', 0) != 0:  # 权重非零才拉取MA数据(支持负权因子)
        code_col_ma = '代码' if '代码' in df.columns else df.columns[1]
        try:
            f_ma = _calc_ma_regression(df, code_col=code_col_ma)
        except Exception:
            pass

    total = (f_chg * w['chg'] + f_turnover * w['turnover'] +
             f_amount * w['amount'] + f_vr * w['vol_ratio'] +
             f_nh * w['new_high'] + f_ma * w.get('ma_rev', 0))

    # 归一化到0-100 (除以当前权重绝对值总和, 支持负权)
    weight_sum = sum(abs(v) for v in w.values())
    normalized = (total / max(weight_sum, 1) * 100) if weight_sum != 0 else total

    # v3.3d: v2 position_factor 乘性调节 (持续性+回撤位置, 过滤一日游)
    position_factor = pd.Series(1.0, index=df.index)
    mc_series = pd.Series(5.0, index=df.index)
    pd_series = pd.Series(5.0, index=df.index)
    if today_str is not None:
        try:
            from plans.factors_v2 import compute_v2_factors as _compute_v2
            v2 = _compute_v2(df, today_str)
            mc_series = v2['momentum_consistency']
            pd_series = v2['pullback_depth']
            mc_factor = 0.85 + mc_series / 50.0
            pd_factor = 0.90 + pd_series / 50.0
            position_factor = (mc_factor * pd_factor).clip(0.75, 1.20)
        except Exception:
            pass

    df = df.copy()
    df['动量评分'] = (normalized * position_factor).clip(lower=0).round(1)
    df['trend_chg'] = (f_chg * w['chg']).round(1)
    df['trend_turnover'] = (f_turnover * w['turnover']).round(1)
    df['trend_amount'] = (f_amount * w['amount']).round(1)
    df['trend_vr'] = (f_vr * w['vol_ratio']).round(1)
    df['trend_nh'] = (f_nh * w['new_high']).round(1)
    df['trend_ma'] = (f_ma * w.get('ma_rev', 0)).round(1)
    df['f_v2_mc'] = mc_series.round(1)
    df['f_v2_pd'] = pd_series.round(1)

    return df


def _calc_ma_regression(df: pd.DataFrame, code_col: str = None) -> pd.Series:
    """计算MA回归因子: 当前价 vs 5日/10日均线的偏离度

    偏离越小(贴近均线) → 分数越高 (趋势健康)
    偏离越大(远离均线) → 分数越低 (超买回调风险)

    返回 0-10 的 Series
    """
    if df is None or df.empty:
        return pd.Series(0.0, index=df.index)

    code_col = code_col or ('代码' if '代码' in df.columns else df.columns[1])
    codes = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        if len(code) == 6:
            codes.append(code)

    if not codes:
        return pd.Series(0.0, index=df.index)

    today = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')
    prices = {}

    def _fetch(code):
        try:
            hist = ak.stock_zh_a_hist(symbol=code, period='daily',
                                       start_date=start, end_date=today,
                                       adjust='qfq')
            if hist is not None and not hist.empty and len(hist) >= 5:
                closes = hist['收盘'].astype(float).values
                return code, closes
        except Exception:
            pass
        return code, None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_fetch, c): c for c in codes}
        for f in as_completed(futures):
            code, closes = f.result()
            if closes is not None and len(closes) >= 5:
                prices[code] = closes

    scores = pd.Series(0.0, index=df.index)
    for idx in df.index:
        code = str(df.loc[idx, code_col]).strip().zfill(6)
        closes = prices.get(code)
        if closes is None or len(closes) < 5:
            scores[idx] = 5.0  # 无数据给中性分
            continue

        current = closes[-1]
        ma5 = closes[-5:].mean()
        ma10 = closes[-10:].mean() if len(closes) >= 10 else ma5

        # 偏离度: (当前价/MA - 1) * 100
        dev5 = (current / ma5 - 1) * 100
        dev10 = (current / ma10 - 1) * 100
        dev = max(dev5, dev10)  # 取最大偏离

        # 评分: 偏离<2%→满分; 2-5%→递减; 5-8%→低分; >8%→0
        if dev <= 2:
            scores[idx] = 10.0
        elif dev <= 5:
            scores[idx] = 10 - (dev - 2) * 2     # 2%→10, 5%→4
        elif dev <= 8:
            scores[idx] = max(0, 4 - (dev - 5))   # 5%→4, 8%→1
        else:
            scores[idx] = 0

    return scores.round(1)


# ═══════════════════════════════════════════
#  板块联动评分
# ═══════════════════════════════════════════

def score_sector_data(limit_df: pd.DataFrame, zhaban_df: pd.DataFrame,
                      dieting_df: pd.DataFrame, top_n: int = TOP_N) -> list:
    """板块联动强度纯评分（无print）。Web card端点和CLI共享。
    输入三个涨停/炸板/跌停DataFrame，返回按联动强度排序的板块列表。"""
    # 涨停行业分布
    limit_counts = {}
    if not limit_df.empty:
        ind_col = '所属行业' if '所属行业' in limit_df.columns else (limit_df.columns[15] if len(limit_df.columns) > 15 else None)
        if ind_col: limit_counts = limit_df[ind_col].value_counts().to_dict()
    # 炸板行业分布
    zhaban_counts = {}
    if not zhaban_df.empty:
        ind_col2 = '所属行业' if '所属行业' in zhaban_df.columns else (zhaban_df.columns[15] if len(zhaban_df.columns) > 15 else None)
        if ind_col2: zhaban_counts = zhaban_df[ind_col2].value_counts().to_dict()
    # 跌停行业分布
    dieting_counts = {}
    if not dieting_df.empty:
        ind_col3 = '所属行业' if '所属行业' in dieting_df.columns else (dieting_df.columns[15] if len(dieting_df.columns) > 15 else None)
        if ind_col3: dieting_counts = dieting_df[ind_col3].value_counts().to_dict()

    all_industries = set(list(limit_counts.keys()) + list(zhaban_counts.keys()) + list(dieting_counts.keys()))
    if not all_industries: return []

    stats = []
    for industry in all_industries:
        lc = limit_counts.get(industry, 0)
        zc = zhaban_counts.get(industry, 0)
        dc = dieting_counts.get(industry, 0)
        total = lc + zc + dc
        stats.append({
            'industry': industry,
            'limit_cnt': lc, 'zhaban_cnt': zc, 'dieting_cnt': dc,
            'link_strength': round(lc - zc * 0.3 - dc * 0.5, 1),
            'profit_effect': round(lc / total * 100, 0) if total > 0 else 0,
            'seal_rate': round(lc / (lc + zc) * 100, 0) if (lc + zc) > 0 else 50,
        })
    stats.sort(key=lambda x: x['link_strength'], reverse=True)
    return stats[:top_n]


def _get_sector_stocks(industry: str, limit_df: pd.DataFrame, zhaban_df: pd.DataFrame,
                       dieting_df: pd.DataFrame = None) -> list:
    """给定板块名,返回该板块所有涨停+炸板个股的 code/name 列表

    用于 P2.3 板块 tab 回测: 板块级 → 个股级映射
    返回: [{'code': '000001', 'name': '平安银行', 'source': 'limit'}, ...]
    """
    stocks = []
    seen = set()
    ind_col = None

    for source_name, df in [('limit', limit_df), ('zhaban', zhaban_df), ('dieting', dieting_df)]:
        if df is None or df.empty:
            continue
        if ind_col is None or ind_col not in df.columns:
            ind_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)
        if ind_col is None or ind_col not in df.columns:
            continue
        for _, row in df.iterrows():
            if str(row.get(ind_col, '')) != industry:
                continue
            code = str(row.get('代码', '') or row.iloc[1]).strip().zfill(6)
            if not code or code in seen:
                continue
            seen.add(code)
            stocks.append({
                'code': code,
                'name': str(row.get('名称', '') or row.iloc[2]),
                'source': source_name,
            })
    return stocks


def _score_sector(date_str: str, top_n: int = TOP_N) -> pd.DataFrame:
    """P2.3 板块 tab 回测评分: 板块级 → 个股级映射

    流程:
    1. 拉当日涨停+炸板+跌停池
    2. score_sector_data 算板块联动强度
    3. 每个板块的 TOP 个股按板块强度打"板块强度分"
    4. 输出: 个股级 DataFrame,含 '板块强度' 列

    注: 板块级回测策略 = 板块 TOP1 的所有涨停个股等权买 → D+1 算收益
    """
    # 拉数据
    try:
        limit_df = ak.stock_zt_pool_em(date=date_str)
    except Exception:
        limit_df = pd.DataFrame()
    try:
        zhaban_df = ak.stock_zt_pool_zbgc_em(date=date_str)
    except Exception:
        zhaban_df = pd.DataFrame()
    try:
        dieting_df = ak.stock_zt_pool_dtgc_em(date=date_str)
    except Exception:
        dieting_df = pd.DataFrame()

    # 板块评分
    sectors = score_sector_data(limit_df, zhaban_df, dieting_df, top_n=top_n)
    if not sectors:
        return pd.DataFrame()

    # 板块 → 个股
    rows = []
    for sector in sectors:
        industry = sector['industry']
        link = sector['link_strength']
        stocks = _get_sector_stocks(industry, limit_df, zhaban_df, dieting_df)
        # 按 link_strength 排序后分配: 板块强度分 = link_strength * 10 (clip 0-100)
        sector_score = max(0, min(100, int(link * 10 + 50)))
        for stock in stocks:
            rows.append({
                '代码': stock['code'],
                '名称': stock['name'],
                '所属行业': industry,
                '板块强度': sector_score,
                'link_strength': link,
                '_source': stock['source'],
                'limit_cnt': sector['limit_cnt'],
                'zhaban_cnt': sector['zhaban_cnt'],
                'dieting_cnt': sector['dieting_cnt'],
                'profit_effect': sector['profit_effect'],
                'seal_rate': sector['seal_rate'],
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════
#  跌停翘板评分 (5 因子可调权)
# ═══════════════════════════════════════════

def score_dtqiaoban_data(df: pd.DataFrame, weights: dict = None, today_str: str = None) -> pd.DataFrame:
    """翘板反抽评分 (P5: 5因子可调权, v3.3d 优化)。

    v3.3d: 新增 today_str 参数和 v2 position_factor (偏好持续活跃、高位回撤的票)
    """
    df = df.copy()
    # v3.3d 微调权重: cont(连跌)提权25→30, time(时间)降权10→5
    defaults = {'deal': 25, 'seal': 25, 'cont': 30, 'turnover': 15, 'time': 5}
    w = dict(defaults)
    if weights:
        w.update({k: v for k, v in weights.items() if k in defaults})
    max_raw = sum(w.values())  # 用实际权重和归一化，防止权重膨胀导致分数溢出
    # 列识别
    deal_col = df.columns[12] if len(df.columns) > 12 else None
    seal_fund_col = df.columns[10] if len(df.columns) > 10 else None
    cont_dieting_col = df.columns[13] if len(df.columns) > 13 else None
    turnover_col = df.columns[9] if len(df.columns) > 9 else None
    seal_time_col = df.columns[11] if len(df.columns) > 11 else None

    f_deal = pd.Series(0.2, index=df.index)
    f_seal = pd.Series(0.5, index=df.index)
    f_cont = pd.Series(0.2, index=df.index)
    f_turn = pd.Series(0.1, index=df.index)
    f_time = pd.Series(0.3, index=df.index)
    for idx in df.index:
        if deal_col is not None:
            dv = float(df.loc[idx, deal_col]) if pd.notna(df.loc[idx, deal_col]) else 0
            if dv > 3000e4: f_deal[idx] = 1.0
            elif dv > 1000e4: f_deal[idx] = 0.8
            elif dv > 500e4: f_deal[idx] = 0.6
            elif dv > 100e4: f_deal[idx] = 0.4
            else: f_deal[idx] = 0.2
        if seal_fund_col is not None:
            sv = float(df.loc[idx, seal_fund_col]) if pd.notna(df.loc[idx, seal_fund_col]) else 0
            if sv < 100e4: f_seal[idx] = 1.0
            elif sv < 1000e4: f_seal[idx] = 0.8
            elif sv < 5000e4: f_seal[idx] = 0.4
            else: f_seal[idx] = 0.12
        if cont_dieting_col is not None:
            cv = int(float(df.loc[idx, cont_dieting_col])) if pd.notna(df.loc[idx, cont_dieting_col]) else 0
            if cv >= 5: f_cont[idx] = 1.0
            elif cv == 4: f_cont[idx] = 0.9
            elif cv == 3: f_cont[idx] = 0.8
            elif cv == 2: f_cont[idx] = 0.6
            elif cv == 1: f_cont[idx] = 0.4
            else: f_cont[idx] = 0.2
        if turnover_col is not None:
            tv = float(df.loc[idx, turnover_col]) if pd.notna(df.loc[idx, turnover_col]) else 0
            if tv > 15: f_turn[idx] = 1.0
            elif tv > 10: f_turn[idx] = 0.8
            elif tv > 5: f_turn[idx] = 0.6
            elif tv > 3: f_turn[idx] = 0.4
            elif tv > 1: f_turn[idx] = 0.2
            else: f_turn[idx] = 0.0
        if seal_time_col is not None:
            t = str(df.loc[idx, seal_time_col]).strip()
            if len(t) >= 4:
                minutes = int(t[:2]) * 60 + int(t[2:4])
                if minutes >= 840: f_time[idx] = 1.0
                elif minutes >= 750: f_time[idx] = 0.5
                else: f_time[idx] = 0.2
    total = (f_deal*w['deal'] + f_seal*w['seal'] + f_cont*w['cont'] + f_turn*w['turnover'] + f_time*w['time'])
    base_score = (total / max_raw * 100).clip(lower=0)

    # v3.3d: v2 position_factor 乘性调节 (持续性+回撤位置, 偏好持续活跃的票)
    position_factor = pd.Series(1.0, index=df.index)
    mc_series = pd.Series(5.0, index=df.index)
    pd_series = pd.Series(5.0, index=df.index)
    if today_str is not None:
        try:
            from plans.factors_v2 import compute_v2_factors as _compute_v2
            v2 = _compute_v2(df, today_str)
            mc_series = v2['momentum_consistency']
            pd_series = v2['pullback_depth']
            mc_factor = 0.85 + mc_series / 50.0
            pd_factor = 0.90 + pd_series / 50.0
            position_factor = (mc_factor * pd_factor).clip(0.75, 1.20)
        except Exception:
            pass

    df['翘板评分'] = (base_score * position_factor).clip(lower=0).round(1)
    df['dt_deal'] = (f_deal*w['deal']).round(1)
    df['dt_seal'] = (f_seal*w['seal']).round(1)
    df['dt_cont'] = (f_cont*w['cont']).round(1)
    df['dt_turnover'] = (f_turn*w['turnover']).round(1)
    df['dt_time'] = (f_time*w['time']).round(1)
    df['f_v2_mc'] = mc_series.round(1)
    df['f_v2_pd'] = pd_series.round(1)
    return df.sort_values('翘板评分', ascending=False).head(TOP_N)
