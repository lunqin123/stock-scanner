#!/usr/bin/env python3
"""拆分 backtest/backtest_engine.py 为职责清晰的子模块 (2026-08-01)。

产出 (同目录):
  backtest_tabs.py     tab 常量/买入策略/自取池标记
  backtest_metrics.py  聚合统计/确定性成交/因子IC
  backtest_pools.py    信号池拉取 + 本地归档 fallback
  backtest_ohlcv.py    OHLCV 批量/archive 构造兜底
  backtest_scores.py   6 tab 评分包装 + SCORE_FUNCS/SCORE_COLUMNS
  backtest_engine.py   主循环 + 公共入口 (facade, 保留全部符号)

安全: 纯机械切分, 函数体/常量逐字保留; 拆分后需跑 test_invariants 验证。
用法: python scripts/split_backtest_engine.py
"""
import ast
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_SRC = os.path.join(_ROOT, 'backtest', 'backtest_engine.py')
_OUT = os.path.join(_ROOT, 'backtest')

with open(_SRC, encoding='utf-8') as f:
    source = f.read()
lines = source.splitlines(keepends=True)
tree = ast.parse(source)


def _node_span(node):
    return node.lineno - 1, node.end_lineno  # 0-based 半开区间


nodes = {}  # name -> (start, end)
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        nodes[node.name] = _node_span(node)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                nodes[t.id] = _node_span(node)


def _extract(names):
    """按源码顺序提取指定节点的原始文本。"""
    picked = []
    for name in names:
        if name not in nodes:
            print(f'  [warn] 未找到节点: {name}')
            continue
        s, e = nodes[name]
        picked.append((s, e, name))
    picked.sort()
    out = []
    for s, e, _ in picked:
        out.append(''.join(lines[s:e]))
        if not out[-1].endswith('\n'):
            out[-1] += '\n'
    return ''.join(out)


def _write(path, header, body):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(header + '\n' + body)
    print(f'  写入 {os.path.basename(path)} ({len(body.splitlines())} 行)')


# ── 1. backtest_tabs.py (常量, 手写) ──
tabs_header = '''"""Tab 常量与默认策略 (2026-08-01 自 backtest_engine.py 拆分)。

集中管理 6 个回测 tab 的: 名称常量 / 中文名 / 默认买入策略 / 自取池标记 /
信号池类型映射。评分与主循环分别从 backtest_scores / backtest_engine 引用。
"""

TAB_LIMIT_UP = 'limit-up'
TAB_TREND = 'trend'
TAB_ZHABAN = 'zhaban'
TAB_DTQIAOBAN = 'dtqiaoban'
TAB_REVERSAL = 'reversal'
TAB_SECTOR = 'sector'

ALL_TABS = [TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR]

TAB_NAMES_CN = {
    TAB_LIMIT_UP: '涨停扫描',
    TAB_TREND: '趋势扫描',
    TAB_ZHABAN: '炸板反包',
    TAB_DTQIAOBAN: '跌停翘板',
    TAB_REVERSAL: '涨停回调',
    TAB_SECTOR: '板块联动',
}

# 各 tab 默认买入策略 (close=T日尾盘买 T+1开盘卖; open=T+1开盘买 T+N开盘卖)
_TAB_BUY_TIME = {
    TAB_LIMIT_UP: 'close',
    TAB_DTQIAOBAN: 'open',
    TAB_TREND: 'open',
    TAB_ZHABAN: 'open',
    TAB_REVERSAL: 'open',
    TAB_SECTOR: 'open',
}

# 全部 tab 已实现
_PENDING_TABS = set()

# 这些 tab 的 score_fn 自行拉数据 (不依赖 fetcher 返回的 pool)
_SELF_FETCHING_TABS = {TAB_SECTOR}

# 每个 tab 的 pool_type 对应 archive_pools/ 中的 pickle 文件名前缀
_TAB_POOL_TYPE = {
    TAB_LIMIT_UP: 'limit_up',
    TAB_REVERSAL: 'prev_pool',
    TAB_TREND: 'strong',
    TAB_ZHABAN: 'zhaban',
    TAB_DTQIAOBAN: 'dtqiaoban',
    TAB_SECTOR: 'limit_up',
}
'''
_write(os.path.join(_OUT, 'backtest_tabs.py'), tabs_header, '')


