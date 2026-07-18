#!/usr/bin/env python3
"""
缓存工具模块：2小时短期缓存 + 每日收盘缓存
"""
import os
import sys
import json
import time
import pickle as _pickle
from datetime import date, datetime, timezone, timedelta

# 缓存目录 + TTL 统一从 config 导入 (P1-1 重构)
from config import CACHE_DIR as _CACHE_DIR, CACHE_TTL as _CACHE_TTL

# 缓存版本号：每次变更数据格式/运算逻辑后 +1
# BUG-6 修复: v8→v9 同步 P0/P1/P2 评分逻辑大改 — 旧 daily 缓存里的 total_score/base_score
# 仍是旧公式算的, 不 bump 的话用户看到的是旧分 (新代码已生效但缓存命中)
_CACHE_VER = 13  # v12→v13: v3.4b 趋势涨幅反转+价格因子+炸板市值反转, 旧缓存评分无效
# 旧 daily 缓存里的 total_score 是用 seal=9.2/money=0.4 等退化权重算的,
# 恢复 seal=28/money=17 默认权重后, 旧缓存评分与当前评分不一致, 必须 bump

# ─── 2小时短期缓存（避免重复拉取慢 API） ───

def get(name):
    path = os.path.join(_CACHE_DIR, f"{name}_v{_CACHE_VER}.pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < _CACHE_TTL:
            with open(path, 'rb') as f:
                return _pickle.load(f)
    except Exception as e:
        print(f"  [cache get] 读取失败 ({name}): {e}", file=sys.stderr)
    return None

def put(name, data):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_CACHE_DIR, f"{name}_v{_CACHE_VER}.pkl"), 'wb') as f:
            _pickle.dump(data, f)
    except Exception as e:
        print(f"  [cache put] 写入失败 ({name}): {e}", file=sys.stderr)


# ─── 持久化缓存 (无 TTL, 用于历史 OHLCV 等不变数据) ───

def persistent_get(name):
    """读取持久化缓存 (无 TTL 限制, 文件存在即有效)

    BUG-7 修复: 升级 _CACHE_VER 后, 所有 _v8.pkl 历史归档找不到 _v9.pkl
    (例: engine_limit_up_20260629_v8.pkl 在 8→9 升级后失效)
    加 fallback: 找不到 v_current 时, 扫描 _v{N}.pkl (N < current) 找最新版本
    并自动迁移 (rename) 到 v_current, 一次性解决历史兼容问题
    """
    path = os.path.join(_CACHE_DIR, f"{name}_v{_CACHE_VER}.pkl")
    try:
        if os.path.exists(path):
            with open(path, 'rb') as f:
                return _pickle.load(f)
    except Exception as e:
        print(f"  [cache persistent_get] 读取失败 ({name}): {e}", file=sys.stderr)
        return None
    # BUG-7 fallback: 升级版本后, 旧 _v{N}.pkl 找不到 → 自动迁移
    try:
        if not os.path.isdir(_CACHE_DIR):
            return None
        prefix = f"{name}_v"
        candidates = []
        for fname in os.listdir(_CACHE_DIR):
            if fname.startswith(prefix) and fname.endswith('.pkl'):
                # 提取版本号
                try:
                    ver = int(fname[len(prefix):-4])
                    if ver < _CACHE_VER:
                        candidates.append((ver, fname))
                except ValueError:
                    continue
        if not candidates:
            return None
        # 取最新旧版本
        candidates.sort(reverse=True)
        old_ver, old_fname = candidates[0]
        old_path = os.path.join(_CACHE_DIR, old_fname)
        with open(old_path, 'rb') as f:
            data = _pickle.load(f)
        # 自动迁移: rename 到当前版本
        try:
            os.rename(old_path, path)
            print(f"  [cache persistent_get] 迁移 {old_fname} → {os.path.basename(path)}", file=sys.stderr)
        except Exception:
            pass  # 迁移失败也返数据, 不影响本次调用
        return data
    except Exception as e:
        print(f"  [cache persistent_get] 旧版本迁移失败 ({name}): {e}", file=sys.stderr)
        return None


