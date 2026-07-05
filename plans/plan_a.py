#!/usr/bin/env python3
"""
Plan A — 9因子加权 + 本金适配 + 危险信号检测

评分链路：
  1. 因子计算: seal/money/sector/tech/buyability/stock_sentiment/principal/history
  2. apply_weights: 9因子归一化 + 大盘情绪温和系数(×0.85~1.15)
  3. score_danger_signals: 交叉惩罚(虚板/三无/控盘等)
  4. build_stocks: 组装前端卡片数据 + 竞价验证条件
"""

PLAN_NAME = "A"
PLAN_DESC = "9因子加权+本金适配+危险信号"
PLAN_SOURCES = []  # Plan A 只用 akshare 基础数据, 无需扩展源

import pandas as pd
import sys
import threading


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _gen_auction_check(row, idx, sector_mom, money_scores, filtered, pool=None):
    """生成次日竞价验证条件（板块龙头=同行业最高连板+最早封板，用原始池找）"""
    st = str(row.get('首次封板时间', ''))[:4]
    try: turnover = float(row.get('换手率', 10))
    except: turnover = 10.0
    sm = float(sector_mom.get(idx, 8))
    mn = float(money_scores.get(idx, 5))
    parts = []
    if st and int(st[:2]) < 10: parts.append("高开5-7%")
    elif st and int(st[:2]) < 11: parts.append("高开3-5%")
    elif st and int(st[:2]) < 13: parts.append("高开2-3%")
    else: parts.append("平开或高开1-2%")
    if turnover > 15: parts.append("竞价量>上交易日成交8%")
    elif turnover > 5: parts.append("竞价量>上交易日成交5%")
    else: parts.append("竞价量>上交易日成交3%")
    # ── 板块双龙头: 情绪龙头(连板最高,游资) + 中军龙头(大市值,机构) ──
    if sm >= 10 and pool is not None and not pool.empty:
        ind_col = '所属行业' if '所属行业' in pool.columns else (pool.columns[15] if len(pool.columns) > 15 else None)
        lb_col = '连板数' if '连板数' in pool.columns else (pool.columns[14] if len(pool.columns) > 14 else None)
        st_col = '首次封板时间' if '首次封板时间' in pool.columns else pool.columns[11]
        cap_col = '流通市值' if '流通市值' in pool.columns else None
        if ind_col and lb_col and st_col:
            industry = str(row.get(ind_col, ''))
            if not industry:
                ind_col2 = '所属行业' if '所属行业' in filtered.columns else filtered.columns[15]
                industry = str(row.get(ind_col2, ''))
            if industry:
                same = pool[pool[ind_col].astype(str) == industry].copy()
                if len(same) >= 2:
                    same['_lb'] = same[lb_col].fillna(1).astype(float)
                    same['_st'] = same[st_col].fillna('9999').astype(str)
                    same['_st_min'] = same['_st'].apply(lambda t: int(t[:2])*60+int(t[2:4]) if len(str(t))>=4 else 9999)
                    if cap_col:
                        same['_cap'] = same[cap_col].fillna(0).astype(float)
                    else:
                        same['_cap'] = 0
                    # 1) 情绪龙头: 连板最高+封板最早+跟风验证(市值通常<200亿)
                    qx = same.copy()
                    candidates = qx.sort_values(['_lb', '_st_min'], ascending=[False, True])
                    emo_leader, is_emo = None, False
                    for ci in candidates.index:
                        c = candidates.loc[ci]
                        followers = sum(1 for i in same.index
                                       if i != ci and int(same.loc[i, '_st_min']) > int(c['_st_min']))
                        if followers >= 2 or float(c['_lb']) >= 3:
                            emo_leader, is_emo = c, True; break
                    if emo_leader is None:
                        emo_leader = candidates.iloc[0]
                        is_emo = len(same) >= 2
                    # 2) 中军龙头: 板块内大市值(>100亿)+趋势强(连板或涨幅)
                    big = same[same['_cap'] > 100 * 1e8].copy()
                    jun_leader = None
                    if not big.empty:
                        big = big.sort_values('_cap', ascending=False)
                        jun_leader = big.iloc[0]  # 板块市值最大的票
                    # 输出
                    m_code = str(row.get('代码', '')).strip().zfill(6)
                    el_code = str(emo_leader.get('代码', '')).strip().zfill(6)
                    el_name = str(emo_leader.get('名称', ''))
                    el_lb = int(float(emo_leader.get('_lb', 1)))
                    # 情绪龙头条件 (橙色高亮名字)
                    if el_code == m_code:
                        parts.append("情绪龙(<span class=\"ld-emo\">" + str(el_lb) + "连板</span>)自身竞价不绿，高开3-7%确认")
                    else:
                        parts.append("情绪龙<span class=\"ld-emo\">" + el_name + "</span>(" + el_code + " " + str(el_lb) + "连板)竞价不绿")
                    # 中军龙头条件 (蓝色高亮名字)
                    if jun_leader is not None:
                        jl_code = str(jun_leader.get('代码', '')).strip().zfill(6)
                        jl_name = str(jun_leader.get('名称', ''))
                        jl_cap = float(jun_leader.get('_cap', 0)) / 1e8
                        if jl_code != el_code:  # 不同票才显示
                            if jl_code == m_code:
                                parts.append("自身为中军<span class=\"ld-jun\">(" + str(int(jl_cap)) + "亿)</span>")
                            else:
                                parts.append("中军<span class=\"ld-jun\">" + jl_name + "</span>(" + jl_code + " " + str(int(jl_cap)) + "亿)趋势不破")
    if mn >= 10: parts.append("竞价无大单净流出")
    elif mn <= 3: parts.append("竞价放量确认,否则放弃")
    return "；".join(parts)


