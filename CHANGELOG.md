# Changelog

## v1.24.3 (2026-06-13)

### 修复 — 审计 2: 缓存/竞态/持久化 (4 项 BUG)
- **BUG 1 [高]**: `app.js:runCurrent` cache key 被 `_r=<random>` 污染 → 永不命中, 每次 14s 重 fetch
  - 修法: 拆分 `stableKey` (业务 cache) + `url` (含 `_r`, 绕浏览器 HTTP 缓存)
- **BUG 2 [高]**: `cache.py:daily_get_pkl` 无 mtime 过期检查 → 跨日沿用周五的旧 pkl
  - 修法: 仿 `daily_get` 加 mtime 跨日/盘前检查, 自动删旧文件
- **BUG 3 [中]**: `weight_manager.py` 5 个 save_* (`save_weights`/`save_reversal_weights`/`save_trend_weights`/`save_daily_correlations`/`save_tab_performance`) 全非原子写 → 写崩后 except 吞错返默认值, 调权静默丢失
  - 修法: 新增 `_atomic_write_json` helper (tmp + os.replace)
- **BUG 4 [低]**: `cache.py:clear_all` 跳过 .json 文件, 旧版 daily_*.json 残留
  - 修法: .pkl 和 .json 一起检查版本号
- **测试**: `test_invariants.py` 新增 section 9 持久化与缓存安全 (9 项回归测试), 总 80 → 89 全过
- **报告**: `docs/audit2-cache-concurrency.md`

## v1.24.2 (2026-06-11)

### 调度 — 盘后自动调权 (解决 v1.23 之前的死代码)
- **新增 `weight_scheduler.py`**: 盘后调权统一入口
  - **互斥锁**: `threading.Lock` + `data/weight_adjust.lock` 文件 (跨进程防并发)
  - **盘中不调**: `get_market_status() in ('trading', 'lunch')` 立即跳过, 避免冲撞用户实时拉数据
  - **僵死锁恢复**: 状态文件 > 10 分钟没动 → 视为上次崩了, 允许新调权启动
  - **fire-and-forget**: 调权期间用户请求直接用旧值 (不阻塞用户)
- **接入点**: `app.py:_run_close_scan()` 完成后 (15:05+60s) 触发
  - 阶段1: plan_a — 跑 5 天回测 → score × net_ret_pct 相关性 → 调权重
  - 阶段2: trend — 从 archive.db daily_stocks 读 next_day 数据 → 调权重
  - 阶段3: reversal/tab — 暂搁, archive.db 缺 rev_xxx 因子分列
- **新 API**:
  - `GET /api/weights/status` — 上次调权时间/状态/tabs
  - `POST /api/weights/run?force=bool` — 手动触发 (CLI/调试)
- **CLI**: `python weight_scheduler.py --status` / `--force`

### 修复 — archiver 漏补 trend 的 next_day
- `_update_next_day_data` 之前只用 `stock_zt_pool_previous_em` (limit_up only)
- **新增方法2**: 用 `stock_zh_a_spot_em` 全市场行情查 trend 等其他类型代码的次日涨跌幅
- 效果: 15:05 收盘后, trend 类型的 next_day_change 自动填上 (历史 248 条全 None → 明日自动开始累积)

### 数据缺口 (已知)
- `backtest_engine.run_tab_backtest` 返回的 trades **不包含每笔的因子分** (seal_score/tech_score/...)
  → plan_a 调权只能用 `score 总体 × net_ret_pct` 相关性, 4 个因子共用同一相关系数 (粗调, 后续升级需 backtest_engine 补 trades 因子分列)

### 文档
- CLAUDE.md 加"盘后调度"说明
- 注释: `auto_verify_backtest` 在 weight_manager.py 中实际是 `daily_adjust_weights` + `save_daily_correlations` 的合称 (v1.23 之前叫法, 现统一为 `weight_scheduler.run_after_hours_weight_adjust`)

---

## v1.24.1 (2026-06-11)

### 配置
- **新增 `INCLUDE_CHINEXT` 开关** (`config.py`): 默认 `False` (历史行为, 排除 ST/科创/北交/创业板)
  - 改 `True` 保留创业板 (30/301) + 科创板 (688) — 让用户 A/B 对比 20% 涨停
  - **不**影响北交所 (8/9/92/94 仍排除) 和 ST (永排除)
  - CLI: `INCLUDE_CHINEXT=True python scanner.py`

