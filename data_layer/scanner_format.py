"""scanner_format.py - 文本输出格式化层

职责: 把已评分的 DataFrame 渲染成 CLI 文本 (表格 / 详细文本两种风格)。
约束: 仅依赖 utils / weight_manager (运行时导入) / pandas。
"""
import sys
from datetime import date

import pandas as pd

from scanner_utils import money_str, TOP_N


def format_table_output(df: pd.DataFrame, money_scores: pd.Series,
                        sector_raw: pd.Series, seal_scores: pd.Series,
                        tech_scores: pd.Series,
                        raw_money: pd.Series = None,
                        sentiment_score: float = 5.0,
                        sentiment_level: str = "未知",
                        sentiment_detail: dict = None,
                        history_scores: pd.Series = None,
                        buyability_scores: pd.Series = None,
                        sector_res_scores: pd.Series = None,
                        stock_sentiment_scores: pd.Series = None,
                        weights: dict = None) -> str:
    """精简表格输出 (TOP N)."""
    import weight_manager
    w = weights if weights else weight_manager.DEFAULT_WEIGHTS
    s_history = history_scores if history_scores is not None else pd.Series(2.5, index=df.index)
    s_buyability = buyability_scores if buyability_scores is not None else pd.Series(6.0, index=df.index)
    s_res = sector_res_scores if sector_res_scores is not None else pd.Series(4.0, index=df.index)
    s_ss = stock_sentiment_scores if stock_sentiment_scores is not None else pd.Series(5.0, index=df.index)

    s_sector = (s_res + sector_raw) / 2.0
    base_totals = weight_manager.apply_weights(seal_scores, money_scores, s_sector, tech_scores, s_history, sentiment_score=pd.Series(float(sentiment_score), index=df.index), stock_sentiment_scores=s_ss, weights=w)
    total_scores = base_totals
    df = df.copy()
    df['基础总分'] = base_totals.round(1)
    df['总分'] = total_scores.round(1)
    df['封板质量'] = seal_scores.round(1)
    df['资金面'] = money_scores.round(1)
    df['板块热度'] = sector_raw.round(1)
    df['技术形态'] = tech_scores.round(1)
    df['市场情绪'] = pd.Series(sentiment_score, index=df.index).round(1)
    df['历史股性'] = s_history.round(1)
    df['舆情评分'] = pd.Series(0, index=df.index)
    df['开盘可行性'] = s_buyability.round(1)
    df['个股情绪'] = s_ss.round(1)
    df['板块共振'] = s_res.round(1)
    def _pad(s, w, right=False):
        """CJK字符宽度补齐：中文占2格，英文/数字占1格"""
        s = str(s)
        cjk_len = sum(2 if '一' <= c <= '鿿' else 1 for c in s)
        sp = max(0, w - cjk_len)
        return (' ' * sp + s) if right else (s + ' ' * sp)

    if raw_money is not None:
        df['净流入'] = df.index.map(lambda idx: money_str(raw_money.get(idx, 0)))
    else:
        df['净流入'] = ""

    df = df.sort_values('总分', ascending=False)

    # ── 控制输出列数 ──
    out = df.head(TOP_N)

    sep = "─" * 88
    col_w = [3, 6, 8, 4, 3, 3, 3, 3, 3, 8, 4, 4, 4]  # display widths
    hdr = f" # │{_pad('代码',col_w[1])}│{_pad('名称',col_w[2])}│{_pad('总分',col_w[3])}│{_pad('涨停',col_w[4])}│{_pad('资金',col_w[5])}│{_pad('板块',col_w[6])}│{_pad('量价',col_w[7])}│{_pad('股性',col_w[8])}│{_pad('舆情',col_w[12])}│{_pad('净流入',col_w[9])}│{_pad('换手%',col_w[10])}│{_pad('封板',col_w[11])}"
    div_parts = ['─' * w for w in col_w]
    div = "─" + "┼".join(div_parts) + "─"

    lines = [sep, hdr, div]
    for rank, (_, row) in enumerate(out.iterrows(), 1):
        code = str(row.get('代码', row.iloc[1])).strip().zfill(6)
        name = row.get('名称', row.iloc[2])
        total = str(int(round(float(row['总分']))))
        seal_s = f"{float(row['封板质量']):.0f}"
        money_s = f"{float(row['资金面']):.0f}"
        sector_s = f"{float(row['板块热度']):.0f}"
        tech_s = f"{float(row['技术形态']):.0f}"
        history_s = f"{float(row['历史股性']):.0f}"
        community_s = f"{float(row['舆情评分']):.0f}"
        money_detail = str(row['净流入'])
        turnover = f"{float(row.get('换手率', 0)):.1f}" if pd.notna(row.get('换手率')) else "?"
        seal_time = str(row.get('首次封板时间', row.get('', '')))[:4]
        lines.append(f"{rank:<2} │{_pad(code,col_w[1])}│{_pad(name,col_w[2])}│{_pad(total,col_w[3],right=True)}│{_pad(seal_s,col_w[4],right=True)}│{_pad(money_s,col_w[5],right=True)}│{_pad(sector_s,col_w[6],right=True)}│{_pad(tech_s,col_w[7],right=True)}│{_pad(history_s,col_w[8],right=True)}│{_pad(community_s,col_w[12],right=True)}│{_pad(money_detail,col_w[9],right=True)}│{_pad(turnover,col_w[10],right=True)}│{_pad(seal_time,col_w[11],right=True)}")

    lines.append(sep)
    return "\n".join(lines)