# ═══════════════════════════════════════════
#  因子计算
# ═══════════════════════════════════════════

def compute_factors(filtered, fund_df, principal):
    """计算所有因子得分。返回 dict[str, pd.Series]"""
    from scanner import (score_seal_strength, get_money_flow_scores,
                         get_sector_heat_scores, score_tech_form, score_buyability,
                         score_stock_sentiment, score_by_principal, get_sector_resonance)

    degraded = fund_df is None

    seal = score_seal_strength(filtered)
    if not degraded:
        money, raw_money = get_money_flow_scores(filtered, fund_df=fund_df)
    else:
        money = pd.Series(0.0, index=filtered.index)
        raw_money = pd.Series(0.0, index=filtered.index)
    sector_mom = get_sector_heat_scores(filtered, money_series=raw_money if not degraded else None)
    sector_res = get_sector_resonance(filtered)
    tech = score_tech_form(filtered)
    buyability = score_buyability(filtered)
    stock_sent = score_stock_sentiment(filtered, money, buyability)
    # 舆情分数并入个股情绪 (30%权重，非阻塞)
    # P0-3 修复: 舆情缺失时填 5.0(中性分),而非 fillna(stock_sent) — 后者导致加权完全失效
    # (用自身值填充再加权 = (x*0.7 + x*0.3) = x, 舆情分数实质未生效)
    try:
        import stock_community
        comm_scores = stock_community.score_community(filtered)
        if comm_scores is not None and not comm_scores.empty:
            comm_aligned = comm_scores.reindex(stock_sent.index).fillna(5.0)
            stock_sent = (stock_sent * 0.7 + comm_aligned / 7.0 * 10.0 * 0.3).clip(0, 10)
    except Exception as e:
        print(f"  [舆情] 不可用: {e}", file=sys.stderr)  # 舆情不可用时不改变 stock_sent
    pr = score_by_principal(filtered, principal)

    # v2.0: 北向资金因子 (市场级，所有标的统一分)
    north_flow = pd.Series(5.0, index=filtered.index)
    try:
        from north_flow_tracker import score_north_flow_factor
        nf_scores, nf_meta = score_north_flow_factor(filtered)
        north_flow = nf_scores
        print(f"  [Plan A] 北向因子: {nf_meta.get('north_direction', 'N/A')}, "
              f"累计{nf_meta.get('north_cumulative_net', 0):+.1f}亿", file=sys.stderr)
    except Exception as e:
        print(f"  [Plan A] 北向因子获取异常: {e}", file=sys.stderr)

    # v3.1: GTJA Alpha 5 因子组合
    alpha = pd.Series(5.0, index=filtered.index)
    try:
        from scoring.scanner_factors import score_alpha_factors
        alpha_scores = score_alpha_factors(scoring_base, today_fmt)
        alpha = alpha_scores.reindex(filtered.index, fill_value=5.0)
    except Exception as e:
        print(f"  [Plan A] Alpha因子跳过: {e}", file=sys.stderr)

    return {
        'seal': seal, 'money': money, 'raw_money': raw_money,
        'sector_mom': sector_mom, 'sector_res': sector_res,
        'tech': tech, 'buyability': buyability,
        'stock_sentiment': stock_sent, 'principal': pr,
        'north_flow': north_flow,
        'alpha': alpha,
    }


