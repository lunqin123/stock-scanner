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
    if turnover > 15: parts.append("竞价量>昨日成交8%")
    elif turnover > 5: parts.append("竞价量>昨日成交5%")
    else: parts.append("竞价量>昨日成交3%")
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
    try:
        import stock_community
        comm_scores = stock_community.score_community(filtered)
        if comm_scores is not None and not comm_scores.empty:
            comm_aligned = comm_scores.reindex(stock_sent.index).fillna(stock_sent)
            stock_sent = (stock_sent * 0.7 + comm_aligned / 7.0 * 10.0 * 0.3).clip(0, 10)
    except Exception:
        pass  # 舆情不可用时不改变 stock_sent
    pr = score_by_principal(filtered, principal)

    return {
        'seal': seal, 'money': money, 'raw_money': raw_money,
        'sector_mom': sector_mom, 'sector_res': sector_res,
        'tech': tech, 'buyability': buyability,
        'stock_sentiment': stock_sent, 'principal': pr,
    }


def reindex_factors(factors, idx):
    """将所有因子 reindex 到新索引（过滤后使用）"""
    return {k: v.loc[idx] if hasattr(v, 'loc') and len(v) > 0 else v for k, v in factors.items()}


# ═══════════════════════════════════════════
#  评分聚合
# ═══════════════════════════════════════════

def apply_scores(filtered, factors, sentiment_score, history_scores, lhb_bonus, today_str):
    """
    因子加权 + 大盘情绪系数 + 危险信号惩罚。
    返回 (total_scores, base_scores, danger_flags, weights)
    """
    import weight_manager

    money = factors['money']
    if hasattr(lhb_bonus, 'loc') and not lhb_bonus.empty:
        money = (money + lhb_bonus.loc[filtered.index]).clip(upper=20.0)
    else:
        money = money.clip(upper=20.0)

    sentiment_series = pd.Series(float(sentiment_score), index=filtered.index)
    h_scores = history_scores.loc[filtered.index] if hasattr(history_scores, 'loc') and len(filtered.index) > 0 \
               else pd.Series(2.5, index=filtered.index)

    weights = weight_manager.load_weights()
    base = weight_manager.apply_weights(
        factors['seal'], money, factors['sector_res'], factors['sector_mom'],
        factors['tech'], h_scores,
        sentiment_series,
        stock_sentiment_scores=factors['stock_sentiment'],
        principal_scores=factors['principal'],
        weights=weights)

    from scanner import score_danger_signals
    danger_penalty, danger_flags = score_danger_signals(filtered, factors['raw_money'], today_str)
    total = (base + danger_penalty).clip(lower=0)

    # 后台回测
    try:
        from scanner import auto_verify_backtest
        threading.Thread(target=lambda: auto_verify_backtest(today_str, current_weights=weights),
                        daemon=True).start()
    except Exception:
        pass

    return total, base, danger_flags, weights


# ═══════════════════════════════════════════
#  组装前端数据
# ═══════════════════════════════════════════

def build_stocks(filtered, factors, total_scores, base_scores, danger_flags,
                 sentiment_score, history_scores, pool=None):
    """组装 stocks 列表，供前端卡片渲染"""
    from scanner import money_str, TOP_N

    top_indices = list(total_scores.sort_values(ascending=False).head(TOP_N).index)

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

def score(inputs: dict) -> dict:
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
        filtered, factors, sentiment_score, history_scores, lhb_bonus, today_str)

    # 3. 组装 stocks
    print("  [PlanA] 组装TOP股票...", file=sys.stderr)
    stocks = build_stocks(filtered, factors, total_scores, base_scores, danger_flags,
                          sentiment_score, history_scores, pool)

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
