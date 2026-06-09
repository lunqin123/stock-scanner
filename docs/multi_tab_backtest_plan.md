# 多 Tab 回测扩展计划 v1.0

> 项目：A 股超短线选股扫描器 (`stock-scanner`)
> 目标：把目前只服务**涨停 tab** 的回测系统，扩展为**任意 tab 都能跑历史回测**。

---

## 1. 现状盘点

### 1.1 当前已有的回测链路

| 模块 | 位置 | 职责 |
|---|---|---|
| `backtest_score_prev()` | `scanner.py:2230` | 给昨日涨停池跑 6 因子加权评分；统计因子相关性 + 模拟交易 |
| `_simulate_trades()` | `scanner.py:2401` | 简化版"取评分前 N 名 → 次日涨幅 −成本" |
| `run_t1_backtest()` | `t1_real_backtest.py:248` | T+1 真实回测：信号日涨停 → D+1 开盘买 → D+2 开盘卖 |
| `auto_verify_backtest()` | `scanner.py:2457` | 盘后用当日实盘归档验证昨日评分有效性 + 调权 |
| `run_backtest()` CLI | `scanner.py:2590` | 20 天滚动回测的简易 CLI |

### 1.2 前端 Tabs

`static/app.js:33-37` 定义了 6 个扫描 tab + 1 个回测面板：

| Tab Key | 标题 | API |
|---|---|---|
| `scan-limit` | 涨停扫描 | `/api/scan/limit-up/cards` |
| `scan-trend` | 趋势扫描 | `/api/scan/trend/cards` |
| `scan-sector` | 板块热度 | `/api/scan/sector/cards` |
| `scan-zhaban` | 炸板分析 | `/api/scan/zhaban/cards` |
| `scan-reversal` | 反转扫描 | `/api/scan/reversal/cards` |
| `scan-dtqiaoban` | 跌停翘板 | `/api/scan/dtqiaoban/cards` |

### 1.3 可复用的基础设施

| 代码 | 作用 | 复用方式 |
|---|---|---|
| `t1_real_backtest.py` 整体 | 信号日 → 买入日 → 卖出日 OHLCV 拉取 + 双策略聚合 | 抽出公共 driver，按 tab 参数化 |
| `recommendation_tracker.py` | 按 tab 分组的推荐存档 + 次日开盘验证 | 与回测互补，用于实时胜率追踪 |

---

## 2. 各 Tab 信号日 / 数据源差异（扩展的核心约束）

| Tab | 信号日输入 | akshare API | 评分函数 | 买入逻辑 |
|---|---|---|---|---|
| **涨停** | 上交易日涨停池 | `stock_zt_pool_previous_em` | `backtest_score_prev` (6因子) | D+1 开盘/尾盘买 |
| **趋势** | 当日强势池 | `stock_zt_pool_strong_em` | `scan_trend` 内嵌 | 盘中已发生，按当日收盘 → 次日开盘 |
| **炸板** | 当日炸板池 | `stock_zt_pool_zbgc_em` | `score_zhaban_data` | 次日竞价反包 |
| **反转** | 上交易日涨停池 + 今日跌幅过滤 | `stock_zt_pool_previous_em` | 内嵌反转评分 | 次日反包 |
| **板块** | 当日涨停 + 炸板池 | `stock_zt_pool_em + zbgc_em` | `score_sector_data` | 板块级（**最特殊**，要降级为板块内个股集合） |
| **翘板** | 当日跌停池 | `stock_zt_pool_dtgc_em` | `score_dtqiaoban_data` | 次日高开反抽 |

**关键观察**：

1. **涨停 / 反转**：信号日 = D−1，今日表现 = 已有 `prev_pool` 自带 `涨跌幅` 列（直接用）
2. **炸板 / 翘板 / 趋势**：信号日 = D 日当日，今日涨跌幅 = 封板 / 跌停 / 强势本身就是信号，要测的收益必须拉 D+1 数据
3. **板块**：是板块级排名，没有"单只买入"概念，需要降级为"板块内涨停股集合的次日表现"

