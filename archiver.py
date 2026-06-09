#!/usr/bin/env python3
"""
每日数据归档系统 — SQLite 本地存储，积累30+交易日数据后支持完整回测。

工作流（每交易日两阶段）：
  Day T 收盘后: 拉取涨停池+趋势候选+市场快照 → daily_stocks (T日数据)
  Day T+1 收盘后: 拉取次日涨跌幅 → 更新 daily_stocks.next_day_* 字段

表结构:
  daily_stocks:    每只股票每日评分因子+次日验证数据
  market_daily:    每日市场快照（情绪/涨跌比/热点板块）
  archive_log:     归档操作日志

用法:
  python archiver.py              # 自动检测阶段（盘中→跳过，盘后→运行Day T）
  python archiver.py --stage t    # 强制运行 Day T 阶段
  python archiver.py --stage t1   # 强制运行 Day T+1 阶段（更新前一天的next_day数据）
  python archiver.py --status     # 查看归档统计
"""

import sqlite3
import os
import sys
import json
import time
import pickle
from datetime import datetime, timedelta, timezone

from cache import _is_trading_day, _last_trading_date  # 交易日历工具
from config import CACHE_DIR as _CONFIG_CACHE_DIR

_CST = timezone(timedelta(hours=8))
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive.db")
_ARCHIVE_POOL_DIR = os.path.join(_CONFIG_CACHE_DIR, 'archive_pools')