def reindex_factors(factors, idx):
    """将所有因子 reindex 到新索引（过滤后使用）"""
    return {k: v.loc[idx] if hasattr(v, 'loc') and len(v) > 0 else v for k, v in factors.items()}


# ═══════════════════════════════════════════
#  评分聚合
# ═══════════════════════════════════════════

def apply_scores(filtered, factors, sentiment_score, history_scores, lhb_bonus, today_str,
                  weights=None, use_v2=True):
    """
    因子加权 + 大盘情绪系数 + 危险信号惩罚。
    返回 (total_scores, base_scores, danger_flags, weights)
    weights: 可选，传入自定义权重字典，None 时从 weight_manager 加载全局权重
    """
    import weight_manager

    # BUG-3 修复: lhb_bonus 不再混入 money (clip(20) 会损失 5 分 lhb 信号)
    # 原代码: money = (money + lhb).clip(upper=20) — money=20 + lhb=5 → 25 → 20, 损失 5
    # 新方案: money 保持原样, lhb_bonus 作为独立加性微调 (缩放 50%, 范围 [-2, +2.5])
    money = factors['money']
    if hasattr(lhb_bonus, 'loc') and not lhb_bonus.empty:
        lhb_series = lhb_bonus.loc[filtered.index].reindex(money.index, fill_value=0)
        lhb_adjust = lhb_series * 0.5  # 缩放 50%, -4~+5 → -2~+2.5
    else:
        lhb_adjust = pd.Series(0.0, index=money.index)

    sentiment_series = pd.Series(float(sentiment_score), index=filtered.index)
    h_scores = history_scores.loc[filtered.index] if hasattr(history_scores, 'loc') and len(filtered.index) > 0 \
               else pd.Series(2.5, index=filtered.index)

    if weights is None:
        weights = weight_manager.load_weights()
    # P1-1 修复: sector 因子直接用 sector_mom(superset),不再平均 sector_res
    # 原代码 (sector_res + sector_mom)/2.0 让 sector_mom 满 15 分被压到 11.5,
    # 权重 17 实际只发挥 13 分的威力(76.5%)。
    # sector_mom 已经含 sector_res 的所有信息(板块涨停数)+资金一致性+ETF共振
    sector_scores = factors['sector_mom']
    base = weight_manager.apply_weights(
        factors['seal'], money, sector_scores,
        factors['tech'], h_scores,
        sentiment_series,
        stock_sentiment_scores=factors['stock_sentiment'],
        principal_scores=factors['principal'],
        north_flow_scores=factors.get('north_flow'),
        alpha_scores=factors.get('alpha'),
        weights=weights)

    from scanner import score_danger_signals
    danger_penalty, danger_flags = score_danger_signals(filtered, factors['raw_money'], today_str)
    # P1-2 修复: 危险信号改乘性惩罚,避免加性 clip -30 导致烂票均匀对待、失去区分度
    # penalty 范围 -30~0 → factor 范围 0.7~1.0(最多扣 30%,保留 70% 底)
    danger_factor = 1.0 + danger_penalty / 100.0  # -30→0.7, -15→0.85, -5→0.95, 0→1.0

    # v2.0 新增: 持续性 + 回撤位置 乘性因子
    # 设计目的: 解决"评分高=追高陷阱"问题
    #   - momentum_consistency (0-10): mc=0 → factor 0.85 (扣 15%), mc=10 → factor 1.05 (加 5%)
    #     → 持续强势的票小幅加分, 一日游的票扣分
    #   - pullback_depth (0-10): pd=0 → factor 0.90 (扣 10%), pd=10 → factor 1.10 (加 10%)
    #     → 高位强势 / 刚回踩后回升的票加分, 深度回撤中的票扣分
    # 无历史数据时 mc/pd=5.0 → factor=1.0 (中性, 不影响)
    mc_series = factors.get('momentum_consistency', pd.Series(5.0, index=filtered.index))
    pd_series = factors.get('pullback_depth', pd.Series(5.0, index=filtered.index))
    if use_v2:
        mc_factor = 0.85 + mc_series / 50.0      # mc=10→1.05, mc=0→0.85, mc=5→0.95
        pd_factor = 0.90 + pd_series / 50.0      # pd=10→1.10, pd=0→0.90, pd=5→1.00
        position_factor = mc_factor * pd_factor   # 总范围 0.765~1.155
    else:
        position_factor = 1.0  # 老评分模式,无 v2 影响
    # BUG-3 修复: lhb_bonus 独立加性微调,在 danger 之后叠加
    total = ((base + lhb_adjust) * danger_factor * position_factor).clip(lower=0)

    # 后台回测
    try:
        from scanner import auto_verify_backtest
        threading.Thread(target=lambda: auto_verify_backtest(
            today_str, current_weights=weights, plan_name='A'), daemon=True).start()
    except Exception as e:
        print(f"  [回测] 启动失败: {e}", file=sys.stderr)

    return total, base, danger_flags, weights


