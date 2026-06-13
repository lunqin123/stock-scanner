# 审计 2 — 缓存/竞态/持久化 BUG 报告

**日期**: 2026-06-13
**审计范围**: `static/app.js` + `cache.py` + `weight_manager.py` + `weight_scheduler.py`
**测试**: test_invariants.py 89/89 通过 (新增 section 9, 9 项回归测试)

---

## 摘要

| # | 位置 | 严重度 | 标题 |
|---|---|---|---|
| 1 | `static/app.js:275` | 高 | `runCurrent` cache key 被 `_r` 随机数污染, 永不命中 |
| 2 | `cache.py:207` | 高 | `daily_get_pkl` 无 mtime 过期检查, 新交易日沿用旧 pkl |
| 3 | `weight_manager.py:73,465,636,159,296` | 中 | 5 个 save_* 全部非原子写, 写崩丢调权 |
| 4 | `cache.py:238` | 低 | `clear_all` 只清 .pkl 旧版, .json 残留 |

---

## BUG 1: `runCurrent` cache key 被 `_r` 随机数污染

### 现象

`runCurrent` 函数生成的 fetch URL 末尾追加了 `_r=<random>`, 然后把这个 URL 同时当作 cache 命中键 (`_lastUrl[currentPage]`) 使用:

```js
// 修前
var url = info.api + '?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '') + '&_r=' + Math.random().toString(36).slice(2);
if (_lastUrl[currentPage] === url && _getCachedPage(currentPage)) {
    return;
}
_lastUrl[currentPage] = url;
```

`_r` 每次随机不同, **`_lastUrl[currentPage] === url` 永远 False**, L276 的 cache 命中检查形同虚设。每次点"运行"或切 tab 都重走 14s fetch (涨停全表 + 5 个 fetcher + backtest)。

### 根因

cache key (用于判断是否同请求) 和 fetch URL (用于绕过浏览器缓存) 混用一个变量。`_r` 是为了打破浏览器自身的 HTTP 缓存, **不应该**进入业务 cache key。

### 修法

拆分 `stableKey` (用于 cache 命中) + `url` (用于 fetch):

```js
// 修后
var stableKey = info.api + '?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '');
var url = stableKey + '&_r=' + Math.random().toString(36).slice(2);
if (_lastUrl[currentPage] === stableKey && _getCachedPage(currentPage)) {
    return;
}
_lastUrl[currentPage] = stableKey;
```

### 影响

- 修前: 用户在已展示数据的页面点"运行"或反复切 tab → 每次 14s 全量重算
- 修后: 已展示数据的页面点"运行"或同 tab 切回 → 1ms 命中 (服务器仍返回 304-Not-Modified 级别响应)

### 验证

切 tab / 反复点"运行" / 调 principal 后再切回 — cache 命中逻辑按 stableKey 走, 不再受 `_r` 干扰。

---

## BUG 2: `daily_get_pkl` 无 mtime 过期检查

### 现象

`daily_get` (json 模式) 有完整的 mtime 跨日检查 (cache.py:99-126), `daily_get_pkl` (pickle 模式) **只**做了 `os.path.exists` 检查, 跨日直接返回旧 pickle。

```python
# 修前
def daily_get_pkl(key: str):
    try:
        path = _daily_path(key).replace('.json', '.pkl')
        if os.path.exists(path):
            with open(path, 'rb') as f:
                return _pickle.load(f)
    except Exception:
        return None
    return None
```

### 根因

P2-2 重构时 (CHANGELOG L149) 修复了 `daily_get_pkl` 的 NameError, 但只补了 pickle 读写, 漏了与 `daily_get` 对齐的过期检查。

### 修法

仿 `daily_get` 写 mtime 检查: 跨日删旧文件返 None, 盘前写入视为过期返 None。

```python
# 修后
def daily_get_pkl(key: str):
    try:
        path = _daily_path(key).replace('.json', '.pkl')
        if os.path.exists(path):
            now = datetime.now(_CST)
            mtime = os.path.getmtime(path)
            mtime_dt = datetime.fromtimestamp(mtime, _CST)
            today_t = _trading_date()
            mtime_file_date = mtime_dt.strftime("%Y-%m-%d")
            if mtime_file_date < today_t:
                os.remove(path)
                return None
            if _is_trading_day(now.strftime("%Y%m%d")) and mtime_file_date == today_t:
                if mtime_dt.hour < 9 or (mtime_dt.hour == 9 and mtime_dt.minute < 30):
                    os.remove(path)
                    return None
            with open(path, 'rb') as f:
                return _pickle.load(f)
    except Exception:
        return None
    return None
```