def persistent_put(name, data):
    """写入持久化缓存 (无 TTL)"""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_CACHE_DIR, f"{name}_v{_CACHE_VER}.pkl"), 'wb') as f:
            _pickle.dump(data, f)
    except Exception as e:
        print(f"  [cache persistent_put] 写入失败 ({name}): {e}", file=sys.stderr)


# ─── 每日缓存（日内持久化，避免重复扫描；按日期自动隔离） ───

_CST = timezone(timedelta(hours=8))

def _trading_date() -> str:
    """返回当前交易日的日期：凌晨/周末/节假日归为上一个交易日"""
    now = datetime.now(_CST)
    d = now
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        d = now - timedelta(hours=12)
    for _ in range(10):
        d_str = d.strftime("%Y-%m-%d")
        yyyymmdd = d.strftime("%Y%m%d")
        if d.weekday() < 5 and _is_trading_day(yyyymmdd):
            return d_str
        d -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")  # fallback

def _last_trading_date(date_str: str = None) -> str:
    """返回指定日期(或今天)的上个交易日,YYYYMMDD 格式。
    跳过周末+节假日(正确处理周一/节假日后第一天)。
    date_str: 可选,YYYYMMDD 8位;不传则用当前交易日
    """
    if date_str is None:
        date_str = _trading_date().replace('-', '')
    cur = datetime.strptime(date_str, '%Y%m%d')
    for _ in range(10):
        cur = cur - timedelta(days=1)
        s = cur.strftime('%Y%m%d')
        if cur.weekday() < 5 and _is_trading_day(s):
            return s
    return cur.strftime('%Y%m%d')  # fallback

def _daily_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"daily_{_trading_date()}_{key}_v{_CACHE_VER}.json")

def daily_get(key: str):
    path = _daily_path(key)
    try:
        if os.path.exists(path):
            now = datetime.now(_CST)
            mtime = os.path.getmtime(path)
            mtime_dt = datetime.fromtimestamp(mtime, _CST)
            # 缓存文件是前一个交易日创建的 → 过期（如周五缓存周一看）
            today_t = _trading_date()
            mtime_file_date = mtime_dt.strftime("%Y-%m-%d")
            if mtime_file_date < today_t:
                os.remove(path)
                return None
            # 今日但开盘前（9:30前）写入的缓存视为过期
            if _is_trading_day(now.strftime("%Y%m%d")) and mtime_file_date == today_t:
                if mtime_dt.hour < 9 or (mtime_dt.hour == 9 and mtime_dt.minute < 30):
                    os.remove(path)
                    return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"  [cache daily_get] 读取失败 ({key}): {e}", file=sys.stderr)
    return None

_CALENDAR_CACHE = None
_CALENDAR_MTIME = 0

def _load_trading_calendar():
    """加载 A 股交易日历（缓存7天，含节假日）"""
    global _CALENDAR_CACHE, _CALENDAR_MTIME
    now = time.time()
    if _CALENDAR_CACHE is not None and now - _CALENDAR_MTIME < 604800:
        return _CALENDAR_CACHE  # 7天内不重复拉取
    path = os.path.join(_CACHE_DIR, "trading_calendar.txt")
    dates = set()
    try:
        if os.path.exists(path) and now - os.path.getmtime(path) < 604800:
            with open(path, 'r') as f:
                dates = set(line.strip() for line in f)
        else:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                col = df.columns[0]
                for d in df[col]:
                    dates.add(str(d).replace('-', '')[:8])
                os.makedirs(_CACHE_DIR, exist_ok=True)
                with open(path, 'w') as f:
                    for d in sorted(dates):
                        f.write(d + '\n')
    except Exception as e:
        print(f"  [cache calendar] 加载失败: {e}", file=sys.stderr)
    _CALENDAR_CACHE = dates
    _CALENDAR_MTIME = now
    return dates


def _is_trading_day(d_str: str) -> bool:
    """判断 YYYYMMDD 是否为 A 股交易日"""
    return d_str in _load_trading_calendar()