def get_db():
    """获取数据库连接，自动创建表"""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_stocks (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            stock_type TEXT NOT NULL DEFAULT 'limit_up',
            change_pct REAL,
            price REAL,
            turnover REAL,
            seal_time TEXT,
            seal_fund REAL,
            zhaban_times INTEGER,
            consecutive INTEGER,
            industry TEXT,
            market_cap REAL,
            volume REAL,
            next_day_change REAL,
            next_day_open_change REAL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(trade_date, code, stock_type)
        );
        CREATE INDEX IF NOT EXISTS idx_ds_date ON daily_stocks(trade_date);
        CREATE INDEX IF NOT EXISTS idx_ds_code ON daily_stocks(code);
        CREATE INDEX IF NOT EXISTS idx_ds_type ON daily_stocks(stock_type);

        CREATE TABLE IF NOT EXISTS market_daily (
            trade_date TEXT PRIMARY KEY,
            limit_up_count INTEGER,
            zhaban_count INTEGER,
            dieting_count INTEGER,
            up_count INTEGER,
            down_count INTEGER,
            sentiment_score REAL,
            sentiment_level TEXT,
            hot_industries TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS archive_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT,
            stage TEXT,
            records_saved INTEGER,
            status TEXT,
            error_msg TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_log_date ON archive_log(trade_date);

        CREATE TABLE IF NOT EXISTS stock_daily (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            chg_pct REAL,
            PRIMARY KEY (code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_sd_code ON stock_daily(code);
        CREATE INDEX IF NOT EXISTS idx_sd_date ON stock_daily(trade_date);
    """)
    conn.commit()


def _today_str() -> str:
    """返回今天的日期 YYYYMMDD"""
    return datetime.now(_CST).strftime("%Y%m%d")


def _is_after_close() -> bool:
    """是否已过收盘时间 (>=15:00)"""
    now = datetime.now(_CST)
    return now.hour >= 15


def _log(conn, trade_date, stage, records, status, error=None):
    conn.execute(
        "INSERT INTO archive_log (trade_date, stage, records_saved, status, error_msg) VALUES (?,?,?,?,?)",
        (trade_date, stage, records, status, str(error)[:500] if error else None)
    )
    conn.commit()


# ═══════════════════════════════════════════
#  池数据 pickle 归档 (供回测引擎 fallback)
# ═══════════════════════════════════════════

def _ensure_archive_dir():
    """确保归档目录存在"""
    os.makedirs(_ARCHIVE_POOL_DIR, exist_ok=True)
    return _ARCHIVE_POOL_DIR


def _save_pool_pickle(trade_date: str, pool_type: str, df):
    """保存池原始 DataFrame 为 pickle，供回测引擎 akshare 不可用时 fallback

    Args:
        trade_date: YYYYMMDD
        pool_type: 'limit_up' | 'zhaban' | 'dtqiaoban' | 'strong' | 'prev_pool'
        df: akshare 返回的原始 DataFrame (含完整列结构)
    """
    if df is None:
        return
    try:
        if hasattr(df, 'empty') and df.empty:
            return
    except (ValueError, TypeError):
        pass
    _ensure_archive_dir()
    path = os.path.join(_ARCHIVE_POOL_DIR, f'{pool_type}_{trade_date}.pkl')
    try:
        with open(path, 'wb') as f:
            pickle.dump(df, f)
    except Exception as e:
        print(f"  [归档 pickle] {pool_type}_{trade_date} 保存失败: {e}", file=sys.stderr)


def _load_pool_pickle(trade_date: str, pool_type: str):
    """加载归档的池 DataFrame

    Returns:
        DataFrame or None (文件不存在/损坏时)
    """
    path = os.path.join(_ARCHIVE_POOL_DIR, f'{pool_type}_{trade_date}.pkl')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            df = pickle.load(f)
        if df is not None and hasattr(df, 'empty') and not df.empty:
            return df
        return None
    except Exception:
        return None


def list_archive_dates(pool_type: str = None):
    """列出已归档的交易日

    Args:
        pool_type: 池类型过滤，None 返回所有类型
    Returns:
        list of (date_str, pool_type) or list of date_str
    """
    if not os.path.exists(_ARCHIVE_POOL_DIR):
        return []
    result = []
    for fname in os.listdir(_ARCHIVE_POOL_DIR):
        if not fname.endswith('.pkl'):
            continue
        parts = fname.replace('.pkl', '').rsplit('_', 1)
        if len(parts) != 2:
            continue
        pt, dt = parts
        if pool_type and pt != pool_type:
            continue
        result.append((dt, pt) if pool_type is None else dt)
    return sorted(result, reverse=True)


# ═══════════════════════════════════════════
#  扫描输入归档 (fund_df/sentiment/lhb/history — 实时数据, 历史不可得)
# ═══════════════════════════════════════════

def save_scan_inputs(trade_date: str, fund_df, sentiment_score, sentiment_level,
                     sentiment_detail, sentiment_ok, lhb_bonus, history_scores):
    """保存每日扫描的实时输入数据, 供回测引擎历史回放使用。

    Args:
        trade_date: YYYYMMDD
        fund_df: DataFrame from fetch_fund_flow_data() or None
        sentiment_score/level/detail/ok: from detect_market_sentiment()
        lhb_bonus: Series from analyze_dragon_tiger() or empty Series
        history_scores: Series from score_stock_history()
    """
    _ensure_archive_dir()
    path = os.path.join(_ARCHIVE_POOL_DIR, f'scan_inputs_{trade_date}.pkl')
    data = {
        'fund_df': fund_df,
        'sentiment_score': sentiment_score,
        'sentiment_level': sentiment_level,
        'sentiment_detail': sentiment_detail,
        'sentiment_ok': sentiment_ok,
        'lhb_bonus': lhb_bonus,
        'history_scores': history_scores,
    }
    try:
        with open(path, 'wb') as f:
            pickle.dump(data, f)
    except Exception as e:
        print(f"  [归档 scan_inputs] {trade_date} 保存失败: {e}", file=sys.stderr)


def load_scan_inputs(trade_date: str):
    """加载历史扫描输入数据

    Returns:
        dict with fund_df/sentiment_*/lhb_bonus/history_scores, or None
    """
    path = os.path.join(_ARCHIVE_POOL_DIR, f'scan_inputs_{trade_date}.pkl')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return None


# ═══════════════════════════════════════════
#  Day T 阶段: 拉取当日数据
# ═══════════════════════════════════════════

def archive_day_t(trade_date: str = None):
    """
    归档 Day T 数据：涨停池 + 上交易日涨停今日续涨(趋势候选) + 市场快照。
    trade_date: YYYYMMDD，默认今天
    """
    if trade_date is None:
        trade_date = _today_str()

    print(f"[归档 Day T] {trade_date} 开始...")
    conn = get_db()
    total = 0

    try:
        import akshare as ak
        import pandas as pd

        # ── 1. 涨停池 ──
        print("  [1/4] 拉取涨停池...")
        try:
            pool = ak.stock_zt_pool_em(date=trade_date)
            if pool is not None and not pool.empty:
                from scanner import filter_non_main_board
                pool = filter_non_main_board(pool)
                count = _save_limit_up_pool(conn, trade_date, pool)
                _save_pool_pickle(trade_date, 'limit_up', pool)  # 归档原始 DF
                _log(conn, trade_date, 'day_t_limit_up', count, 'success')
                total += count
                print(f"    涨停池: {count} 只")
            else:
                _log(conn, trade_date, 'day_t_limit_up', 0, 'empty')
                print("    涨停池: 空")
        except Exception as e:
            _log(conn, trade_date, 'day_t_limit_up', 0, 'error', e)
            print(f"    涨停池错误: {e}")

        # ── 2. 上交易日涨停今日续涨（趋势候选） ──
        print("  [2/4] 拉取趋势候选(上交易日涨停今日表现)...")
        try:
            prev = ak.stock_zt_pool_previous_em(date=trade_date)
            if prev is not None and not prev.empty:
                _save_pool_pickle(trade_date, 'prev_pool', prev)  # 归档原始 DF
                from scanner import filter_non_main_board
                prev = filter_non_main_board(prev)
                # 涨幅2-9%为趋势候选
                chg_col = prev.columns[3] if '涨跌幅' in prev.columns else prev.columns[3]
                prev_vals = prev[chg_col].astype(float)
                trend = prev[(prev_vals >= 2) & (prev_vals < 9)].copy()
                count = _save_trend_stocks(conn, trade_date, trend)
                _log(conn, trade_date, 'day_t_trend', count, 'success')
                total += count
                print(f"    趋势候选: {count} 只 (上交易日涨停共{len(prev)}只)")
            else:
                _log(conn, trade_date, 'day_t_trend', 0, 'empty')
                print("    趋势候选: 空")
        except Exception as e:
            _log(conn, trade_date, 'day_t_trend', 0, 'error', e)
            print(f"    趋势候选错误: {e}")

        # ── 3. 市场快照 ──
        print("  [3/4] 拉取市场快照...")
        try:
            _save_market_snapshot(conn, trade_date)
            _log(conn, trade_date, 'day_t_market', 1, 'success')
            print("    市场快照: 已保存")
        except Exception as e:
            _log(conn, trade_date, 'day_t_market', 0, 'error', e)
            print(f"    市场快照错误: {e}")

        # ── 4. 尝试更新前一天的 next_day 数据 ──
        print("  [4/4] 更新上交易日 next_day 数据...")
        try:
            yesterday = _last_trading_date(trade_date)
            updated = _update_next_day_data(conn, yesterday, trade_date)
            if updated > 0:
                _log(conn, yesterday, 'day_t1', updated, 'success')
                print(f"    更新上交易日数据: {updated} 只")
        except Exception as e:
            print(f"    更新上交易日数据错误: {e}")

    except Exception as e:
        print(f"  [归档] 严重错误: {e}")
        _log(conn, trade_date, 'day_t', 0, 'error', e)
    finally:
        conn.close()

    print(f"[归档 Day T] 完成，共保存 {total} 条记录")


def _calc_volume_5d_avg(code: str, trade_date: str, lookback_days: int = 5) -> tuple:
    """
    计算近 N 日涨停日均量,用于量比。
    返回 (avg_volume, sample_days)。
    - 0 天数据 → (None, 0)
    - N 天数据 → (avg, N)
    """
    from cache import _last_trading_date
    try:
        # 用 _last_trading_date 链式找 N 个交易日(自动处理周末+节假日)
        trading_days = [trade_date]
        cur = trade_date
        for _ in range(lookback_days - 1):
            cur = _last_trading_date(cur)
            trading_days.append(cur)
        conn = get_db()
        placeholders = ','.join('?' * len(trading_days))
        r = conn.execute(
            f"SELECT AVG(volume), COUNT(*) FROM daily_stocks "
            f"WHERE code=? AND trade_date IN ({placeholders}) AND volume IS NOT NULL AND volume > 0",
            [code] + trading_days
        ).fetchone()
        conn.close()
        avg, cnt = r[0], r[1]
        if avg is None or cnt == 0:
            return (None, 0)
        return (float(avg), int(cnt))
    except Exception:
        return (None, 0)


def _save_limit_up_pool(conn, trade_date, df):
    """保存涨停池到 daily_stocks (stock_type='limit_up')"""
    import pandas as _pd
    code_col = '代码' if '代码' in df.columns else df.columns[1]
    name_col = '名称' if '名称' in df.columns else df.columns[2]
    chg_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    to_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    st_col = '首次封板时间' if '首次封板时间' in df.columns else (df.columns[11] if len(df.columns) > 11 else None)
    sf_col = '封板资金' if '封板资金' in df.columns else (df.columns[14] if len(df.columns) > 14 else None)
    zb_col = '炸板次数' if '炸板次数' in df.columns else (df.columns[12] if len(df.columns) > 12 else None)
    lb_col = '连板数' if '连板数' in df.columns else (df.columns[13] if len(df.columns) > 13 else None)
    ind_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)
    cap_col = '流通市值' if '流通市值' in df.columns else (df.columns[6] if len(df.columns) > 6 else None)
    vol_col = '成交额' if '成交额' in df.columns else (df.columns[5] if len(df.columns) > 5 else None)

    count = 0
    for _, row in df.iterrows():
        try:
            code = str(row[code_col]).strip().zfill(6)
            name = str(row[name_col])
            conn.execute("""
                INSERT OR REPLACE INTO daily_stocks
                (trade_date, code, name, stock_type, change_pct, price, turnover,
                 seal_time, seal_fund, zhaban_times, consecutive, industry, market_cap, volume)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade_date, code, name, 'limit_up',
                float(row[chg_col]) if _pd.notna(row[chg_col]) else None,
                float(row[price_col]) if _pd.notna(row[price_col]) else None,
                float(row[to_col]) if to_col and _pd.notna(row[to_col]) else None,
                str(row[st_col])[:8] if st_col and _pd.notna(row[st_col]) else None,
                float(row[sf_col]) if sf_col and _pd.notna(row[sf_col]) else None,
                int(float(row[zb_col])) if zb_col and _pd.notna(row[zb_col]) else 0,
                int(float(row[lb_col])) if lb_col and _pd.notna(row[lb_col]) else 1,
                str(row[ind_col]) if ind_col and _pd.notna(row[ind_col]) else '',
                float(row[cap_col]) if cap_col and _pd.notna(row[cap_col]) else None,
                float(row[vol_col]) if vol_col and _pd.notna(row[vol_col]) else None,
            ))
            count += 1
        except Exception:
            continue
    conn.commit()
    return count


def _save_trend_stocks(conn, trade_date, df):
    """保存趋势候选到 daily_stocks (stock_type='trend')"""
    import pandas as _pd
    code_col = '代码' if '代码' in df.columns else df.columns[1]
    name_col = '名称' if '名称' in df.columns else df.columns[2]
    chg_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
    price_col = '最新价' if '最新价' in df.columns else df.columns[4]
    to_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    lb_col = '连板数' if '连板数' in df.columns else (df.columns[13] if len(df.columns) > 13 else None)
    ind_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)
    cap_col = '流通市值' if '流通市值' in df.columns else (df.columns[6] if len(df.columns) > 6 else None)
    vol_col = '成交额' if '成交额' in df.columns else (df.columns[5] if len(df.columns) > 5 else None)

    count = 0
    for _, row in df.iterrows():
        try:
            code = str(row[code_col]).strip().zfill(6)
            name = str(row[name_col])
            conn.execute("""
                INSERT OR REPLACE INTO daily_stocks
                (trade_date, code, name, stock_type, change_pct, price, turnover,
                 consecutive, industry, market_cap, volume)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade_date, code, name, 'trend',
                float(row[chg_col]) if _pd.notna(row[chg_col]) else None,
                float(row[price_col]) if _pd.notna(row[price_col]) else None,
                float(row[to_col]) if to_col and _pd.notna(row[to_col]) else None,
                int(float(row[lb_col])) if lb_col and _pd.notna(row[lb_col]) else 1,
                str(row[ind_col]) if ind_col and _pd.notna(row[ind_col]) else '',
                float(row[cap_col]) if cap_col and _pd.notna(row[cap_col]) else None,
                float(row[vol_col]) if vol_col and _pd.notna(row[vol_col]) else None,
            ))
            count += 1
        except Exception:
            continue
    conn.commit()
    return count


def _save_market_snapshot(conn, trade_date):
    """保存市场快照 + 各池原始 DataFrame pickle 归档"""
    import akshare as ak
    import pandas as _pd

    limit_up_count = 0
    zhaban_count = 0
    dieting_count = 0

    try:
        pool = ak.stock_zt_pool_em(date=trade_date)
        if pool is not None and not pool.empty:
            limit_up_count = len(pool)
    except Exception as e:
        print(f"  [archiver L312] failed: {e}", file=sys.stderr)

    # 炸板池 — 保存原始 DF 供回测引擎 fallback
    zb = None
    try:
        zb = ak.stock_zt_pool_zbgc_em(date=trade_date)
        if zb is not None and not zb.empty:
            zhaban_count = len(zb)
            _save_pool_pickle(trade_date, 'zhaban', zb)
    except Exception as e:
        print(f"  [archiver L319] failed: {e}", file=sys.stderr)

    # 跌停/翘板池 — 保存原始 DF 供回测引擎 fallback
    dt = None
    try:
        dt = ak.stock_zt_pool_dtgc_em(date=trade_date)
        if dt is not None and not dt.empty:
            dieting_count = len(dt)
            _save_pool_pickle(trade_date, 'dtqiaoban', dt)
    except Exception as e:
        print(f"  [archiver L326] failed: {e}", file=sys.stderr)

    # 强势池 (趋势 tab 信号源) — 保存原始 DF 供回测引擎 fallback
    strong_count = 0
    try:
        strong_df = ak.stock_zt_pool_strong_em(date=trade_date)
        if strong_df is not None and not strong_df.empty:
            strong_count = len(strong_df)
            _save_pool_pickle(trade_date, 'strong', strong_df)
    except Exception as e:
        print(f"  [archiver strong] {trade_date} failed: {e}", file=sys.stderr)

    # 全市场涨跌比（采样方式）
    up_count, down_count = 0, 0
    try:
        import requests
        # 用akshare的stock_zh_a_spot_em采样
        spot = ak.stock_zh_a_spot_em()
        if spot is not None and not spot.empty:
            chg_col = '涨跌幅' if '涨跌幅' in spot.columns else spot.columns[3]
            changes = spot[chg_col].astype(float)
            up_count = int((changes > 0).sum())
            down_count = int((changes < 0).sum())
    except Exception as e:
        print(f"  [archiver L340] failed: {e}", file=sys.stderr)

    # 热点行业
    hot_industries = "[]"
    try:
        if limit_up_count > 0:
            pool = ak.stock_zt_pool_em(date=trade_date)
            if pool is not None and not pool.empty:
                ind_col = '所属行业' if '所属行业' in pool.columns else (pool.columns[15] if len(pool.columns) > 15 else None)
                if ind_col:
                    hot = pool[ind_col].value_counts().head(5)
                    hot_industries = json.dumps(list(hot[hot >= 3].index), ensure_ascii=False)
    except Exception as e:
        print(f"  [archiver L353] failed: {e}", file=sys.stderr)

    conn.execute("""
        INSERT OR REPLACE INTO market_daily
        (trade_date, limit_up_count, zhaban_count, dieting_count, up_count, down_count, hot_industries)
        VALUES (?,?,?,?,?,?,?)
    """, (trade_date, limit_up_count, zhaban_count, dieting_count, up_count, down_count, hot_industries))
    conn.commit()


# ═══════════════════════════════════════════
#  Day T+1 阶段: 更新前一天的 next_day
# ═══════════════════════════════════════════

def _update_next_day_data(conn, target_date, current_date):
    """
    更新 target_date 的股票的 next_day 数据。
    target_date: 需要补数据的交易日（前一天）
    current_date: 当前日期（今天，用于取次日数据）
    """
    # 检查是否已有数据
    existing = conn.execute(
        "SELECT COUNT(*) FROM daily_stocks WHERE trade_date=? AND next_day_change IS NOT NULL",
        (target_date,)
    ).fetchone()[0]
    if existing > 10:
        return 0  # 已经有足够数据，跳过

    import akshare as ak
    import pandas as _pd

    # 方法1: 用 stock_zt_pool_previous_em 获取上交易日涨停股的今日表现
    updated = 0
    try:
        prev = ak.stock_zt_pool_previous_em(date=current_date)
        if prev is not None and not prev.empty:
            code_col = '代码' if '代码' in prev.columns else prev.columns[1]
            chg_col = '涨跌幅' if '涨跌幅' in prev.columns else prev.columns[3]

            for _, row in prev.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                chg = float(row[chg_col]) if _pd.notna(row[chg_col]) else None
                if chg is not None:
                    conn.execute(
                        "UPDATE daily_stocks SET next_day_change=?, updated_at=datetime('now','localtime') WHERE trade_date=? AND code=? AND next_day_change IS NULL",
                        (chg, target_date, code)
                    )
                    if conn.execute("SELECT changes()").fetchone()[0] > 0:
                        updated += 1
            conn.commit()
    except Exception as e:
        print(f"      更新next_day错误: {e}")

    return updated


# ═══════════════════════════════════════════
#  查询/统计接口
# ═══════════════════════════════════════════

def get_status():
    """返回归档统计"""
    conn = get_db()
    try:
        stats = {}

        # 总天数
        stats['total_days'] = conn.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM daily_stocks"
        ).fetchone()[0]

        # 涨停股总数
        stats['limit_up_total'] = conn.execute(
            "SELECT COUNT(*) FROM daily_stocks WHERE stock_type='limit_up'"
        ).fetchone()[0]

        # 趋势股总数
        stats['trend_total'] = conn.execute(
            "SELECT COUNT(*) FROM daily_stocks WHERE stock_type='trend'"
        ).fetchone()[0]

        # 有次日数据的天数
        stats['days_with_next_day'] = conn.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM daily_stocks WHERE next_day_change IS NOT NULL"
        ).fetchone()[0]

        # 有次日数据的股票数
        stats['stocks_with_next_day'] = conn.execute(
            "SELECT COUNT(*) FROM daily_stocks WHERE next_day_change IS NOT NULL"
        ).fetchone()[0]

        # 每日统计
        stats['daily'] = []
        for row in conn.execute("""
            SELECT trade_date,
                   SUM(CASE WHEN stock_type='limit_up' THEN 1 ELSE 0 END) as lu,
                   SUM(CASE WHEN stock_type='trend' THEN 1 ELSE 0 END) as tr,
                   SUM(CASE WHEN next_day_change IS NOT NULL THEN 1 ELSE 0 END) as nd
            FROM daily_stocks
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT 10
        """):
            stats['daily'].append({
                'date': row[0],
                'limit_up': row[1],
                'trend': row[2],
                'with_next_day': row[3]
            })

        # 市场快照天数
        stats['market_days'] = conn.execute(
            "SELECT COUNT(*) FROM market_daily"
        ).fetchone()[0]

        return stats
    finally:
        conn.close()


def print_status():
    """打印归档状态"""
    import pandas as pd
    stats = get_status()
    print(f"\n{'='*60}")
    print(f"  归档数据库状态")
    print(f"{'='*60}")
    print(f"  文件: {_DB_PATH}")
    print(f"  交易日: {stats['total_days']} 天")
    print(f"  涨停股: {stats['limit_up_total']} 只")
    print(f"  趋势股: {stats['trend_total']} 只")
    print(f"  有次日数据: {stats['stocks_with_next_day']}/{stats['limit_up_total'] + stats['trend_total']} 只 ({stats['days_with_next_day']}天)")
    print(f"  市场快照: {stats['market_days']} 天")
    print(f"\n  最近10天:")
    df = pd.DataFrame(stats['daily'])
    if not df.empty:
        df.columns = ['日期', '涨停', '趋势', '有次日']
        print(df.to_string(index=False))
    print()


# ═══════════════════════════════════════════
#  回测数据导出
# ═══════════════════════════════════════════

def export_backtest_csv(output_path: str = None):
    """
    导出完整回测数据集为CSV。
    需要至少30天数据才能做有意义的回测。
    """
    conn = get_db()
    try:
        df = pd.read_sql_query("""
            SELECT trade_date as date, code, name, stock_type,
                   change_pct, price, turnover, seal_time, seal_fund,
                   zhaban_times, consecutive, industry, market_cap, volume,
                   next_day_change
            FROM daily_stocks
            WHERE next_day_change IS NOT NULL
            ORDER BY trade_date, stock_type, code
        """, conn)
        if output_path is None:
            output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       f"backtest_archive_{_today_str()}.csv")
        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        print(f"导出 {len(df)} 条到 {output_path}")
        return output_path
    finally:
        conn.close()


# ═══════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════

def _should_skip() -> bool:
    """盘中跳过（数据不完整），周末跳过"""
    now = datetime.now(_CST)
    if now.weekday() >= 5:
        return True
    if not _is_after_close():
        return True
    return False


# ═══════════════════════════════════════════
#  快速批量历史数据（腾讯源+缓存，回测核心）
# ═══════════════════════════════════════════

def batch_fetch_history(codes, start_date, end_date, max_workers=8):
    """
    批量获取个股历史涨跌幅，缓存到 archive.db。
    - 先查缓存，只拉取缺失的
    - ThreadPoolExecutor 并行加速
    - 返回 {code: {date_str: chg_pct}}
    """
    conn = get_db()

    # 1. 查缓存
    cached = {}
    placeholders = ','.join('?' * len(codes))
    rows = conn.execute(
        f"SELECT code, trade_date, chg_pct FROM stock_daily WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ?",
        codes + [start_date, end_date]
    ).fetchall()
    for code, d, chg in rows:
        cached.setdefault(code, {})[d] = chg

    # 2. 找出缺失
    missing = [c for c in codes if c not in cached or len(cached[c]) < 3]
    if not missing:
        conn.close()
        return cached

    # 3. 并行拉取腾讯源
    print(f"  [批量拉取] {len(missing)} 只股票 ({len(codes)-len(missing)} 已缓存)...", file=sys.stderr)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import akshare as ak
    import pandas as pd

    start_fmt = f'{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}'
    end_fmt = f'{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}'

    def _fetch_one(code):
        prefix = 'sh' if code.startswith('6') else 'sz'
        try:
            df = ak.stock_zh_a_hist_tx(symbol=f'{prefix}{code}', start_date=start_fmt, end_date=end_fmt)
            if df is None or df.empty: return code, {}
            df['chg'] = df['close'].pct_change() * 100
            df['d'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
            return code, dict(zip(df['d'], df['chg'].round(2)))
        except:
            return code, {}

    fetched = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_one, c): c for c in missing}
        for f in as_completed(futures):
            code, data = f.result()
            if data:
                cached.setdefault(code, {}).update(data)
                # 写入缓存
                for d, chg in data.items():
                    try:
                        conn.execute("INSERT OR IGNORE INTO stock_daily (code, trade_date, chg_pct) VALUES (?,?,?)",
                                     (code, d, chg))
                    except: pass
                conn.commit()
            fetched += 1

    conn.close()
    print(f"  [批量拉取] 完成, 共 {len(cached)} 只有效数据", file=sys.stderr)
    return cached


def get_cached_history(code, start_date, end_date):
    """从缓存读取单只股票历史涨跌幅 {date_str: chg_pct}"""
    conn = get_db()
    rows = conn.execute(
        "SELECT trade_date, chg_pct FROM stock_daily WHERE code=? AND trade_date BETWEEN ? AND ?",
        (code, start_date, end_date)
    ).fetchall()
    conn.close()
    return {d: chg for d, chg in rows}


# ═══════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="每日数据归档系统")
    parser.add_argument('--stage', choices=['t', 't1', 'auto'], default='auto',
                        help='归档阶段: t=拉取当日数据, t1=补上交易日next_day, auto=自动检测')
    parser.add_argument('--date', type=str, default=None,
                        help='指定日期 YYYYMMDD（默认今天）')
    parser.add_argument('--status', action='store_true', help='查看归档状态')
    parser.add_argument('--force', action='store_true', help='强制运行（忽略盘中检测）')
    args = parser.parse_args()

    if args.status:
        print_status()
        sys.exit(0)

    trade_date = args.date or _today_str()

    if args.stage == 'auto':
        if not args.force and _should_skip():
            print(f"[归档] 盘中/周末跳过 (force=True 可强制)")
            sys.exit(0)
        # 自动：先做 Day T，然后尝试更新前一天的 next_day
        archive_day_t(trade_date)

    elif args.stage == 't':
        archive_day_t(trade_date)

    elif args.stage == 't1':
        trade_date = args.date or _today_str()
        conn = get_db()
        try:
            updated = _update_next_day_data(conn, trade_date, _today_str())
            print(f"[归档 Day T+1] {trade_date} 更新 {updated} 条 next_day 数据")
            _log(conn, trade_date, 'day_t1', updated, 'success')
        except Exception as e:
            print(f"[归档 Day T+1] 错误: {e}")
            _log(conn, trade_date, 'day_t1', 0, 'error', e)
        finally:
            conn.close()
