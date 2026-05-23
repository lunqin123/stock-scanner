#!/usr/bin/env python3
"""
社区热评 + 新闻聚合模块
为超短线选股扫描器提供舆情数据支持
"""
import json
import os
import sys
import time

try:
    import akshare as ak
except ImportError:
    ak = None

# ─── 本地缓存（日内数据不常变，避免重复拉取） ───
_CACHE_DIR = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "claude_stock_cache")
_CACHE_TTL = 7200  # 2小时

def _cache_get(key):
    path = os.path.join(_CACHE_DIR, f"{key}.json")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < _CACHE_TTL:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None

def _cache_set(key, data):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = os.path.join(_CACHE_DIR, f"{key}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=None, separators=(',', ':'))
    except Exception:
        pass

TOP_NEWS = 3       # 每只股票取前N条新闻
TOP_STOCKS = 10    # 查新闻的目标股票数


def fetch_news_for_stocks(df, top_n=TOP_STOCKS):
    """对 TOP N 股票获取东方财富个股新闻（并行）"""
    if ak is None:
        return {}
    codes = []
    for _, row in df.head(top_n).iterrows():
        code = str(row.get('代码', '')).strip().zfill(6)
        codes.append((code, row.get('名称', '')))

    def _fetch_one(code, name):
        try:
            news_df = ak.stock_news_em(code)
            if news_df is not None and not news_df.empty:
                items = []
                for _, nr in news_df.head(TOP_NEWS).iterrows():
                    title = str(nr.get('title', nr.get('新闻标题', '')))[:80]
                    t = str(nr.get('public_time', nr.get('发布时间', '')))[:19]
                    items.append({'title': title, 'time': t})
                return code, {'name': name, 'news': items}
        except Exception:
            pass
        return None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    result = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_one, code, name): code for code, name in codes}
        for f in as_completed(futures):
            try:
                r = f.result()
                if r:
                    result[r[0]] = r[1]
            except Exception:
                pass
    return result


def fetch_guba_rank():
    """东方财富股吧人气排名 TOP100，带缓存"""
    if ak is None:
        return {}
    cache_key = "guba_rank"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    try:
        df = ak.stock_hot_rank_em()
        if df is not None and not df.empty:
            result = {
                str(row['代码']).strip().zfill(6): {
                    'rank': int(row['当前排名']),
                    'name': row['股票名称'],
                }
                for _, row in df.iterrows()
            }
            _cache_set(cache_key, result)
            return result
    except Exception:
        pass
    return {}


def fetch_comment_scores():
    """东方财富千股千评（综合评分、机构参与度），带缓存"""
    if ak is None:
        return {}
    cache_key = "comment_scores"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    try:
        df = ak.stock_comment_em()
        if df is not None and not df.empty:
            result = {}
            for _, row in df.iterrows():
                code = str(row['代码']).strip().zfill(6)
                result[code] = {
                    '综合得分': row.get('综合得分'),
                    '机构参与度': row.get('机构参与度'),
                    '关注指数': row.get('关注指数'),
                }
            _cache_set(cache_key, result)
            return result
    except Exception:
        pass
    return {}


def fetch_xueqiu_rank():
    """雪球关注榜，带缓存"""
    if ak is None:
        return {}
    cache_key = "xueqiu_rank"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    try:
        df = ak.stock_hot_follow_xq()
        if df is not None and not df.empty:
            result = {}
            for i, (_, row) in enumerate(df.iterrows(), 1):
                code = None
                for col in ['code', '股票代码', '代码']:
                    if col in row:
                        code = str(row[col]).strip().zfill(6)
                        break
                if code:
                    result[code] = {'xq_rank': i, 'name': row.get('股票名称', '')}
            _cache_set(cache_key, result)
            return result
    except Exception:
        pass
    return {}


def fetch_global_news():
    """同花顺全局资讯 + 财联社快讯"""
    if ak is None:
        return []
    items = []
    try:
        ths = ak.stock_info_global_ths()
        if ths is not None and not ths.empty:
            for _, row in ths.head(5).iterrows():
                items.append({
                    'source': '同花顺',
                    'title': str(row.iloc[0])[:80],
                    'time': str(row.iloc[1])[:19] if len(row) > 1 else '',
                })
    except Exception:
        pass
    try:
        cls = ak.stock_info_global_cls()
        if cls is not None and not cls.empty:
            for _, row in cls.head(5).iterrows():
                items.append({
                    'source': '财联社',
                    'title': str(row.iloc[0])[:80],
                    'time': str(row.iloc[1])[:19] if len(row) > 1 else '',
                })
    except Exception:
        pass
    return items


def build_sentiment_map(df, guba, comments, news):
    """汇总所有舆情数据到每只标的"""
    smap = {}
    for _, row in df.iterrows():
        code = str(row.get('代码', '')).strip().zfill(6)
        e = {'name': row.get('名称', ''), 'code': code}
        if code in guba:
            e['guba_rank'] = guba[code]['rank']
        if code in comments:
            e['comment_score'] = comments[code].get('综合得分')
            e['机构参与度'] = comments[code].get('机构参与度')
        if code in news:
            e['news'] = news[code]['news']
        smap[code] = e
    return smap