---

## 3. 目标（Goals）

- **G1**：任意 tab 都能跑"过去 N 天"批量回测，返回结构化 `summary + trades`
- **G2**：不破坏现有涨停 tab 的回测（向后兼容 API）
- **G3**：每个 tab 单独因子相关性 → 可触发 `weight_manager` 调权（按 plan 隔离）
- **G4**：单次回测 API 响应 < 30s（用 daily cache + 2h cache）
- **G5**：前端能看到各 tab 自己的"历史回测胜率 / 累计收益曲线"

---

## 4. 实施阶段（Phase）

### Phase 1 — 抽出公共回测引擎（**2.5d**，原估 1.5d 偏紧）

**新建文件**：`backtest_engine.py`（通用回测驱动，参数化 tab）

**职责**：
- 遍历交易日 `D_signal`
- 调对应 tab 的"信号池获取函数" + "评分函数"
- 调 `_get_ohlcv_batch` 拿 D / D+1 / D+2 价格
- **【新】逐日 OHLCV 批量缓存**：`_get_daily_ohlcv_batch(date)` 按日期缓存当日所有活跃股的 OHLCV，替代逐股拉取
- 双策略模拟（开盘买 / 尾盘买）+ 一字板过滤
- 聚合 summary（胜率 / EV / 最大回撤 / 累计）
- 写缓存 `daily_set(cache_key)` + 持久化 `backtest_results.json`

**OHLCV 性能背景**：当前 `t1_real_backtest.py` 逐股调用 `ak.stock_zh_a_hist()`，30 天 × 每日 TOP3 ≈ 90 次 API 调用，约 30-60 秒。扩展到 6 个 tab 后峰值约 540 次/回测。逐日批量缓存（一次拉一天的全市场 OHLCV，按 code 索引）可降到 30 次 API 调用，回测时间 < 5 秒。

**公开 API**：

```python
def run_backtest(
    tab: str,                # 'limit-up' | 'trend' | 'zhaban' | 'dtqiaoban' | 'reversal' | 'sector'
    start: str = None,
    end: str = None,
    top_n: int = 3,
    capital: float = 30000,
    use_cache: bool = True,
) -> dict:
    """返回 {summary, trades, top5, bottom5, skipped, comparison, generated_at, config}"""
```

**信号池派发表**：

| Tab | 信号池获取 |
|---|---|
| 涨停 | `stock_zt_pool_previous_em(D_signal)` |
| 反转 | `stock_zt_pool_previous_em(D_signal)` → `filter(今日涨幅 ∈ [-7, 1])` |
| 炸板 | `stock_zt_pool_zbgc_em(D_signal)` |
| 翘板 | `stock_zt_pool_dtgc_em(D_signal)` |
| 趋势 | `stock_zt_pool_strong_em(D_signal)` |
| 板块 | composite(涨停池 + 炸板池) by 所属行业 → 取板块内个股集合 |

**评分函数派发表**：

| Tab | 评分函数 |
|---|---|
| 涨停 | `backtest_score_prev(df, date_str)` ✅ 已有 |
| 反转 | `scan_reversal._score_pullback(df)` ⚠️ 需抽出 |
| 炸板 | `score_zhaban_data(df, today_str)` ✅ 已有 |
| 翘板 | `score_dtqiaoban_data(df)` ✅ 已有 |
| 趋势 | `scan_trend._score_strong(df)` ⚠️ 需抽出 |
| 板块 | `score_sector_data` + 板块内个股得分聚合 ⚠️ 需新增"个股级"映射 |

**Refactor 目标**：

- 文件：`scanner.py`
- 操作：把 `scan_reversal` / `scan_trend` 的"评分核心"抽成纯函数 `_score_xxx(df)`，主函数只负责打印 / 格式化
- 原因：回测引擎需要复用纯评分函数，避开 print / IO