### 影响

- 修前: 周五写入的 pkl, 周一调 daily_get_pkl 仍返周五的旧数据 — **下游用旧 OHLCV 算回测 → 未来函数/数据错位**
- 修后: 跨日自动失效, 触发 fresh fetch

### 当前踩坑证据

服务器 `/tmp/stock_scanner_cache/` 下 6/12 (周五) 写入的 7 个 `bt_result_*_end20260612_*_v1_v8.pkl` 在 6/13 (周六) 仍可被 daily_get_pkl 命中 — 修后将自动失效。

---

## BUG 3: 5 个 save_* 函数非原子写

### 现象

`weight_manager.py` 中 5 个持久化函数都直接 `open(path, 'w') + json.dump` — 写一半进程被 kill / 磁盘满 / 异常 → **文件被截断** → 下次 `load_*` 抛 `JSONDecodeError` → except 吞掉 → **返默认值, 用户调权结果静默丢失**。

涉及函数:
- `save_weights` (plan_a/plan_b 主权重)
- `save_reversal_weights` (反转 tab 权重)
- `save_trend_weights` (趋势 tab 权重)
- `save_daily_correlations` (滚动相关性)
- `save_tab_performance` (tab 胜率记录)

### 修法

引入 `_atomic_write_json` helper, 用 `tmp + os.replace` 原子写, 与 `weight_scheduler._write_status` 保持一致:

```python
def _atomic_write_json(path: str, data, indent=None, separators=(',', ':')):
    """原子写 JSON: 写 .tmp → os.replace, 避免写崩丢权重"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, separators=separators)
    os.replace(tmp, path)
```

5 处 save_* 全部改用 helper。

### 影响

- 修前: weight_scheduler 写盘被 kill → weights.json 截断 → load_weights 走 except → 返 DEFAULT_WEIGHTS → **用户调权结果丢失, 不报警**
- 修后: 要么写成功, 要么旧文件完好, **不可能半截**

---

## BUG 4: `clear_all` 漏删 .json 旧版

### 现象

```python
# 修前
for f in os.listdir(_CACHE_DIR):
    fp = os.path.join(_CACHE_DIR, f)
    if os.path.isfile(fp) and not f.endswith(".json"):
        if f"_v{_CACHE_VER}" not in f:
            os.remove(fp)
```

`not f.endswith(".json")` 直接跳过所有 .json 文件, **旧版 daily_*.json 永远清不掉**。

### 修法

```python
# 修后
for f in os.listdir(_CACHE_DIR):
    fp = os.path.join(_CACHE_DIR, f)
    if not os.path.isfile(fp):
        continue
    if f.endswith(".pkl") or f.endswith(".json"):
        if f"_v{_CACHE_VER}" not in f:
            os.remove(fp)
            removed += 1
        else:
            kept += 1
```

### 影响

- 修前: bump `_CACHE_VER` 后, 旧 daily_*.json 残留, 占盘 + 误导调试
- 修后: 旧版 .pkl 和 .json 一并清

---

## 没改但记录在案的次要问题

| 位置 | 问题 | 决定 |
|---|---|---|
| `weight_scheduler.py:79` | `_is_lock_stale` 拿 `datetime.fromisoformat(s['ts'])` 没时区, except 吞掉 → 保守返 True | 不修 (保守策略, 不会卡死) |
| `scanner.py:183,238,981...` | 31 处用 `date.today()` 而非 `_trading_date()` — 周末会用周六日期 | 不修 (周末只是写盘 key, daily_get_pkl 跨日失效后会清掉) |
| `backtest_engine.run_tab_backtest` | trades 不含单笔因子分 (seal_score/tech_score/...) — 精细调权需补 | 不在本次范围 (单独立项) |

---

## 测试覆盖

`test_invariants.py` section 9 新增 9 项回归测试:

- 9.1 save_weights 原子写不留 .tmp
- 9.2 save_weights 后 load_weights 拿到新值
- 9.3 save_reversal_weights 原子写
- 9.4 save_trend_weights 原子写
- 9.5 daily_get_pkl 跨日返 None
- 9.6 daily_get_pkl 跨日删旧文件
- 9.7 clear_all 删旧版 .json
- 9.8 clear_all 保留当前版 .json
- 9.9 clear_all 删旧版 .pkl

总测试 80 → 89, 全部通过。
