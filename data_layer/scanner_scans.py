"""scanner_scans.py - 5 种扫描模式的主流程

职责: 每个模式一个 scan_xxx 函数,负责: 拉数据 → 过滤 → 调 scoring 评分 → 渲染输出。
约束: 不依赖 scanner_backtest; 可依赖 utils / filters / data / factors / scoring / format。
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

from scanner_utils import (
    get_market_status, money_str, TOP_N,
)
from scanner_filters import (
    filter_non_main_board, filter_xr_xd_dr, filter_by_price,
)
from scanner_data import fetch_fund_flow_data
from scanner_scoring import (
    score_zhaban_data, _score_reversal, _score_trend,
    score_sector_data, score_dtqiaoban_data,
)


# ═══════════════════════════════════════════
#  反转扫描
# ═══════════════════════════════════════════

def scan_reversal(today_str: str, table_mode: bool = False, top_n: int = None):
    """
    涨停回调反转扫描：找"上交易日涨停今日下跌"的股票，评估明日反包潜力。
    逻辑：上交易日强势封板→今天回调洗盘→明天最可能反转大涨。
    达实智能这类"上交易日跌今天涨停"的反转股，上交易日大概率不在涨停池，
    但前天涨停上交易日回调的股票，今天就是反转候选。
    """
    n = top_n if top_n is not None else TOP_N
    print("[反转扫描] 获取上交易日涨停今日表现...", file=sys.stderr)
    try:
        prev = ak.stock_zt_pool_previous_em(date=today_str)
    except Exception as e:
        print(f"  数据获取失败: {e}", file=sys.stderr)
        return
    if prev.empty:
        print("no_data")
        return

    df = filter_non_main_board(prev)
    if df.empty:
        print("no_data")
        return

    chg_col = df.columns[3]; code_col = df.columns[1]; name_col = df.columns[2]
    df = filter_xr_xd_dr(df, name_col=name_col)
    if df.empty:
        print("no_data")
        return
    price_col = df.columns[4]; turnover_col = df.columns[9]
    ind_col = df.columns[15] if len(df.columns) > 15 else None
    seal_stat_col = df.columns[14] if len(df.columns) > 14 else None

    df['今日涨幅'] = df[chg_col].astype(float)

    # 筛选：今日下跌或微涨<1%（回调中）
    pullback = df[(df['今日涨幅'] >= -7) & (df['今日涨幅'] <= 1)].copy()
    if pullback.empty:
        print("no_data")
        return
    print(f"  → 上交易日涨停今回调: {len(pullback)} 只 (总{len(df)}只)", file=sys.stderr)

    # ── 反转评分 (P2.1 抽到 _score_reversal) ──
    pullback = _score_reversal(pullback, today_str=today_str)

    pullback = pullback.sort_values('反转评分', ascending=False).head(n)

    # ── 输出 ──
    if table_mode:
        lines = []
        lines.append(f"{'代码':<8s} {'名称':<8s} {'今涨幅':>7s} {'换手':>6s} {'连板':>4s} {'行业':<10s} {'反转分':>6s} {'建议':<14s}")
        lines.append("-" * 72)
        for _, row in pullback.iterrows():
            code = str(row[code_col]).strip().zfill(6)
            name = str(row[name_col])
            chg = row['今日涨幅']
            to = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
            raw = str(row[seal_stat_col]) if seal_stat_col else ''
            lb = int(raw.split('/')[1]) if '/' in raw else 0
            ind = str(row[ind_col]) if ind_col and pd.notna(row[ind_col]) else ''
            score = int(row['反转评分'])
            if score >= 80: adv = '⭐ 重点观察'
            elif score >= 65: adv = '加入自选'
            elif score >= 50: adv = '观望'
            else: adv = '暂不参与'
            lines.append(f"{code:<8s} {name:<8s} {chg:+6.1f}% {to:5.1f}% {lb:4d} {ind:<10s} {score:6d} {adv:<14s}")
        print('\n'.join(lines))
    else:
        for _, row in pullback.iterrows():
            code = str(row[code_col]).strip().zfill(6)
            name = str(row[name_col])
            score = int(row['反转评分'])
            chg = row['今日涨幅']
            to = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
            ind = str(row[ind_col]) if ind_col and pd.notna(row[ind_col]) else ''
            print(f"{code} {name}: 反转{score}分 | 今{chg:+.1f}% | 换手{to:.1f}% | {ind}")

    return pullback[['反转评分']] if not pullback.empty else None


# ═══════════════════════════════════════════
#  炸板反包扫描
# ═══════════════════════════════════════════

def scan_zhaban(today_str: str, table_mode: bool = False, top_n: int = None):
    n = top_n if top_n is not None else TOP_N
    print("[炸板扫描] 获取炸板股池...", file=sys.stderr)
    try:
        zb_df = ak.stock_zt_pool_zbgc_em(date=today_str)
    except Exception as e:
        print(f"  炸板数据获取失败: {e}", file=sys.stderr)
        return
    if zb_df.empty: print("no_data"); return
    print(f"  → 炸板股共 {len(zb_df)} 只", file=sys.stderr)

    df = zb_df.copy()
    before = len(df)
    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)
    from config import MAX_MARKET_CAP, MAX_PRICE
    if '流通市值' in df.columns:
        df = df[df['流通市值'].astype(float) <= MAX_MARKET_CAP * 1e8]
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    df = df[df[price_col].astype(float) <= MAX_PRICE]
    after = len(df)
    print(f"  → 过滤后 {after}/{before} 只", file=sys.stderr)
    if df.empty: print("no_data"); return

    # 统一评分 (加载调权后的权重, 与回测/信号系统对齐)
    try:
        from weight_manager import _load_tab_weights
        w = _load_tab_weights('zhaban')
    except Exception:
        w = None
    out_df = score_zhaban_data(df, today_str, weights=w)
    if n < TOP_N: out_df = out_df.head(n)

    from datetime import date
    today_display = date.today().strftime('%Y-%m-%d')
    lines = [f"炸板股反包潜力 TOP{n} ({today_display})", "=" * 70]

    for rank, (_, row) in enumerate(out_df.iterrows(), 1):
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        score = float(row['总分'])
        seal_s = float(row['封板质量'])
        money_s = float(row['资金承接'])
        feat_s = float(row['炸板特征'])
        turn_s = float(row['换手评分'])
        sec_s = float(row['板块热度'])
        st_col = '首次封板时间' if '首次封板时间' in out_df.columns else out_df.columns[11]
        zb_col = '炸板次数' if '炸板次数' in out_df.columns else out_df.columns[12]
        to_col = '换手率' if '换手率' in out_df.columns else out_df.columns[9]
        seal_time = str(row.get(st_col, ''))[:4]
        zhaban_cnt = int(float(row.get(zb_col, 0))) if pd.notna(row.get(zb_col, None)) else 0
        turnover = float(row.get(to_col, 0)) if pd.notna(row.get(to_col, None)) else 0

        # 净流入文本
        net = float(row.get('净流入', 0))
        if abs(net) >= 1e8:
            net_str = f"{net/1e8:.2f}亿"
        elif abs(net) >= 1e4:
            net_str = f"{net/1e4:.0f}万"
        else:
            net_str = f"{net:.0f}"

        # 反包潜力判断
        parts = []
        if seal_s >= 15:
            parts.append("封板质量高")
        if money_s >= 12:
            parts.append(f"资金承接强({net_str})")
        elif net < 0:
            parts.append(f"资金流出{net_str}")
        if feat_s >= 10:
            parts.append("炸板特征优(早封+适中换手)")
        if 8 <= turnover <= 20:
            parts.append("换手适中")
        elif turnover > 30:
            parts.append("换手过高⚠️")
        logic = " + ".join(parts) if parts else "标准炸板反包标的"

        lines.append(f"\n{rank}. {code} {name} | 反包潜力 {score:.1f}")
        lines.append(f"   封板时间 {seal_time} | 炸板 {zhaban_cnt}次 | 换手 {turnover:.1f}%")
        lines.append(f"   评分拆解: 封板质量{seal_s:.1f}/25 + 资金承接{money_s:.1f}/20 + 炸板特征{feat_s:.1f}/15 + 换手评分{turn_s:.1f}/10 + 板块热度{sec_s:.1f}/12")
        lines.append(f"   {logic}")
        risk = "次日反包需竞价放量高开确认，低开-3%直接放弃" if net >= 0 else "资金净流出，反包概率降低，观望为主"
        lines.append(f"   {risk}")

    # ── 构建返回数据（供全维度综合榜单使用） ──
    zhaban_results = []
    for _, row in out_df.iterrows():
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        score = float(row['总分'])
        zhaban_results.append({'code': code, 'name': name, 'score': score, 'dimension': '炸板反包'})

    lines.append(f"\n{'=' * 70}")
    lines.append("反包策略: 竞价高开+放量 = 可参与 | 竞价平开/低开 = 放弃")
    lines.append("止损: 参与后次日收盘不反包即出，-5%硬止损")
    print("\n".join(lines))
    return zhaban_results


# ═══════════════════════════════════════════
#  趋势动量扫描
# ═══════════════════════════════════════════

def scan_trend(today_str: str, _table_mode: bool = False, top_n: int = None):
    """
    趋势动量股扫描。
    找出近期趋势强劲、量价配合好但未涨停的标的。
    适用于"不涨停不停涨"的趋势交易模式。
    """
    from datetime import date
    n = top_n if top_n is not None else TOP_N
    print("[趋势扫描] 获取强势股池...", file=sys.stderr)

    # ── 主方案：akshare 强势池（盘中实时数据，已验证可用）──
    strong_df = None
    try:
        strong_df = ak.stock_zt_pool_strong_em(date=today_str)
        if strong_df is not None and not strong_df.empty:
            print(f"  -> 强势池 {len(strong_df)} 只（实时数据）", file=sys.stderr)
    except Exception as e:
        print(f"  ! 强势池获取失败: {e}", file=sys.stderr)

    if strong_df is not None and not strong_df.empty:
        # ── 强势池数据清洗与筛选 ──
        df = strong_df.copy()
        before = len(df)

        # 列名标准化（强势池列名已经规范，但做防御处理）
        code_col = '代码' if '代码' in df.columns else df.columns[1]
        name_col = '名称' if '名称' in df.columns else df.columns[2]
        change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
        turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
        vol_ratio_col = '量比' if '量比' in df.columns else None   # 强势池特有
        volume_col = '成交额' if '成交额' in df.columns else df.columns[6]
        cap_col = '流通市值' if '流通市值' in df.columns else None
        industry_col = '所属行业' if '所属行业' in df.columns else None
        new_high_col = '是否新高' if '是否新高' in df.columns else None   # 强势池特有

        # 板块过滤
        df = filter_non_main_board(df, code_col=code_col)

        # 市值过滤
        from config import MAX_MARKET_CAP, MAX_PRICE
        if cap_col and cap_col in df.columns:
            df = df[df[cap_col].astype(float) <= MAX_MARKET_CAP * 1e8]

        # 价格过滤
        price_col = '最新价' if '最新价' in df.columns else df.columns[4]
        df = df[df[price_col].astype(float) <= MAX_PRICE]

        # ── 核心筛选：涨幅2.5-8.5%（没涨停的强势股，能买进！）──
        changes = df[change_col].astype(float)
        df = df[(changes >= 2.5) & (changes < 8.5)]

        after = len(df)
        print(f"  -> 过滤后 {after}/{before} 只（涨幅2.5-8.5% + 非ST/科创/北交 + 市值<{MAX_MARKET_CAP}亿）", file=sys.stderr)

        if not df.empty:
            # ── 动量评分 (P2.2 抽到 _score_trend) ──
            try:
                from weight_manager import load_trend_weights
                _w = load_trend_weights()
            except Exception:
                _w = None
            df = _score_trend(df, weights=_w)
            df = df.sort_values('动量评分', ascending=False).head(n)

            # ── 输出 ──
            market_status = get_market_status()
            time_tag = "盘中实时" if market_status == 'trading' else "盘后强势池"
            today_display = date.today().strftime('%Y-%m-%d')
            lines = [f"趋势动量股 TOP{n} ({today_display} {time_tag})", "=" * 80]

            trend_results = []
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                name = row[name_col]
                score = float(row['动量评分'])
                trend_results.append({'code': code, 'name': name, 'score': min(score, 100), 'dimension': '趋势动量'})

            for rank, (_, row) in enumerate(df.iterrows(), 1):
                code = str(row[code_col]).strip().zfill(6)
                name = row[name_col]
                score = float(row['动量评分'])
                chg = float(row[change_col])
                t = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
                v = float(row[volume_col]) if pd.notna(row[volume_col]) else 0
                vol_str = f"{v/1e8:.2f}亿" if v >= 1e8 else f"{v/1e4:.0f}万"
                # 量比显示
                vr_display = ""
                if vol_ratio_col and vol_ratio_col in df.columns:
                    vr_val = float(row[vol_ratio_col]) if pd.notna(row.get(vol_ratio_col, None)) else 0
                    if vr_val > 0:
                        vr_display = f" 量比{vr_val:.1f}"
                # 新高显示
                nh_display = ""
                if new_high_col and new_high_col in df.columns:
                    if str(row.get(new_high_col, '')) == '是':
                        nh_display = " [新高!]"
                # 行业
                industry = ""
                if industry_col and industry_col in df.columns:
                    industry = str(row.get(industry_col, ''))

                # 评级
                if score >= 75:
                    grade = "AAA"
                    grade_desc = "强势突破"
                elif score >= 60:
                    grade = "AA"
                    grade_desc = "趋势优良"
                elif score >= 45:
                    grade = "A"
                    grade_desc = "值得关注"
                else:
                    grade = "B"
                    grade_desc = "一般"

                lines.append(f"\n{rank}. {code} {name} | {grade}级 {score:.1f}分{nh_display}")
                lines.append(f"   涨幅 {chg:+.1f}% | 换手 {t:.1f}% | 成交 {vol_str}{vr_display}")
                signal = "量价齐升" if t > 5 and chg > 4 else "温和放量" if t > 2 else "缩量上涨"
                lines.append(f"   信号: {signal}({grade_desc}) | 行业: {industry}")
                lines.append(f"   策略: 沿5/10日线低吸，放量滞涨止盈，不追高")

            lines.append(f"\n{'=' * 80}")
            lines.append("盘中趋势策略: 从未涨停的强势股中寻找动量标的，买在启动前")
            lines.append("筛选条件: 涨幅2.5-8.5% + 换手5-15% + 量比>1.0（盘中实时）")
            lines.append("止损: 跌破10日线或单日跌超-5%出局")
            print("\n".join(lines))
            return trend_results

        # ── 强势池有数据但筛选后为空 ──
        print("  -> 强势池中无符合条件标的（涨幅2.5-8.5% + 非ST/科创/北交）", file=sys.stderr)
        # 放宽条件重试
        df_loose = strong_df.copy()
        df_loose = filter_non_main_board(df_loose)
        changes_loose = df_loose['涨跌幅' if '涨跌幅' in df_loose.columns else df_loose.columns[3]].astype(float)
        df_loose = df_loose[(changes_loose >= 1.5) & (changes_loose < 9.5)]
        if not df_loose.empty:
            print(f"  -> 放宽条件后 {len(df_loose)} 只（涨幅1.5-9.5%）", file=sys.stderr)
            # 简单排序输出
            df_loose['动量评分'] = changes_loose[df_loose.index] * 8
            df_loose = df_loose.sort_values('动量评分', ascending=False).head(n)
            today_display = date.today().strftime('%Y-%m-%d')
            lines = [f"趋势动量股 TOP{n} ({today_display} 放宽条件)", "=" * 60]
            trend_results = []
            for _, row in df_loose.iterrows():
                c = str(row.get('代码', row.iloc[1])).strip().zfill(6)
                nm = row.get('名称', row.iloc[2])
                chg = float(row.get('涨跌幅', row.iloc[3]))
                trend_results.append({'code': c, 'name': nm, 'score': min(chg * 10, 100), 'dimension': '趋势动量'})
                lines.append(f"{len(trend_results)}. {c} {nm} | 涨幅 {chg:+.1f}%")
            lines.append(f"\n{'=' * 60}")
            lines.append("放宽条件扫描，标的仅供参考")
            print("\n".join(lines))
            return trend_results

    # ── 降级方案：上交易日涨停今日续强（盘后可用）──
    print("  强势池不可用，使用「上交易日涨停今日续强」策略", file=sys.stderr)
    print("  该策略寻找上交易日涨停后今日继续走强(涨幅3-9%)的标的", file=sys.stderr)
    today_dt = datetime.strptime(today_str, '%Y%m%d') if len(today_str) == 8 else datetime.today()
    wd = today_dt.weekday()
    days_back = 3 if wd == 0 else (2 if wd == 6 else 1)
    yesterday = (today_dt - timedelta(days=days_back)).strftime('%Y%m%d')
    try:
        prev = ak.stock_zt_pool_previous_em(date=yesterday)
    except Exception:
        prev = pd.DataFrame()

    if prev.empty:
        print("no_data")
        return

    change_col = '涨跌幅' if '涨跌幅' in prev.columns else (prev.columns[3] if len(prev.columns) > 3 else prev.columns[0])
    df = prev.copy()
    df = filter_non_main_board(df)
    changes = df[change_col].astype(float)
    df = df[(changes >= 3) & (changes < 9)]
    if df.empty:
        print("no_data")
        return

    df['趋势评分'] = changes * 10
    df = df.sort_values('趋势评分', ascending=False).head(n)

    lines = [f"趋势动量股 TOP{n} | 上交易日涨停今日续强", "=" * 70]
    for rank, (_, row) in enumerate(df.iterrows(), 1):
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        chg = float(row[change_col])
        lines.append(f"{rank}. {code} {name} | 今日涨幅 {chg:+.1f}%")
        lines.append(f"   上交易日涨停后今日继续走强，趋势延续中")
        lines.append(f"   策略: 沿5日线持有，破5日线止盈")

    trend_results = []
    for _, row in df.iterrows():
        code = str(row.iloc[1]).strip().zfill(6)
        name = row.iloc[2]
        chg = float(row[change_col])
        trend_results.append({'code': code, 'name': name, 'score': min(chg * 10, 100), 'dimension': '趋势动量'})

    lines.append(f"\n{'=' * 70}")
    print("\n".join(lines))
    return trend_results


# ═══════════════════════════════════════════
#  板块联动扫描
# ═══════════════════════════════════════════

def scan_sector(today_str: str, table_mode: bool = False, top_n: int = None):
    n = top_n if top_n is not None else TOP_N
    print("[板块扫描] 获取涨停池+炸板池行业分布...", file=sys.stderr)

    # 并行获取涨停池、炸板池
    pool_data = {}

    def _get_limit():
        try:
            return 'limit', ak.stock_zt_pool_em(date=today_str)
        except Exception:  # BUG-5 修复: bare except → except Exception (不吞 KeyboardInterrupt)
            return 'limit', pd.DataFrame()

    def _get_zhaban():
        try:
            return 'zhaban', ak.stock_zt_pool_zbgc_em(date=today_str)
        except Exception:  # BUG-5 修复
            return 'zhaban', pd.DataFrame()

    def _get_dieting():
        try:
            return 'dieting', ak.stock_zt_pool_dtgc_em(date=today_str)
        except Exception:  # BUG-5 修复
            return 'dieting', pd.DataFrame()

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(f): f.__name__ for f in [_get_limit, _get_zhaban, _get_dieting]}
        for f in as_completed(futs):
            try:
                k, v = f.result()
                pool_data[k] = v
            except Exception as e:
                print(f"  [scanner_scans] pool fetch failed: {e}", file=sys.stderr)

    limit_df = pool_data.get('limit', pd.DataFrame())
    zhaban_df = pool_data.get('zhaban', pd.DataFrame())
    dieting_df = pool_data.get('dieting', pd.DataFrame())

    if limit_df.empty and zhaban_df.empty:
        print("no_data")
        return

    # 统一评分
    top_sectors = score_sector_data(limit_df, zhaban_df, dieting_df, top_n=n)

    # ── 获取资金流 (可选增强) ──
    fund_df, _ = fetch_fund_flow_data()
    sector_fund_map = {}
    if fund_df is not None and not fund_df.empty:
        # 获取行业板块资金流
        try:
            sector_fund = ak.stock_sector_fund_flow_rank(indicator="今日")
            if sector_fund is not None and not sector_fund.empty:
                for _, row in sector_fund.iterrows():
                    name = str(row.iloc[1]) if len(row) > 1 else ''
                    net_in = float(row.iloc[4]) if len(row) > 4 else 0
                    sector_fund_map[name] = net_in
        except Exception as e:
            print(f"  [scanner_scans] sector_fund failed: {e}", file=sys.stderr)

    from datetime import date
    today_display = date.today().strftime('%Y-%m-%d')
    lines = [f"今日板块概览 TOP{n} ({today_display})", "=" * 80]
    lines.append(f"{'排名':<4} {'板块名称':<14} {'联动分':<8} {'涨停':<6} {'炸板':<6} {'跌停':<6} {'赚钱效应':<10} {'封板率':<8} {'资金净流':<12} {'ETF共振'}")

    # ── ETF资金共振标记 (v2.0新增) ──
    etf_resonance = {}
    try:
        from scanner_factors import _get_sector_etf_resonance
        etf_resonance = _get_sector_etf_resonance()
    except Exception:
        pass

    for rank, s in enumerate(top_sectors, 1):
        fund_str = ""
        if s['industry'] in sector_fund_map:
            fv = sector_fund_map[s['industry']]
            if fv > 0:
                fund_str = f"+{fv/1e8:.1f}亿" if fv >= 1e8 else f"+{fv/1e4:.0f}万"
            else:
                fund_str = f"{fv/1e8:.1f}亿" if abs(fv) >= 1e8 else f"{fv/1e4:.0f}万"

        # ETF共振信号
        etf_signal = ""
        etf_score = etf_resonance.get(s['industry'], 0) if etf_resonance else 0
        if etf_score >= 3:
            etf_signal = "★★★ 强共振"
        elif etf_score >= 2:
            etf_signal = "★★ 中等共振"
        elif etf_score >= 1:
            etf_signal = "★ 弱共振"
        else:
            etf_signal = "—"

        lines.append(f" #{rank:<2} {s['industry']:<12} {s['link_strength']:<6.1f}  {s['limit_cnt']:<4}  {s['zhaban_cnt']:<4}  {s['dieting_cnt']:<4}  {s['profit_effect']:<5.0f}%   {s['seal_rate']:<5.0f}%  {fund_str:<12} {etf_signal}")

    lines.append(f"\n{'=' * 80}")
    lines.append("此面板为今日市场概览，描述已发生的事实，不作为明日交易信号。")
    lines.append("ETF共振: 板块资金净流入>1亿★, >5亿★★, >10亿★★★ (真金白银验证)")
    lines.append("联动分 = 涨停数 - 炸板×0.3 - 跌停×0.5 (基于今日数据)")
    print("\n".join(lines))


# ═══════════════════════════════════════════
#  跌停翘板扫描
# ═══════════════════════════════════════════

def scan_dtqiaoban(today_str: str, table_mode: bool = False, top_n: int = None):
    n = top_n if top_n is not None else TOP_N
    print("[翘板扫描] 获取跌停股池...", file=sys.stderr)
    try:
        dt_df = ak.stock_zt_pool_dtgc_em(date=today_str)
    except Exception as e:
        print(f"  跌停数据获取失败: {e}", file=sys.stderr)
        return
    if dt_df.empty:
        print("no_data")
        return
    print(f"  → 跌停股共 {len(dt_df)} 只", file=sys.stderr)

    df = dt_df.copy()
    before = len(df)

    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)

    # 过滤市值过大（市值小的更容易翘板）
    from config import MAX_MARKET_CAP
    if '流通市值' in df.columns:
        df = df[df['流通市值'].astype(float) <= MAX_MARKET_CAP * 1e8]
    elif len(df.columns) > 6:
        try:
            df = df[df.iloc[:, 6].astype(float) <= MAX_MARKET_CAP * 1e8]
        except Exception as e:
            print(f"  [scanner_scans] cap filter failed: {e}", file=sys.stderr)

    after = len(df)
    print(f"  → 过滤后 {after}/{before} 只", file=sys.stderr)
    if df.empty:
        print("no_data")
        return

    # 统一评分
    df = score_dtqiaoban_data(df)
    if n < TOP_N: df = df.head(n)

    # ── 列识别（用于输出） ──
    code_col = df.columns[1]
    name_col = df.columns[2]
    change_col = df.columns[3]
    turnover_col = df.columns[9] if len(df.columns) > 9 else None
    seal_fund_col = df.columns[10] if len(df.columns) > 10 else None
    cont_dieting_col = df.columns[13] if len(df.columns) > 13 else None

    from datetime import date
    today_display = date.today().strftime('%Y-%m-%d')
    lines = [f"跌停翘板信号 TOP{n} ({today_display})", "=" * 70]

    for rank, (_, row) in enumerate(df.iterrows(), 1):
        code = str(row[code_col]).strip().zfill(6)
        name = row[name_col]
        score = float(row['翘板评分'])
        chg = float(row[change_col]) if pd.notna(row[change_col]) else -10
        turn = float(row[turnover_col]) if turnover_col and pd.notna(row.get(turnover_col)) else 0
        # 信号描述：基于评分和换手
        sigs = []
        if score >= 60: sigs.append("高信号")
        elif score >= 35: sigs.append("中等信号")
        else: sigs.append("弱信号")
        if turn > 10: sigs.append("高换手")
        detail_str = " + ".join(sigs)

        # 封单信息
        seal_str = ""
        if seal_fund_col:
            sf = float(row[seal_fund_col]) if pd.notna(row.get(seal_fund_col)) else 0
            if sf >= 1e8:
                seal_str = f"封单{sf/1e8:.2f}亿"
            elif sf >= 1e4:
                seal_str = f"封单{sf/1e4:.0f}万"
            else:
                seal_str = f"封单{sf:.0f}"

        # 连续跌停
        cont_str = ""
        if cont_dieting_col:
            cv = row.get(cont_dieting_col)
            if pd.notna(cv):
                cv_int = int(float(cv))
                if cv_int > 0:
                    cont_str = f"连跌{cv_int}板"

        lines.append(f"\n{rank}. {code} {name} | 翘板评分 {score:.1f}")
        lines.append(f"   跌幅 {chg:.2f}% | 换手 {turn:.1f}% | {seal_str} | {cont_str}")
        lines.append(f"   信号: {detail_str}")
        if score >= 60:
            lines.append(f"   次日策略: 竞价观察，放量高开可博弈反抽，目标+3%~+5%止损-3%")
        elif score >= 35:
            lines.append(f"   次日策略: 仅观望，需竞价放量确认才有参与价值")
        else:
            lines.append(f"   次日策略: ❌ 不建议参与，无量封死跌停抛压大")

    # ── 构建返回数据（供全维度综合榜单使用） ──
    dtqiaoban_results = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        name = row[name_col]
        score = float(row['翘板评分'])
        dtqiaoban_results.append({'code': code, 'name': name, 'score': score, 'dimension': '跌停翘板'})

    lines.append(f"\n{'=' * 70}")
    lines.append("翘板策略: 放量+封单小+连续跌停 = 翘板信号 | 无量+封单大 = 回避")
    lines.append("风险提示: 跌停翘板属于极高风险博弈，务必轻仓+严格止损(-3%)")
    print("\n".join(lines))
    return dtqiaoban_results