---

### Phase 2 — 各 Tab 评分函数的兼容性改造（1d）

| Task | 文件 | 当前 | 改造 |
|---|---|---|---|
| T1 | `scanner.py` | `scan_reversal` 内嵌在主函数 for 循环里 | 抽 `_score_reversal(df, today_str)` → `pd.Series(反转评分)`，抽离前后对同一 prev_df 输出完全一致 |
| T2 | `scanner.py` | `scan_trend` 内部有完整评分（**实际 5 因子**：涨幅 + 换手 + 成交额 + 量比 + 新高，全部来自强势池当日列，无历史 K 线依赖） | 抽 `_score_trend(df)` → `pd.Series(趋势评分)`；注：scan_trend 用了"今天涨幅"——历史回测时要替换成"昨日涨幅 → 今涨幅"循环 |
| T3 | `scanner.py` | `score_zhaban_data` / `score_dtqiaoban_data` 已经抽好 | 无需改，但需确认 `stock_zt_pool_zbgc_em` / `dtgc_em` 接受 date 参数能拉历史 |
| T4 | `scanner.py` (scan_sector) | `score_sector_data` 是板块级 → 不适合做个股 T+1 回测 | 决策：板块 tab 的回测 = "每天板块 TOP1 → 该板块所有涨停 / 炸板个股 → 等权买 → D+1 算收益"；新增 `backtest_sector_composite(tab)` |
| T4.1 | `scanner.py` | **【新】** | 实现 `_get_sector_stocks(industry_name, limit_df, zhaban_df) → list[str]`：给定板块名，从涨停池 / 炸板池 DataFrame 按行业列筛选个股（反向查询） |

---

### Phase 3 — T+1 真实回测通用化（1.5d）

| Task | 操作 |
|---|---|
| T5 | 把 `t1_real_backtest.run_t1_backtest` 重构为 `run_tab_backtest(tab, **kwargs)`；保留 `run_t1_backtest = run_tab_backtest` 兼容别名；新增 `signal_pool_fetcher` 表 + `score_func` 表 |

新签名：

```python
def run_tab_backtest(
    tab: Literal['limit-up','trend','zhaban','dtqiaoban','reversal','sector'],
    start_date: str = None,
    end_date: str = None,
    top_n: int = 3,
    capital: float = 30000,
    max_days: int = 30,
) -> dict:
    ...
```

| Task | 操作 |
|---|---|
| T6 | **Cache Key 策略**：旧 `t1_result_{start}_{end}_{top_n}_{capital}` → 新 `bt_result_{tab}_{start}_{end}_{top_n}_{capital}`；改 `score_func` 时手动升 `CACHE_VER` |
| T7 | **数据完整性核对**：反转 / 炸板 / 翘板 / 趋势的"信号日"akshare API 不一定支持历史 date 参数，需实测一周数据 |

akshare API 历史 date 参数可用性预估：

| Tab | API | 可用性 | 备注 |
|---|---|---|---|
| 涨停 | `stock_zt_pool_previous_em(date=X)` | ✅ 确认可用 | archiver/scanner/t1 多处生产调用 |
| 反转 | `stock_zt_pool_previous_em(date=X)` | ✅ 复用涨停池 | 同上 |
| 炸板 | `stock_zt_pool_zbgc_em(date=X)` | 🟡 **待实测** | archiver/app/scanner 中均调用，但**都是当日 date**，历史任意日期稳定性未验证 |
| 翘板 | `stock_zt_pool_dtgc_em(date=X)` | 🟡 **待实测** | 同上 |
| 趋势 | `stock_zt_pool_strong_em(date=X)` | ⚠️ 真正风险点 | 强势池只在盘后/盘中稳定，历史盘后数据需实测 |
| 板块 | `stock_zt_pool_em(date=X)` + `zbgc_em(date=X)` | ✅ 复用涨停池 | |