def _is_market_frozen() -> bool:
    """盘后 15:00 起冻结每日缓存，节假日同样冻结"""
    now = datetime.now(_CST)
    wd = now.weekday()
    if wd >= 5 or not _is_trading_day(now.strftime("%Y%m%d")):
        return True
    minute = now.hour * 60 + now.minute
    if minute >= 900:  # 15:00 之后
        return True
    return False

def daily_set(key: str, data, force=False):
    # 盘中不写缓存(盘中数据是临时快照,写会污染盘后数据)
    if not _is_market_frozen():
        return
    # 盘后: 已有缓存文件时不覆盖,保留全天数据(除非 force=True)
    try:
        if not force:
            path = _daily_path(key)
            if os.path.exists(path):
                return
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_daily_path(key), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [cache daily_set] 写入失败 ({key}): {e}", file=sys.stderr)


def daily_set_pkl(key: str, data, force=False):
    """pickle 模式缓存(支持 DataFrame/set/dict 等任意 Python 对象)。
    用于缓存原始数据(akshare 返回的 df),而非计算结果。
    这样改评分逻辑后,可用缓存原始数据 + 新逻辑重算,不用 bump _CACHE_VER。"""
    if force:
        pass  # force=true 时跳过所有冻结检查, 允许写缓存
    elif not _is_market_frozen():
        return
    try:
        if not force:
            path = _daily_path(key).replace('.json', '.pkl')
            if os.path.exists(path):
                return
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_daily_path(key).replace('.json', '.pkl'), 'wb') as f:
            _pickle.dump(data, f)
    except Exception as e:
        print(f"  [cache daily_set_pkl] 写入失败 ({key}): {e}", file=sys.stderr)


def daily_get_pkl(key: str):
    """pickle 模式读。失败返回 None(不抛异常)。
    与 daily_get 保持一致: mtime 跨日则删, 盘前写入视为过期。"""
    try:
        path = _daily_path(key).replace('.json', '.pkl')
        if os.path.exists(path):
            now = datetime.now(_CST)
            mtime = os.path.getmtime(path)
            mtime_dt = datetime.fromtimestamp(mtime, _CST)
            today_t = _trading_date()
            mtime_file_date = mtime_dt.strftime("%Y-%m-%d")
            # 跨日: 旧交易日写入, 新交易日读取 → 删旧文件返 None
            if mtime_file_date < today_t:
                os.remove(path)
                return None
            # 今日但开盘前写入视为过期
            if _is_trading_day(now.strftime("%Y%m%d")) and mtime_file_date == today_t:
                if mtime_dt.hour < 9 or (mtime_dt.hour == 9 and mtime_dt.minute < 30):
                    os.remove(path)
                    return None
            with open(path, 'rb') as f:
                return _pickle.load(f)
    except Exception:
        return None
    return None


# ═══════════════════════════════════════════
#  统一缓存 key 构造 (P2-2 重构)
# ═══════════════════════════════════════════

def make_key(module: str, feature: str, version: int = 1, **params) -> str:
    """统一构造缓存 key, 格式: {module}_{feature}_{param_kv}_v{version}
    避免散落各文件硬编码 key 字符串
    """
    parts = [module, feature]
    # 排序 params 保证稳定性
    for k in sorted(params):
        v = params[k]
        if v is None:
            continue
        parts.append(f"{k}{v}")
    parts.append(f"v{version}")
    return "_".join(str(p) for p in parts if p)


def clear_all():
    """清除所有缓存（版本号不匹配的文件也会被清理）"""
    import glob
    try:
        kept = 0
        removed = 0
        for f in os.listdir(_CACHE_DIR):
            fp = os.path.join(_CACHE_DIR, f)
            if not os.path.isfile(fp):
                continue
            # .pkl 和 .json 都检查版本号, 旧版一律删
            if f.endswith(".pkl") or f.endswith(".json"):
                if f"_v{_CACHE_VER}" not in f:
                    os.remove(fp)
                    removed += 1
                else:
                    kept += 1
        print(f"  [缓存清理] 删除 {removed} 个旧版本, 保留 {kept} 个当前版本", file=__import__('sys').stderr)
    except Exception as e:
        print(f"  [cache clear_all] 清理失败: {e}", file=__import__('sys').stderr)