def format_output(df: pd.DataFrame, money_scores: pd.Series,
                  sector_raw: pd.Series, seal_scores: pd.Series,
                  tech_scores: pd.Series,
                  raw_money: pd.Series = None,
                  sentiment_score: float = 5.0,
                  sentiment_level: str = "未知",
                  sentiment_detail: dict = None,
                  history_scores: pd.Series = None,
                  buyability_scores: pd.Series = None,
                  sector_res_scores: pd.Series = None,
                  stock_sentiment_scores: pd.Series = None,
                  weights: dict = None) -> str:
    """详细文本输出 (TOP N, 含买卖逻辑 / 风险提示)。

    BUG 修复 (2026-06-21 refactor): 此前缺失 stock_sentiment_scores 参数,
    但函数体内 line 952 引用了同名变量 → 一旦调用必触发 NameError。
    """
    import weight_manager
    w = weights if weights else weight_manager.DEFAULT_WEIGHTS
    s_history = history_scores if history_scores is not None else pd.Series(2.5, index=df.index)
    s_buyability = buyability_scores if buyability_scores is not None else pd.Series(6.0, index=df.index)
    s_res = sector_res_scores if sector_res_scores is not None else pd.Series(4.0, index=df.index)
    s_ss = stock_sentiment_scores if stock_sentiment_scores is not None else pd.Series(5.0, index=df.index)

    s_sector = (s_res + sector_raw) / 2.0
    base_totals = weight_manager.apply_weights(seal_scores, money_scores, s_sector, tech_scores, s_history, sentiment_score=pd.Series(float(sentiment_score), index=df.index), stock_sentiment_scores=s_ss, weights=w)
    total_scores = base_totals
    df = df.copy()
    df['基础总分'] = base_totals.round(1)
    df['总分'] = total_scores.round(1)
    df['封板质量'] = seal_scores.round(1)
    df['资金面'] = money_scores.round(1)
    df['板块热度'] = sector_raw.round(1)
    df['技术形态'] = tech_scores.round(1)
    df['市场情绪'] = pd.Series(sentiment_score, index=df.index).round(1)
    df['历史股性'] = s_history.round(1)
    df['舆情评分'] = pd.Series(0, index=df.index)
    df['开盘可行性'] = s_buyability.round(1)
    df['个股情绪'] = s_ss.round(1)
    df['板块共振'] = s_res.round(1)

    top_indices = list(total_scores.sort_values(ascending=False).head(TOP_N).index)
    out = df.loc[top_indices]

    if raw_money is not None:
        money_detail = lambda idx: money_str(raw_money.get(idx, 0))
    else:
        money_detail = lambda idx: "?"

    lines = []

    today_display = date.today().strftime("%Y-%m-%d")
    sentiment_tag = sentiment_level.replace('炸板(', '').replace(')', '') if '炸板' in sentiment_level else sentiment_level
    zhaban_rate = (sentiment_detail or {}).get('zhaban_rate', 0)
    promo_rate = (sentiment_detail or {}).get('promotion_rate', 0)
    avg_premium = (sentiment_detail or {}).get('avg_premium', 0)
    prev_count = (sentiment_detail or {}).get('prev_limit_count', 0)
    lines.append(f"TOP {TOP_N} 超短线标的 ({today_display}) | "
                 f"情绪:{sentiment_level} | "
                 f"上交易日涨停{prev_count}只 | 溢价{avg_premium:+.2f}% | "
                 f"晋级{promo_rate*100:.0f}% | 炸板{zhaban_rate*100:.0f}%")
    lines.append("=" * 70)

    for rank, idx in enumerate(top_indices, 1):
        row = out.loc[idx]
        score = float(row['总分'])
        base_score = float(row['基础总分'])
        seal_score_val = float(row['封板质量'])
        money_score_val = float(row['资金面'])
        sector_score_val = float(row['板块热度'])
        tech_score_val = float(row['技术形态'])
        history_val = float(row['历史股性'])
        community_val = float(row['舆情评分'])

        code = str(row.get('代码', row.get('', ''))).strip().zfill(6)
        name = row.get('名称', row.get('', ''))
        industry = row.get('所属行业', '')
        turnover = f"{float(row['换手率']):.1f}" if '换手率' in df.columns and pd.notna(row.get('换手率')) else "?"
        seal_time = str(row.get('首次封板时间', '') or '')[:4] if '首次封板时间' in df.columns else "?"
        lianban = row.get('连板数', '?')

        # 净流入
        mn = money_detail(idx)
        # 资金面描述
        if money_score_val > 15:
            money_detail_str = "净流入" + mn
        elif money_score_val > 8:
            money_detail_str = "净流入" + mn
        else:
            money_detail_str = "净流入" + mn if money_score_val >= 0 else f"净流出{mn}"

        buy_parts = []
        if seal_score_val >= 15:
            buy_parts.append('封板强度高')
        elif seal_score_val >= 10:
            buy_parts.append('封板质量中等')
        if money_score_val > 10:
            buy_parts.append(money_detail_str)
        if float(row['板块热度']) > 8:
            buy_parts.append('板块效应强')
        if lianban != '?' and float(lianban) >= 2:
            buy_parts.append(f'连板{lianban}')
        buy_logic = '+'.join(buy_parts) if buy_parts else '标准首板标的'

        risk_parts = []
        if float(row['封板质量']) < 11:
            risk_parts.append('封板偏弱')
        try:
            if float(turnover) > 20:
                risk_parts.append('换手过高')
        except (ValueError, TypeError) as e:
            print(f"  [scanner_format] turnover failed: {e}", file=sys.stderr)
        # 连板可买性风险提示：高连板但可买性低 = 明天买不到
        try:
            lb_val = float(lianban) if lianban != '?' else 0
            by_val = float(row.get('开盘可行性', 0))
            if lb_val >= 4 and by_val < 6:
                risk_parts.append(f'{int(lb_val)}连板可买性仅{by_val:.0f}分，明天买不到')
            elif lb_val >= 3 and by_val < 4:
                risk_parts.append(f'{int(lb_val)}连板可买性低，大概率被堵')
        except (ValueError, TypeError) as e:
            print(f"  [scanner_format] lb failed: {e}", file=sys.stderr)
        risk = '; '.join(risk_parts) if risk_parts else '关注次日竞价'

        try:
            fund_str = f"{float(row.get('封板资金', 0)):.0f}"
        except (ValueError, TypeError):
            fund_str = str(row.get('封板资金', '0'))

        lines.append(f"\n{rank}. {code} {name} | 总分 {score:.1f}")
        lines.append(f"   封板: {seal_time} | 封单 {fund_str} | 换手 {turnover}%")
        lines.append(f"   资金面: {money_score_val:.1f}/{w['money']:.0f} {money_detail_str} | 板块: {industry} ({row['板块热度']:.0f}/{w.get('sector_mom', 15):.0f})")
        ss_val = float(row.get('个股情绪', 5))
        lines.append(f"   评分拆解: 封板{seal_score_val:.1f}/{w['seal']:.0f} + 资金{money_score_val:.1f}/{w['money']:.0f} + 板块{float(row['板块热度']):.0f}/{w.get('sector_mom', 15):.0f} + 量价{tech_score_val:.1f}/{w['tech']:.0f} + 股性{history_val:.1f}/{w['history']:.0f} + 个情{ss_val:.1f}/{w.get('stock_sentiment', 10):.0f} + 共振{float(row['板块共振']):.0f}/{w['sector_res']:.0f} (可买={float(row.get('开盘可行性', 0)):.0f}, 仅参考)")
        lines.append(f"   {buy_logic}")
        lines.append(f"   {risk}")

    lines.append(f"\n{'=' * 70}")
    lines.append("仓位建议 (本金 2 万):")
    lines.append("   - 单票上限 6000 元 (30%)")
    lines.append("   - 同时持有 2-3 只")
    lines.append("   - 竞价低于开盘价 3% → 直接出")
    lines.append("   - 竞价在 -1%~+1% → 观察 5 分钟，不上攻立刻出")
    lines.append("   - 竞价高于开盘价 +2% → 持有，10:30 前不涨停分批出")
    lines.append("")
    lines.append("免责: 本数据仅供参考，不构成投资建议。")
    lines.append("akshare 数据存在 5-10 分钟延迟。")

    return "\n".join(lines)