### 评分 (plan_a 100 分制)
- **加权和 71 → 100** (`weight_manager.py:14-32`): 7 因子按比例 ×1.408 拉满, 因子相对关系不变
  - seal 22→31, money 12→17, sector 12→17, tech 6→8, history 4→6, stock_sentiment 9→13, principal_score 6→8
  - 改前评分区间 [0, 71] → 改后 [0, 100] (CHANGELOG 旧"100 分制"承诺兑现)
  - 缓存版本: `_CACHE_VER` 7→8, `_RAW_CACHE_VERSION` 5→6 (旧缓存自动失效)

### 测试 (fail-fast 改造)
- `test_invariants.py` 移除 try/except 兜底 PASS 模式:
  - `scan_trend` 异常 → FAIL (修复历史: NameError 被吞掉的 BUG)
  - `run_tab_backtest` 各 tab 异常 → FAIL (不再静默)
  - 未知 tab 异常 → FAIL
- 历史: v1.23 之前所有"降级处理: ..."的 PASS 都曾是**真 BUG 掩护** (例如 scan_trend new_high_col 缺失)

### 待办
- 用户未决策: 创业板/科创板放开是否值得? (回测跑一段时间看胜率)
- 用户未决策: `sentiment` 是否要变成"加权和"而非"系数调节"? (现仍为系数)

---

## v1.24.0 (2026-06-11)

### 重构 — 回测面板 UI (P4 死代码清理)
- 删除 `renderT1BacktestPanel` (cards.js, 163 行) — 已被 `renderBacktestTabFull` 完全替代
- 删除 `_fetchBacktest` / `refreshT1Backtest` (app.js) — 统一用 `loadBacktestTab`
- 删除 `/api/backtest/t1` / `/api/backtest/t1/top` (app.py) — 改用 `/api/bt/{tab}/full?tab=limit-up`
- 保留 `_run_t1_backtest_cached` — 仍被 daemon 线程调用 (L2245)

### 修复 — 数据准确性 + BUG
- **scan_trend NameError 修复**: `scan_trend()` 缺 `new_high_col` 变量定义 → 整函数直接挂掉,被 test 套兜底
  (test 显示"降级 PASS"实际是 try/except 吞掉 NameError,真用户用趋势功能会看到空结果)
- **fund_flow docstring 修正**: `stock_fund_flow_individual` 列序从"涨跌幅/换手率"错位改为正确的"最新价/涨跌幅"
  注: `iloc[:, 3]` 实际取的是"最新价" (列名错但代码对),`iloc[:, 8]` 取的是"净额" (列名错但代码对)
- **fund_flow 换手率/涨跌幅带 '%' 后缀**: 旧 docstring 没标注, 实际是字符串 "8.37%" 格式,
  `parse_amount` 不处理"%"但只用在净额字段所以 OK

### 改进 — 调权历史持久化
- `_WEIGHT_HISTORY_FILE` 从 `%TEMP%/stock_scanner_cache/weight_history.jsonl` 迁到 `data/weight_history.jsonl`
  (Windows 重启会清 %TEMP%,scp 部署会丢,迁到 data/ 才持久化)

### 改进 — sector tab 走 _tab_position_summary
- v2 计划漏掉: `sector` tab 调 `get_tab_weight_summary('sector')` 报"未知 tab"
- 新增 `_tab_position_summary()`: 把 `compute_tab_weights` 输出的 0.5-1.2 仓位系数展平成因子列表
  sector 是板块联动,无"因子权重"概念,前端文案"tab 仓位权重,非因子权重"明示

### 文档
- 80 项测试,CHANGELOG 旧版"64 项"未更新

### 待办 — 文档/代码脱节 (未改,需用户决策)
- `weight_manager.py:14-30` 实际加权和 = 96 分 (sentiment/sector_res/sector_mom/buyability 是 0 或系数调节)
- CHANGELOG v1.21 写"100 分制"
- 修法: ① 改注释/CHANGELOG 标注"加权和 96, 含 sentiment 系数调节 25" (最小,推荐) ② 重整 DEFAULT_WEIGHTS 到 100 (会触发缓存全失效)
- 暂不修,等用户决定

---

## v1.23.0 (2026-06-07)

### 修复 — 回测系统 12 项 BUG 修复 (P0-P2)

**P0 — 影响数据正确性:**
- T+1 缓存键参数化：不同 `days`/`top_n`/`capital` 不再命中同一缓存
- Seal 黄金奖励双计修复：`backtest_score_prev` 仅在 fallback 路径加金,`has_seal_data` 路径跳过

**P1 — 影响特定场景:**
- `auto_verify_backtest` 周末检查改用 `today_str` 而非 `date.today()`(修复历史回测日期错位)
- `daily_adjust_weights` 无变化时也返回 `new_weights`(消除内存-磁盘不一致)
- LHB 龙虎榜 API 参数 `date_specified`→`date` + 三层 fallback