# ── 2. backtest_metrics.py ──
metrics_header = '''"""回测指标/统计工具 (2026-08-01 自 backtest_engine.py 拆分)。

包含: 确定性成交模拟 / 聚合统计(复利累计+资金曲线回撤+EV 修复) / 因子 IC 分析。
"""
import numpy as np
import pandas as pd
'''
_write(os.path.join(_OUT, 'backtest_metrics.py'), metrics_header,
       _extract(['_deterministic_fill', '_aggregate', '_compute_factor_ics']))


# ── 3. backtest_pools.py ──
pools_header = '''"""信号池拉取 + 本地归档 fallback (2026-08-01 自 backtest_engine.py 拆分)。

每个 tab 一个 fetcher: akshare 拉取 → 失败时读本地 pickle 归档 →
命中结果写入持久缓存 (历史池数据不变)。SIGNAL_POOL_FETCHERS 供主循环派发。
"""
import os
import pandas as pd
import akshare as ak

from cache import _persistent_get, _persistent_put, _cache_get
from backtest_tabs import (
    TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR,
)
from scanner import filter_non_main_board, filter_xr_xd_dr

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

try:
    from archiver import _load_pool_pickle
except ImportError:
    _load_pool_pickle = None  # archiver 未安装时降级

_LOCAL_FALLBACK_ENABLED = True
'''
_write(os.path.join(_OUT, 'backtest_pools.py'), pools_header,
       _extract([
           '_TAB_POOL_TYPE', '_detect_available_days', '_try_local_fallback',
           '_is_placeholder_data', '_fetch_limit_up_pool', '_fetch_reversal_pool',
           '_fetch_zhaban_pool', '_fetch_dtqiaoban_pool', '_fetch_trend_pool',
           '_fetch_sector_pool', '_cached_pool_get', '_pool_cache_put',
           'SIGNAL_POOL_FETCHERS',
       ]))


# ── 4. backtest_ohlcv.py ──
ohlcv_header = '''"""OHLCV 获取 (2026-08-01 自 backtest_engine.py 拆分)。

职责: 按日批量/逐股获取真实历史 OHLCV; archive.db 构造兜底 (标记 _fallback,
严格模式由主循环跳过); spot 快照只允许当日使用 (历史日期一律走逐股历史 API)。
"""
import os
import pandas as pd
import akshare as ak
from datetime import datetime, timedelta

from cache import _cache_get, _cache_put

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
'''
_write(os.path.join(_OUT, 'backtest_ohlcv.py'), ohlcv_header,
       _extract([
           '_SPOT_DISABLED', '_ARCHIVE_DB_PATH', '_ARCHIVE_OHLCV_CACHE',
           '_try_archive_db_ohlcv', '_try_stock_daily_ohlcv',
           '_get_daily_ohlcv_batch',
       ]))


# ── 5. backtest_scores.py ──
scores_header = '''"""6 tab 回测评分包装 (2026-08-01 自 backtest_engine.py 拆分)。

每个 _score_xxx(df, date_str, capital) 输出带排名评分列的 DataFrame;
SCORE_FUNCS / SCORE_COLUMNS 供主循环派发。权重统一走 weight_manager.load_tab_weights
(score_new 权重经 scoring.score_new.load_factor_weights)。
"""
import sys
import pandas as pd

from scanner import (
    filter_non_main_board, filter_xr_xd_dr,
    score_zhaban_data, score_dtqiaoban_data,
    _score_reversal as scanner_score_reversal,
    _score_trend as scanner_score_trend,
    _score_sector as scanner_score_sector,
)
from config import MAX_PRICE, MAX_MARKET_CAP
from t1_real_backtest import CAPITAL_DEFAULT, TOP_N_DEFAULT
'''
_write(os.path.join(_OUT, 'backtest_scores.py'), scores_header,
       _extract([
           '_BACKTEST_USE_SCORE_NEW', '_BACKTEST_USE_V2_DEFAULT', '_apply_v2_to_score',
           '_score_limit_up', '_score_zhaban', '_score_dtqiaoban', '_score_reversal',
           '_score_trend', '_score_sector', 'SCORE_FUNCS', 'SCORE_COLUMNS',
       ]))