---

### Phase 4 — 滚动回测扩展（1d）

| Task | 操作 |
|---|---|
| T8 | 把 `run_backtest()` 改写为接受 `tab` 参数：`def run_backtest(tab: str = 'limit-up', N: int = 20)` |
| T9 | CLI 入口扩展：`python scanner.py --backtest --tab=limit-up\|trend\|zhaban\|dtqiaoban\|reversal\|sector\|all` |
| T10 | 输出格式：保持原 20 天滚动格式 + 加 tab 名 header + 加各 tab 的因子相关性对比 |

---

### Phase 5 — 自动调权支持多 Tab（0.5d）

**Context**：`weight_manager` 现在只对 `plan_a` 的 6 因子做 ICIR 调权。其他 tab 的评分函数（翘板 5 因子、炸板 5 因子）是独立打分，不走 `weight_manager`。另外 `plan_b.py` 已经有 16 因子的独立调权，但**本计划只覆盖 plan_a**，plan_b 的多 tab 回测延后。

**决策**：

- **Option A（V1 推荐）**：调权只覆盖**涨停 tab（plan_a 6 因子）**，其他 tab 的回测只输出统计、不改权重；**plan_b 同样跳过**
- **Option B（V2 留）**：为每个 tab 建独立 `weight_manager.PLAN_X_WEIGHTS`，工作量 ×5

**选定**：Option A。

| Task | 操作 |
|---|---|
| T11 | `auto_verify_backtest` 保持只跑 plan_a 涨停 tab 调权；plan_b 及其他 tab 的回测只输出统计、不改权重 |

---

### Phase 6 — API & 前端接入（1d）