# ═══════════════════════════════════════════
#  组装前端数据
# ═══════════════════════════════════════════

def build_stocks(filtered, factors, total_scores, base_scores, danger_flags,
                 sentiment_score, history_scores, pool=None, max_n: int = None):
    """组装 stocks 列表，供前端卡片渲染

    Args:
        max_n: 输出票数上限 (None = 用全局 TOP_N)
    """
    from scanner import money_str, TOP_N
    n = max_n if max_n is not None else TOP_N

    top_indices = list(total_scores.sort_values(ascending=False).head(n).index)

    stocks = []
    for rank, idx in enumerate(top_indices, 1):
        row = filtered.loc[idx]
        code = str(row.get('代码', '')).strip().zfill(6)
        name = str(row.get('名称', ''))
        net = float(factors['raw_money'].get(idx, 0))
        stocks.append({
            'rank': rank, 'code': code, 'name': name,
            'total_score': round(float(total_scores[idx]), 1),
            'base_score': round(float(base_scores[idx]), 1),
            'seal_score': round(float(factors['seal'].get(idx, 0)), 1),
            'money_score': round(float(factors['money'].get(idx, 0)), 1),
            'sector_mom': round(float(factors['sector_mom'].get(idx, 0)), 1),
            'sector_res': round(float(factors['sector_res'].get(idx, 0)), 1),
            'sector_score': round(float(factors['sector_mom'].get(idx, 0) + factors['sector_res'].get(idx, 0)), 1),
            'tech_score': round(float(factors['tech'].get(idx, 0)), 1),
            'history_score': round(float(history_scores.get(idx, 2.5)), 1),
            'sentiment_score': sentiment_score,
            'buyability_score': round(float(factors['buyability'].get(idx, 5)), 1),
            'stock_sentiment_score': round(float(factors['stock_sentiment'].get(idx, 5)), 1),
            'principal_score': round(float(factors['principal'].get(idx, 5)), 1),
            'north_flow_score': round(float(factors.get('north_flow', pd.Series(5.0, index=factors['seal'].index)).get(idx, 5)), 1),
            'momentum_consistency': round(float(factors.get('momentum_consistency', pd.Series(5.0, index=factors['seal'].index)).get(idx, 5)), 1),
            'pullback_depth': round(float(factors.get('pullback_depth', pd.Series(5.0, index=factors['seal'].index)).get(idx, 5)), 1),
            'danger_flags': danger_flags.get(idx, []),
            'auction_check': _gen_auction_check(row, idx, factors['sector_mom'], factors['money'], filtered, pool),
            'net_money': net,
            'net_money_str': money_str(net),
            'turnover': f"{float(row.get('换手率', 0)):.1f}",
            'seal_time': str(row.get('首次封板时间', ''))[:4],
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
        })
    return stocks


# ═══════════════════════════════════════════
#  主入口: score()
#  app.py 唯一需要调用的函数
# ═══════════════════════════════════════════