# ── 6. backtest_engine.py (保留主循环 + 公共入口 + re-export) ──
removed = set()
for names in (
    ['_deterministic_fill', '_aggregate', '_compute_factor_ics'],
    ['_TAB_POOL_TYPE', '_detect_available_days', '_try_local_fallback',
     '_is_placeholder_data', '_fetch_limit_up_pool', '_fetch_reversal_pool',
     '_fetch_zhaban_pool', '_fetch_dtqiaoban_pool', '_fetch_trend_pool',
     '_fetch_sector_pool', '_cached_pool_get', '_pool_cache_put',
     'SIGNAL_POOL_FETCHERS'],
    ['_SPOT_DISABLED', '_ARCHIVE_DB_PATH', '_ARCHIVE_OHLCV_CACHE',
     '_try_archive_db_ohlcv', '_try_stock_daily_ohlcv', '_get_daily_ohlcv_batch'],
    ['_BACKTEST_USE_SCORE_NEW', '_BACKTEST_USE_V2_DEFAULT', '_apply_v2_to_score',
     '_score_limit_up', '_score_zhaban', '_score_dtqiaoban', '_score_reversal',
     '_score_trend', '_score_sector', 'SCORE_FUNCS', 'SCORE_COLUMNS'],
):
    for n in names:
        if n in nodes:
            s, e = nodes[n]
            removed.update(range(s, e))

keep = [line for i, line in enumerate(lines) if i not in removed]

re_export = '''# ── 子模块 (2026-08-01 拆分: 常量/指标/信号池/OHLCV/评分) ──
# 本文件保留全部历史符号 (公共 API 不变), 实现见各子模块。
from backtest_tabs import (
    TAB_LIMIT_UP, TAB_TREND, TAB_ZHABAN, TAB_DTQIAOBAN, TAB_REVERSAL, TAB_SECTOR,
    ALL_TABS, TAB_NAMES_CN, _TAB_BUY_TIME, _PENDING_TABS, _SELF_FETCHING_TABS,
)
from backtest_metrics import _deterministic_fill, _aggregate, _compute_factor_ics
from backtest_pools import (
    SIGNAL_POOL_FETCHERS, _detect_available_days, _try_local_fallback,
    _is_placeholder_data, _cached_pool_get, _pool_cache_put,
    _fetch_limit_up_pool, _fetch_reversal_pool, _fetch_zhaban_pool,
    _fetch_dtqiaoban_pool, _fetch_trend_pool, _fetch_sector_pool,
    _TAB_POOL_TYPE, _LOCAL_FALLBACK_ENABLED,
)
from backtest_ohlcv import (
    _get_daily_ohlcv_batch, _try_archive_db_ohlcv, _try_stock_daily_ohlcv,
    _ARCHIVE_DB_PATH, _ARCHIVE_OHLCV_CACHE, _SPOT_DISABLED,
)
from backtest_scores import (
    SCORE_FUNCS, SCORE_COLUMNS, _apply_v2_to_score, _BACKTEST_USE_SCORE_NEW,
    _BACKTEST_USE_V2_DEFAULT, _score_limit_up, _score_zhaban, _score_dtqiaoban,
    _score_reversal, _score_trend, _score_sector,
)


'''

# 插入 re-export: 放在 data_manager import 之后
anchor = 'from data_manager import save_backtest_result as _save_backtest_result\n'
new_source = ''.join(keep)
assert anchor in new_source, '找不到插入锚点'
new_source = new_source.replace(anchor, anchor + re_export, 1)
with open(_SRC, 'w', encoding='utf-8') as f:
    f.write(new_source)
print(f'  重写 {os.path.basename(_SRC)} ({len(new_source.splitlines())} 行)')
print('拆分完成, 请运行 test_invariants 验证。')