**P2 — 健壮性/死代码清理:**
- 硬编码 `df.iloc[:, 3]` 改为列名匹配 `涨跌幅`
- `backtest_score_prev` 删除 `today_df` 死参数及无效网络请求
- 删除 `adjust_weights()` 死代码 (67 行,全项目 0 caller)
- 删除 `_T1_BACKTEST_TTL` 未使用常量
- `cache.make_key` 删除永不可达的 `return None`
- `get_rolling_progress` 过滤窗口与显示口径统一为 `ROLLING_WINDOW`
- `seal_time_score` 与 `_vectorized_seal_time_score` 统一阶梯逻辑

### 修正
- `weight_manager.py` docstring 准确描述三种返回路径 + 滚动窗口显式排序

### 技术债务
- 舆情模块不可用 (`No module named 'stock_community'`)
- 服务器 git pull 被墙,暂靠 scp 推送文件
- 测试覆盖集中在范围检查,缺少回测逻辑/缓存键等功能测试

---

## v1.22.0 (2026-06-06)

### 新增 — 趋势扫描数据驱动重构
- 回测发现低涨幅(2-4%)+中换手(5-15%)是唯一正收益组合
- 炸板分析评分优化: 封板资金降权(回测负相关), sealTime+换手为主

### 新增 — T+1 真实回测面板
- `t1_real_backtest.py`: D 日收盘选股 → D+1 开盘买 → D+2 开盘卖
- 参数化缓存 + Web 端点 `/api/backtest/t1`
- `backtest_score_prev` 65x 提速 (1700ms → 26ms)

### 重构
- `config.py` 集中魔数 + `ak_utils.safe_ak_call()` 统一异常处理
- `_cached_pool_loader` / `_fetch_three_pools` 减少 5 个 cards 端点重复
- logging 框架 + 统一缓存 key 命名 + 单元测试

### 修复
- 炸板端点 `/api/scan/zhaban/cards` 用错数据池 + items 字段不匹配
- fund_flow 缓存永远 miss 的 bug,二次响应从 7s 降到 300ms
- `daily_get_pkl` NameError
- T+1 回测排序不稳定导致同分选股变化
- 缓存版本号同步更新 (`_CACHE_VER` 7→8, `_RAW_CACHE_VERSION` 4→5)

---

## v1.21.0 (2026-06-05)

### 新增 — 反转扫描

- 涨停回调反转扫描：找"昨日涨停今日下跌"的股票，评估明日反包潜力
- 数据驱动评分：基于14个交易日、410只回调股回测验证
- A档反转率13.8%，E档5.1%，A/E=2.7倍
- 评分因子：换手率(0-40) + 连板位置(0-35) + 回调深度(0-15) + 板块支撑(0-10)
- CLI: `python scanner.py --reversal`
- Web: 左侧导航 → 🔄 反转扫描
- 独立卡片渲染，红涨绿跌灰平配色

### 新增 — 每日数据归档系统

- SQLite数据库 `archive.db`，每日盘后自动存储
- 三张表：daily_stocks（涨停+趋势+次日验证）、market_daily（市场快照）、archive_log
- 两阶段工作流：Day T收盘拉取 → Day T+1收盘补全次日数据
- CLI: `python archiver.py --status` 查看统计
- 集成到 `app.py` 盘后调度

### 重构 — 评分系统数据驱动优化

- **seal提权** 16→22：回测r=+0.126，唯一显著预测因子
- **sector合并** sector_res+sector_mom→单个sector (8+8→12)：消除重复计算
- **tech简化** 10→6：原换手率×连板矩阵 R²=0.001，改为博弈区间评级
- **principal增强** 2→6：5档分级+流动性底线惩罚
- **seal黄金奖励**：20+分段额外+3分（临界效应，均涨+5.9%/胜率89%）
- **apply_weights新签名**：8因子→7因子，sector_res/sector_mom→sector
- 效果：Top5%均涨+4.4%→+5.2%，胜率75%→81%，相关性+0.140→+0.150

### 修复

- 趋势扫描刷新vs运行排行榜不一致（三层根因：代码重复+_Cap污染stderr+旧进程残留）
- `fmtPct()` 工具函数：彻底杜绝 `+-0.9%` 显示bug
- 回测seal代理未触发（`has_seal_data`检查替代`max<0.01`判断）
- 回测tech负相关优化（回测中专有tech=0权重）
- 回测报告更新：100分制、S/A/B/C等级、新因子名
- 前端 localStorage 缓存清空不同步问题
- 版本号统一管理

### 技术债务

- 回测仅能验证约30%评分逻辑（5/7因子为常数/默认值）
- 趋势扫描回测需等待archive.db积累30+天数据
- akshare API数据保留期限约1个月，早于05/18的请求返回空