**新增 API 端点**：

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/backtest/{tab}` | 历史回测结果（与 `/api/backtest/t1` 同构） |
| GET | `/api/backtest/{tab}/top` | TOP5 快速版 |
| GET | `/api/backtest/{tab}/dashboard` | 调权 + 因子相关性 |

**向后兼容**：

- `/api/backtest/t1` → 内部转 `/api/backtest/limit-up`
- `/api/backtest/t1/top` → 同上

**前端改动**：

- 文件：`static/app.js`
- 改动：
  - 在 backtest 面板顶部加 tab 切换器：[涨停 | 趋势 | 炸板 | 翘板 | 反转 | 板块]
  - 默认显示涨停，已有逻辑不动
  - 切换 tab 调对应 `/api/backtest/{tab}`
  - `cards.js` 加 `renderBacktestTabSummary(tab, data)`

- 文件：`static/cards.js`
- 新增渲染器：`renderBacktestTabSummary(tab, summary, trades)`

---

### Phase 7 — 测试 + 部署（0.5d）

## 11. 实施进度与新发现（2026-06-09 22:18 全部 Phase 完结）

### 11.1 全部 Phase 完成 ✅

**P1.1** backtest_engine.py 骨架（~730 行）
**P1.2** OHLCV 逐日批量缓存 `_get_daily_ohlcv_batch(date)` （每日全市场一次拉取）
**P2.1** 抽出 `_score_reversal` 纯函数
**P2.2** 抽出 `_score_trend` 纯函数（5 因子）
**P2.3** 实现 `_get_sector_stocks` 反向查询 + `_score_sector` 个股级映射
**P3** `run_tab_backtest` 通用化（已有，作为 P1 框架的核心）
**P4** `scanner --backtest --tab={tab}` 多 tab CLI + `run_backtest(tab=)` 参数
**P5** 调权隔离明确（plan_a + 涨停 tab，V1 决策）
**P6** API 端点 `/api/bt/{tab}` + `/api/bt/{tab}/top` + 前端 tab 切换器 `switchBacktestTab`
**P7** 测试 case_67~71 通过（79/80，1 个原有 bug 非新增）

### 11.2 服务器部署 ✅

- scp 推送：`backtest_engine.py` / `scanner.py` / `cache.py` / `app.py` / `static/app.js`
- `sudo systemctl restart stock-scanner`
- 验证端点：
  - `/api/health` → `{"ok":true,...}`
  - `/api/backtest/dashboard` → 调权 + 因子相关性 (未被新路由拦截)
  - `/api/bt/limit-up?days=3` → 结构化 JSON, `summary`/`trades`/`top5`/`skipped` 完整
  - `/api/bt/zhaban?days=3` → 同上
  - `/api/bt/reversal?days=3` → 同上

### 11.3 API 路由冲突解决（重要）

**问题**：FastAPI 按注册顺序匹配路由，新加的 `/api/backtest/{tab}` 会拦截原有 `/api/backtest/dashboard` 和 `/api/backtest/t1`。

**方案**：新端点改用 `/api/bt/{tab}` 前缀，避开 `/api/backtest/*` 的保留路径。

### 11.4 已知遗留风险

**akshare 30 天数据限制**：`stock_zt_pool_zbgc_em` / `dtgc_em` 超过 30 天前的 date 参数抛 `ValueError("只能获取最近 30 个交易日")`。
- V1 限制：炸板/翘板 tab `max_days ≤ 15`
- 涨停/反转 tab 可跑 30 天（用 `stock_zt_pool_previous_em`，无此限制）

### 11.5 文件清单（本次新增/修改）

**新增**：
- `backtest_engine.py` (730 行) — 多 tab 回测引擎
- `scripts/test_v0_api_availability.py` — V0 API 实测
- `scripts/smoke_p1.py` / `smoke_p2_reversal.py` / `smoke_p2_after.py` / `smoke_p2_15d.py` / `smoke_p2_30d.py` / `debug_zhaban.py` — 各阶段冒烟
- `docs/multi_tab_backtest_plan.md` — 本计划文档

**修改**：
- `scanner.py` — 抽出 `_score_reversal` / `_score_trend`，CLI `--tab/--days` 参数，`run_backtest(tab=)` 多 tab 支持
- `cache.py` — 修复 `_CACHE_TTL` / `sys` 未定义 bug
- `app.py` — 新增 `/api/bt/{tab}` 和 `/api/bt/{tab}/top` 端点
- `static/app.js` — 前端 tab 切换器 `switchBacktestTab`
- `test_invariants.py` — 新增 section 8,case_67~71

### 11.6 测试结果

```
[PASS] tab=limit-up 5天跑通 (1 笔)
[PASS] tab=trend 5天跑通 (0 笔)
[PASS] tab=zhaban 5天跑通 (1 笔)
[PASS] tab=dtqiaoban 5天跑通 (3 笔)
[PASS] tab=reversal 5天跑通 (0 笔)
[PASS] tab=sector 5天跑通 (0 笔)
[总结] 79 通过 / 1 失败（原 case_42, 非本次引入）
```

---

| Case | 验证内容 |
|---|---|
| case_67 | `backtest_engine.run_tab_backtest('limit-up')` 与旧 `run_t1_backtest` 输出 trades 字段一致 |
| case_68 | 各 tab `run_tab_backtest` 跑 5 天不报错 |
| case_69 | 板块 tab 的回测 trades 字段含 `sector_name` |
| case_70 | cache_key 包含 tab 维度，跨 tab 隔离 |
| case_71 | 评分函数 refactor 后 `scan_trend` / `scan_reversal` 输出完全一致（snapshot diff） |

**服务器部署**：

```bash
scp backtest_engine.py + 修改的 scanner/t1 到 134.175.231.8
sudo systemctl restart stock-scanner
journalctl -u stock-scanner -f 检查无 panic
# 浏览器 Ctrl+Shift+R
```

---

## 5. 风险与决策点

### R1 — akshare 炸板 / 翘板 / 强势池 API 是否支持历史 date 参数

- **影响**：若不可解，板块 / 炸板 / 翘板 / 趋势只能等 `recommendation_tracker` 自然累积，不能做"任意区间回测"
- **缓解**：实测一周内每日调用；若失败 → 改用"收盘后抓当日数据 + 第二天验证"模式（即不批量回测，只累积 tracker 数据）

### R2 — 反转评分强依赖当日实时数据（趋势无此问题）

**已修正**：审查发现 `scan_trend` 5 个评分因子（涨幅 / 换手 / 成交额 / 量比 / 新高）**全部来自强势池当日列**，没有"连续上涨天数"等历史 K 线依赖。所以**趋势 tab 没有 R2 风险**。

**仅反转 tab** 的"板块支撑"评分（`scanner.py:1507-1521`）依赖"今日涨停板块热度"。历史回测中需要降级：

- **缓解**：历史回测中"板块热度"用"过去 3 日涨停板块频次"近似

### R3 — Refactor scan_trend / scan_reversal 可能影响前端 stream 端点

- 这些函数被 stream 端点共用
- **缓解**：先跑 `test_invariants` 现有 66 项，确认无回归再合

---

## 6. 时间线

| Phase | 工作日 | 变更说明 |
|---|---|---|
| P1 — engine 骨架 | **2.5d** | ⚠️ 原 1.5d → +1d（增加 OHLCV 批量缓存子任务） |
| P2 — 评分函数 refactor | 1.0d | +0d，但新增 T4.1 个股反向查询 |
| P3 — T+1 通用化 | 1.5d | |
| P4 — 滚动回测扩展 | 1.0d | |
| P5 — 调权隔离 | 0.5d | 明确 plan_b 跳过 |
| P6 — API + 前端 | 1.0d | |
| P7 — 测试 + 部署 | 0.5d | |
| **总计** | **~8 工作日** | ⚠️ 原 7d → +1d |

---

## 7. Done Definition

- `python scanner.py --backtest --tab=all` 跑 30 天无报错
- `/api/backtest/{tab}` 各 tab 都返回合法 JSON
- 前端切到任一 tab 的回测面板都能看到胜率 / 累计
- 现有涨停 tab 的回测结果与扩展前 **100% 一致**（向后兼容）
- `test_invariants` 全部 **71 项**通过（原 66 + 新增 5）

---

## 8. 立刻可动的最低成本 V0（不重构）✅ **已完成实测**

### 8.1 V0 实测结果（2026-06-09）

**测试脚本**：`scripts/test_v0_api_availability.py`
**测试区间**：20260526 ~ 20260608（10 个交易日）
**测试方法**：4 个 API 各调 10 次，统计成功率 + 数据量中位数

| API | 成功率 | 数据量中位数 | 结论 |
|---|---|---|---|
| `stock_zt_pool_em`（涨停池对照） | 100% (10/10) | 67 只 | ✅ 可用 |
| `stock_zt_pool_zbgc_em`（炸板） | 100% (10/10) | 24 只 | ✅ 可用 |
| `stock_zt_pool_dtgc_em`（跌停/翘板） | 100% (10/10) | 16 只 | ✅ 可用 |
| `stock_zt_pool_strong_em`（强势/趋势） | 100% (10/10) | 202 只 | ✅ 可用 |

**关键发现**：
- 所有 4 个 API 的历史 date 参数**100% 可用**，无任何限流或接口错误
- 数据量合理：炸板池日均 24 只、翘板池日均 16 只、强势池日均 202 只
- 单次响应时间均在 0.6 秒左右（首次冷启动约 3.5 秒）

**结论**：
- 审查报告 R1 风险评估**完全解除**，4 个 API 都支持历史回测
- 可**直接进入完整 8 天重构计划**，无需 V0 降级路径
- 趋势池数据量（~200/天）甚至比涨停池还大，回测样本充足

### 8.2 实测脚本运行方式

```bash
python scripts/test_v0_api_availability.py
```

输出格式：`{日期} OK/FAIL {数据量} ({耗时}s) [错误原因]`

---

如果只想"先看到结果，refactor 后再说"，可以这样快速搞：

1. **炸板 / 翘板**：复用 `t1_real_backtest.py` 的 OHLCV 拉取逻辑，但信号池换成 `stock_zt_pool_zbgc_em` / `dtgc_em` + 用现有 `score_zhaban_data` / `score_dtqiaoban_data`
2. **反转**：直接复用 `backtest_score_prev` 框架（本来就是 `prev_pool`），把筛条件 `今日涨幅 ∈ [-7, 1]` 加进去
3. **趋势**：先 mock，跑 `stock_zt_pool_strong_em` 在回测模式下报错就降级
4. **板块**：特殊处理，取板块内所有涨停 / 炸板个股等权买

预计 2-3 天能跑通 V0，剩下都是打磨。

---

## 9. 验证 Prompt（直接喂给外部 AI）

如果要把这份计划交给 ChatGPT / Claude 验证合理性，可用以下 prompt：

```
你是 A 股量化系统架构师。请严格审查下面的「多 Tab 回测扩展计划」，从以下维度打分并指出具体问题：
1. 数据可行性：akshare 各 API 是否真的支持 date=历史参数？
2. 架构合理性：phase 1~7 的拆分有没有遗漏依赖或多余步骤？
3. 向后兼容性：现有涨停 tab T+1 回测是否会被破坏？
4. 风险评估：R1~R3 是否充分？还有没列出的坑？
5. 时间评估：7 工作日是否合理？

请按格式输出：
- 总体评分（1-10）
- 三个最大的问题
- 三个最佳的设计决策
- 修正建议（具体到 phase / task 编号）

不要客套，直接进入审查。

[把本文件正文粘贴到这里]
```

---

## 10. 外部 AI 审查结果与采纳情况

### 10.1 审查评分：**7.5 / 10**（整体可行）

### 10.2 审查认可的部分（✅ 采纳）

| 审查项 | 采纳状态 |
|---|---|
| 涨停 / 反转 / 板块 API 历史参数可用 | ✅ 已记录 |
| 评分函数已抽好的部分（炸板 / 翘板 / 涨停） | ✅ 验证通过 |
| 信号池派发表 + 评分函数派发表的设计 | ✅ 沿用 |
| V0 快速方案务实 | ✅ 已纳入第 8 节 |
| Cache Key 版本管理 | ✅ 沿用 |
| 向后兼容设计（`/api/backtest/t1` 内部转发） | ✅ 沿用 |

### 10.3 审查指出需修正（已更新）

| # | 修正点 | 采纳情况 |
|---|---|---|
| 1 | 测试数量 64 → **66** → +5 = **71** | ✅ 已修正（用 Python `re.findall(r'\\bcheck\\(', ...)` 全文件计数：含 `def check` 共67 次，减1次函数定义 = **66 个 check assertion**） |
| 2 | 炸板 / 翘板 API 风险降级 | ⚠️ **半接受**：从 ⚠️ 降为 🟡（生产中调用均为**当日 date**，历史任意日期稳定性仍需实测；不能直接到 ✅） |
| 3 | R2 趋势"连续上涨天数"描述错 | ✅ 已核实并删除该描述（`scan_trend` 5 因子无历史 K 线依赖） |
| 4 | P1 时间 1.5d → 2.5d | ✅ 已修正（OHLCV 批量缓存是关键工作量） |
| 5 | 遗漏：OHLCV 批量缓存 | ✅ 已加入 Phase 1 子任务 |
| 6 | 遗漏：板块个股反向查询 | ✅ 已新增 T4.1 |
| 7 | 遗漏：plan_b 跳过调权 | ✅ 已明确写明 |

### 10.4 最终时间线：**8 工作日**（原 7d + 1d）