def score(inputs: dict, max_n: int = None, use_v2: bool = True) -> dict:
    """
    Plan A 评分主入口。

    inputs 必须包含:
        filtered       — 过滤后的 DataFrame（最终输出的股票集）
        scoring_base   — 因子归一化基准集（>=filtered，保证归一化稳定）
        fund_df        — 资金流 DataFrame 或 None
        sentiment_score    — float
        sentiment_level    — str
        sentiment_detail   — dict
        sentiment_ok       — bool
        history_scores     — pd.Series
        lhb_bonus          — pd.Series
        today_str          — str (YYYY-MM-DD)
        pool               — 原始涨停池 DataFrame（供龙头检测）
        principal          — float (本金)

    Args:
        max_n: 输出 stocks 数量上限 (None = 用全局 TOP_N)

    返回 dict:
        stocks, df, seal_scores, money_scores, raw_money,
        sector_mom, sector_res, tech_scores, history_scores,
        buyability_scores, stock_sent_scores, principal_scores,
        sentiment_score, sentiment_level, sentiment_detail, sentiment_ok, date
    """
    filtered = inputs['filtered']
    scoring_base = inputs.get('scoring_base', filtered)
    fund_df = inputs['fund_df']
    sentiment_score = inputs['sentiment_score']
    sentiment_level = inputs['sentiment_level']
    sentiment_detail = inputs['sentiment_detail']
    sentiment_ok = inputs['sentiment_ok']
    history_scores = inputs['history_scores']
    lhb_bonus = inputs['lhb_bonus']
    today_str = inputs['today_str']
    pool = inputs['pool']
    principal = inputs['principal']

    # 1. 在归一化基准集上计算因子，再缩到最终 filtered 集
    print("  [PlanA] 计算9因子...", file=sys.stderr)
    factors_full = compute_factors(scoring_base, fund_df, principal)

    # v2.0 新增: 持续性 + 回撤位置因子 (解决"评分高=追高陷阱"问题)
    # 无历史数据时降级到 5.0 (中性, 不影响总评分)
    # use_v2=False 时跳过 v2 因子,保持与 baseline 一致
    if use_v2:
        try:
            from plans.factors_v2 import compute_v2_factors
            v2 = compute_v2_factors(scoring_base, today_str)
            factors_full['momentum_consistency'] = v2['momentum_consistency']
            factors_full['pullback_depth'] = v2['pullback_depth']
            n_with_hist = sum(1 for v in v2['momentum_consistency'] if v != 5.0)
            print(f"  [PlanA v2] {n_with_hist}/{len(scoring_base)} 票有历史数据,新因子生效",
                  file=sys.stderr)
        except Exception as e:
            print(f"  [PlanA v2] 因子计算失败: {e}", file=sys.stderr)
            factors_full['momentum_consistency'] = pd.Series(5.0, index=scoring_base.index)
            factors_full['pullback_depth'] = pd.Series(5.0, index=scoring_base.index)
    else:
        # A/B 对比: 老评分模式,mc/pd 设为 5.0 (中性),position_factor=0.95 等比缩放
        # 注: 这会让 total = base * 0.95,与 v2 完全关闭不同(v2=全 5.0,position=0.95;关闭 v2=base 不变)
        # 实际效果: 老评分 = base 不变;v2 = base * 0.95 * mc_factor * pd_factor
        # 为保证老评分与 baseline 完全一致,关闭 v2 时 mc/pd 不参与加权
        factors_full['momentum_consistency'] = pd.Series(5.0, index=scoring_base.index)
        factors_full['pullback_depth'] = pd.Series(5.0, index=scoring_base.index)

    # 缩到 filtered 的索引（如果 scoring_base > filtered）
    if len(scoring_base) > len(filtered):
        common_idx = filtered.index.intersection(scoring_base.index)
        factors = {k: v.loc[common_idx] if hasattr(v, 'loc') and len(v) > 0 else v
                   for k, v in factors_full.items()}
    else:
        factors = factors_full

    # 2. 加权 + 危险信号
    print("  [PlanA] 加权+危险信号...", file=sys.stderr)
    total_scores, base_scores, danger_flags, weights = apply_scores(
        filtered, factors, sentiment_score, history_scores, lhb_bonus, today_str,
        use_v2=use_v2)

    # 3. 组装 stocks
    print("  [PlanA] 组装TOP股票...", file=sys.stderr)
    stocks = build_stocks(filtered, factors, total_scores, base_scores, danger_flags,
                          sentiment_score, history_scores, pool, max_n=max_n)

    return {
        'stocks': stocks,
        'df': filtered,
        'seal_scores': factors['seal'],
        'money_scores': factors['money'],
        'raw_money': factors['raw_money'],
        'sector_mom': factors['sector_mom'],
        'sector_res': factors['sector_res'],
        'tech_scores': factors['tech'],
        'history_scores': history_scores,
        'buyability_scores': factors['buyability'],
        'stock_sent_scores': factors['stock_sentiment'],
        'principal_scores': factors['principal'],
        'sentiment_score': sentiment_score,
        'sentiment_level': sentiment_level,
        'sentiment_detail': sentiment_detail,
        'sentiment_ok': sentiment_ok,
        'date': today_str,
    }