def format_output(smap, global_news):
    """格式化输出舆情报告"""
    lines = ["", "=" * 60, "  舆情摘要 | 新闻 + 社区热度", "=" * 60]

    # 财经要闻
    if global_news:
        lines.append("\n【财经要闻】")
        for item in global_news[:5]:
            lines.append(f"  [{item['source']}] {item['title'][:60]}")

    # 个股新闻
    news_stocks = [(c, d) for c, d in smap.items() if d.get('news')]
    if news_stocks:
        lines.append("\n【个股新闻】")
        for code, data in news_stocks[:5]:
            for n in data['news'][:2]:
                lines.append(f"  {data['name']}({code}): {n['title'][:60]}")

    # 股吧热度
    ranked = [(c, d) for c, d in smap.items() if d.get('guba_rank')]
    if ranked:
        ranked.sort(key=lambda x: x[1]['guba_rank'])
        lines.append("\n【股吧热度】")
        for code, data in ranked[:5]:
            lines.append(f"  #{data['guba_rank']} {data['name']}({code})")

    # 千股千评高分标的
    high_score = [(c, d) for c, d in smap.items() if d.get('comment_score') is not None]
    if high_score:
        high_score.sort(key=lambda x: x[1]['comment_score'] or 0, reverse=True)
        lines.append("\n【千股千评高分】")
        for code, data in high_score[:3]:
            lines.append(f"  {data['name']}({code}): 综合{data['comment_score']}")

    lines.append("")
    return "\n".join(lines)


def compute_community_scores(comments: dict, guba: dict, df: pd.DataFrame) -> pd.Series:
    """
    将舆情数据量化为 0-7 分，按每只标的。
    - 千股千评综合得分 (0-3): ≥80→3, ≥70→2, ≥60→1, ≥50→0.5
    - 机构参与度 (0-2): 数值 0-1 按比例映射，无数据则 0
    - 股吧热度排名 (0-2): 前10→2, 前30→1.5, 前50→1, 前100→0.5
    - 无任何舆情数据的股票 → 1.0（象征性存在感）
    """
    import pandas as pd
    scores = pd.Series(1.0, index=df.index)  # 无数据时仅1分
    for idx in df.index:
        code = str(df.loc[idx, '代码']).strip().zfill(6)
        total = 0.0

        # 千股千评 (0-3)
        if code in comments:
            cs = comments[code].get('综合得分')
            if cs is not None:
                try:
                    cs = float(cs)
                    if cs >= 80:
                        total += 3
                    elif cs >= 70:
                        total += 2
                    elif cs >= 60:
                        total += 1
                    elif cs >= 50:
                        total += 0.5
                except (ValueError, TypeError):
                    pass

            # 机构参与度 (0-2): 实际为 0-1 数值
            inst = comments[code].get('机构参与度')
            if inst is not None:
                try:
                    inst_val = float(inst)
                    total += min(2, inst_val * 2)  # 0-1 → 0-2
                except (ValueError, TypeError):
                    pass

        # 股吧热度排名 (0-2)
        if code in guba:
            rank = guba[code].get('rank', 999)
            if rank <= 10:
                total += 2
            elif rank <= 30:
                total += 1.5
            elif rank <= 50:
                total += 1
            elif rank <= 100:
                total += 0.5

        scores[idx] = max(1.0, min(7, total))

    return scores.round(1)


def score_community(df: pd.DataFrame) -> pd.Series:
    """
    舆情评分入口：对候选股池计算每只标的的舆情评分 (0-7)。
    返回与 df.index 对齐的 Series。
    只拉取一次缓存数据（run() 会再拉一次但命中缓存，无额外开销）。
    """
    guba = fetch_guba_rank()
    comments = fetch_comment_scores()
    return compute_community_scores(comments, guba, df)


# ─── 兼容旧接口 ───
def run(df, top_n=TOP_STOCKS):
    """主入口：对候选榜单执行舆情聚合（并行）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(_safe, fetch_guba_rank, {}): "guba",
            ex.submit(_safe, fetch_comment_scores, {}): "comments",
            ex.submit(_safe, lambda: fetch_news_for_stocks(df, top_n), {}): "news",
            ex.submit(_safe, fetch_global_news, []): "gl",
        }
        res = {}
        for f in as_completed(futs):
            key = futs[f]
            try:
                res[key] = f.result()
            except Exception:
                res[key] = futs[key]  # fallback to default
    guba = res.get("guba", {})
    comments = res.get("comments", {})
    news = res.get("news", {})
    gl = res.get("gl", [])

    try:
        smap = build_sentiment_map(df, guba, comments, news)
        output = format_output(smap, gl)
    except Exception:
        output = ""
        smap = {}
    return output, smap
