# Changelog

## v3.6.9 (2026-08-02)

### 修复: 回测/涨停加载进度条"固定30%后突然完成"

**背景**: 回测页与扫描 tab 切换走卡片 JSON 接口, 前端固定显示 30%,
等待期间进度条不动, 完成后数据一次性出现, 没有循序渐进的真实进度感。

**改动**:
- `backtest/backtest_engine.py: run_tab_backtest` 增加 `progress_cb(done,total)`,
  主循环每个交易日开始时回调一次
- `app.py` 新增 `/api/bt/{tab}/full/stream` (SSE): 未命中缓存时按
  "回测中 N/M 天" 推送真实 pct (14%→95%), 完成发完整面板数据; 缓存命中秒发 complete;
  `/full` JSON 端点重构为共用 `_correct_bt_defaults`/`_bt_full_payload` 后保留兜底
- `static/app.js`:
  - `loadBacktestTab` 改走 SSE, 进度条与内容区同步显示 "回测中 N/M 天 (NN%)",
    旧服务端无 stream 端点时自动回退 JSON; 超时放宽到 120s
  - `callApi` 把涨停 tab 的 cards 请求改为流式端点, 切 tab/刷新显示真实进度
- `templates/index.html` 资源版本升至 `20260802v5`

**验证**: 打桩确认 20 天回测推送 20 次 progress (14→95 递增)、缓存命中只发
complete; `python -m pytest` 240 passed。

## v3.6.8 (2026-08-02)

### 优化: 回测页打开时后台预取其余 tab + 手动部署修复线上版本

**背景**: 生产服务器 webhook 自动部署未生效, 仍跑 v3.3h 时代代码
(前端 `force=true` 仍在, 每次点回测都强制全量重算, 日志确认 15:54 的请求全部带
`force=true`)。手动同步最新代码后, 同一请求实测 9.0s → 0.007s(缓存命中)。

**改动**:
- `static/app.js` 回测面板打开后 500ms 静默预取其余 4 个 tab
  (参数与当前一致), 切换 tab 直接命中后端缓存秒开
- 手动部署: git 文件列表打包(排除 data/daily_data/密钥)同步到
  `134.175.231.8:/home/ubuntu/stock-scanner`, 备份
  `stock-scanner-backup-20260802.tgz`, 重启服务验证
- `.gitignore` 排除 `deploy.tgz`/`deploy_files.txt`(部署打包临时文件)

**验证**: 生产实测 `/api/bt/trend/full` 第一次 9.0s, 第二次 0.007s 且结果一致;
`force=true` 已从线上前端消失。

## v3.6.7 (2026-08-02)

### 优化: 今日市场数据逐块显示 — 快的先出, 慢的后补

**背景**: `/api/dashboard` 并行拉 7 个区块但等最慢的(盘前信号/情绪, 冷拉取 20s+)
全部完成才整体返回, 前端一次性渲染, 打开首页/刷新时整个面板干等。

**改动**:
- `app.py` 新增 `/api/dashboard/stream` (SSE): 7 个区块并行拉取, 谁先完成先推送
  (`update` 事件携带当前已就绪字段), 全部完成后写缓存并 `complete`;
  缓存命中时一次性返回秒开; 原 `/api/dashboard` JSON 端点保留作兜底
- `static/dashboard.js` 重写加载逻辑: 先渲染全部卡片骨架(占位符),
  收到哪个区块就填哪个(涨停/炸板/跌停/资金流等快的先显示,
  盘前信号/情绪等慢的到了再补), 数字滚动/风险评估照常
- `templates/index.html` 资源版本升至 `20260802v3`

**验证**: 打桩确认推送顺序=完成顺序(limit→sentiment→zb_dt→premarket)、
缓存命中秒回、JSON 兜底正常; `python -m pytest` 240 passed。

## v3.6.6 (2026-08-02)

### 优化: 回测系统点开秒显(命中缓存直接展示)

**背景**: 前端 `loadBacktestTab` 每次请求都带 `force=true`(v3.3i 为破旧缓存加的),
导致每次点进回测页/切 tab 都全量重算, 后端本可 30ms 命中的结果缓存被完全绕开。

**改动**:
- `static/app.js: loadBacktestTab` 去掉 `force=true`, 默认走后端 daily 缓存:
  同一天/同参数/同权重直接返回, 首次计算后即"提前算好"
- `backtest/backtest_engine.py` 回测缓存 key 增加 `wh`(当前 tab 权重 hash):
  调权/权重优化保存后 hash 变 → 缓存自动失效重算一次, 之后秒开
  (取代"每次 force"方案, 保证权重变化后数据仍然新鲜)

**验证**: 缓存 key 含 `wh=<权重hash>`、命中缓存直接返回、权重稳定时 key 稳定;
`python -m pytest` 240 passed。

## v3.6.5 (2026-08-02)

### 修复: 缓存命中时进度条一闪而过/完全不显示

**背景**: 2h/当日缓存命中时后端秒回，前端在同一帧内执行"显示进度条→立刻隐藏"，
浏览器来不及绘制，用户看不到进度条（尤其「运行」从 raw 缓存重跑评分时没有任何事件流）。

**改动**:
- `static/app.js` 进度条加最短可见时间 (400ms)：秒回时补到 100% 并停留片刻再隐藏，
  所有加载路径统一走 `showProgress`/`hideProgress`（流式卡片/文本/JSON 卡片）
- `app.py: /api/scan/limit-up/run` 缓存重跑改后台线程，先上报"从缓存重跑评分 60%"
  再出结果；无原始缓存时同一线程内降级全量拉取，全程有进度事件
- `app.py: /api/scan/limit-up/stream` 缓存分支补 pct (90→100)，进度条不再是死值

**验证**: 端点打桩确认缓存命中先 60% 后 complete(from_cache)、降级拉取正常、
缓存分支 pct=[90,100]；`python -m pytest` 240 passed。

## v3.6.4 (2026-08-02)

### 新增: 涨停扫描「拉取/运行」真实进度条

**背景**: 旧进度条只有 HTML/JS 骨架且缺基础 CSS（桌面端不可见），前端靠关键词猜百分比，
与真实耗时阶段不对应（资金流/龙虎榜等网络等待期看不到实际进度）。

**改动**:
- `app.py: _scan_limit_up_data` 按真实阶段上报进度 (5%→100%)：涨停池 → 前置过滤 →
  资金流 → 股价/可买到/本金过滤 → 第6步并行预测数据 (N/M 逐项推进) → 市场情绪 →
  评分方案 → 完成
- `app.py: /api/scan/limit-up/stream`、`/api/scan/fetch-all`、`/api/scan/limit-up/run`
  SSE 消息增加 `pct` 字段（fetch-all 将扫描进度映射到全局 10-100，前 10% 为三个池子拉取），
  stderr 文本行继续作为无 pct 的说明性进度
- `static/app.js: loadCardViewStream` 优先使用后端真实 `pct`，进度条文案同步显示百分比；
  旧端点无 pct 时保留关键词估算回退
- `static/style.css` 补进度条基础样式（轨道/填充/状态文字），桌面端与移动端均可见

**验证**: SSE 端点打桩确认 pct 序列 5→100 递增、complete 事件正常；`python -m pytest`
242 passed（`test_get_sector_rotation_speed` 为既有失败，与本次改动无关）。

## v3.6.3 (2026-08-01)

### 优化: 跌停翘板空状态可解释 (不再是裸"暂无数据")

**背景**: 2026-07-31 (周五) 全市场 0 只跌停 (7/30 有 74 只, 7/29 有 9 只),
翘板扫描无标的是市场真实情况, 但前端只显示"暂无数据", 容易被误认为故障。

**改动**:
- `app.py: api_dtqiaoban_cards` 空池时返回 `empty_reason` (今日无跌停股属正常)
  + `recent_dieting` (近 5 个交易日跌停家数, 2h 缓存)
- `static/app.js: loadCardView` 空状态渲染原因 + 近期跌停数, 引导用户看强势板块

**验证**: 接口返回 `empty_reason` 与 `recent_dieting` (0730:74 / 0729:9 /
0728:49 / 0727:6 / 0724:25), 前端资源已更新。

## v3.6.2 (2026-08-01)

### 优化: 涨停扫描「拉取」提速 (24.9s → 1.7s 重复拉取)

**剖析结论** (服务器逐段计时): 全链路 24.9s 中, 市场情绪检测占 22.4s,
其中 20.4s 花在 `get_premarket_signal` (美股/期货等网络因子, 无缓存)。

**优化**:
1. `detect_market_sentiment` 结果缓存 2h (`market_sentiment_{date}`):
   重复「拉取」秒回, 不再每次全量重拉 5 个池 + 新浪采样 + 盘前信号
2. `get_premarket_signal` 缓存 2h (`premarket_signal_{date}`):
   盘前信号是日级数据, 20s 的网络因子聚合只需算一次
3. `_scan_limit_up_data` 并行化: 资金流 + 市场情绪提前与涨停池同时启动
   (互不依赖, 原串行排队); 每步用非阻塞 executor, 取结果后 shutdown
4. `score_alpha_factors` 逐股历史加 2h 缓存 (`alpha_hist_{code}_{s}_{e}`):
   alpha 因子扫全池从 ~1.9s → 0s (第二次起)

**实测 (公网, 服务器)**:
- 第 1 次拉取 (冷, 每日一次): 25.1s (情绪/盘前全量数据为固有成本, 已在进度条中)
- 第 2 次拉取 (2h 内): **1.7s** (原 24.9s, 提速 ~14x)

配套: dashboard 刷新受益于同一缓存 (情绪/盘前不再重拉)。

## v3.6.1 (2026-08-01)

### 修复: 首页"今日市场数据加载失败"

**根因**: nginx 代理默认 60s 读超时, 而 `/api/dashboard?refresh=1` 串行拉取
情绪/涨停池/炸板跌停/盘前/北向/资金流/市场状态合计 ~43s, 偶发超过 60s 被 nginx
截断 → 前端 catch 显示"数据加载失败"。

**修复**:
1. nginx `location /` 增加 `proxy_read_timeout 300s` / `proxy_send_timeout 300s` /
   `proxy_connect_timeout 10s` (公网复测刷新 200)
2. `app.py:api_dashboard` 刷新路径并行化: 7 个独立数据区块改
   `ThreadPoolExecutor(max_workers=4)` 并发拉取, 刷新耗时 **43.6s → 22.3s**
3. 验证: 公网带 token 刷新 HTTP 200, 缓存加载 0.01s, 关键字段完整

## v3.6 (2026-08-01)

### 前端全面优化 + 回测驱动预测概率 (已部署服务器)

**新增: 预测概率条 (每个股票卡片)**
- 新后端 `scoring/probability_estimator.py`: 用修复后的回测引擎对 5 个榜
  (涨停/趋势/炸板/翘板/反转) 构建 "评分分档 → 概率" 映射:
  - 次日上涨概率 (买入后第 2 交易日收盘 > 买入价)
  - 低开高走概率 (低开且收盘高于开盘, 条件概率)
  - 5日上涨概率 (信号日往后第 5 交易日收盘 > 买入价, ≈未来一周)
  - 按评分分档 (<60/60-70/70-80/80-90/≥90), 样本不足回退总体并标注 n
- 数据源: 回测 trades + 逐代码历史 K 线 (东财优先, 腾讯降级 — 服务器东财被封)
- API: `GET /api/probabilities?tab=xxx`; 首次请求后台构建, daily 缓存
- 前端: 卡片新增预测条 (红色次日上涨 / 黄色低开高走 / 绿色5日上涨 + n),
  数据就绪自动重渲染; 页面切换即加载

**前端清理**
- 移除所有卡片 "点击查看同花顺详情 →" 提示 (含社区/板块/迷你/龙虎榜卡片)
- 涨停卡片因子条 9 条 → 4 条关键因子 (资金/板块/量价/情绪), 移除可买性/本金/
  北向/持续性/回撤等低信息量条
- 移除工具栏 "方案" 下拉 (仅剩 Plan A)
- 静态资源版本号统一 v=20260801v1 (服务端另有启动时间戳自动破缓存)

**服务器部署 (134.175.231.8)**
- scp 直传 82 个源码文件 (GitHub webhook 不可用), 备份
  `stock-scanner-backup-20260801.tgz`, systemctl restart, 验证通过
- 首版概率表已预构建 (次日上涨: 涨停 61% / 趋势 46% / 炸板 37% / 翘板 47% /
  反转 40%; 5日上涨: 趋势 74% / 炸板 68% / 翘板 79% / 反转 51%)

## v3.5e (2026-08-01)

### 调权闭环结果 (前 20 交易日训练 / 后 10 交易日验证, 完整 30 天窗口复验)

**涨停榜 (score_new 权重)**: 训练集 EV 2.90→3.43 (回撤 -9.85%→-3.93%), 但验证集
EV 4.59 与基线完全一致 (同 17 笔) → **未保存** (默认权重在验证窗口已接近最优)。

**趋势榜**: 训练 -1.61→-1.36, 验证 -2.96→-2.19 (+0.77 pp) → **已保存**
`trend_weights.json`: chg 4.9 / turnover 29.1 / amount 19.4 / vol_ratio 11.7 /
new_high 20.4 / price 4.9 / ma_rev 9.7
完整 30 天复验: EV -2.62→-2.26, 胜率 38.7%→40.0% (75 笔)

**反转榜**: 训练 -1.24→-0.49, 验证 -0.97→-0.72 (+0.25 pp) → **已保存**
`reversal_weights.json`: turnover 17.9 / consecutive 36.1 / pullback 23.8 /
sector 17.3 / retention 4.8
完整 30 天复验: EV -1.46→-0.90, 胜率 47.1%→50.0% (62 笔)

**炸板榜**: 训练改善但验证中性 (n=2 样本过小) → 未保存
**翘板榜**: 训练 -4.72→-2.70 但验证 3.47→0.44 变差 → **按规则拒绝** (防过拟合生效)

**窗口修复**: `run_tab_backtest` 显式传入 start/end 时不再被 `_detect_available_days`
收缩窗口 (原"30 天回测"实际只跑 ~21 天且训练/验证重叠)。修复后真实 30 天基线:
涨停榜 54 笔 / 胜率 72.2% / EV +3.66% / 复利累计 +564% / 回撤 -9.85%。

**缓存版本**: `_CACHE_VER` 15→16, `_RAW_CACHE_VERSION` 11→12, 回测缓存 version 6→7
(trend/reversal 权重变更, 旧缓存评分失效)

**测试**: Section 12 "回测正确性回归" 18 项, 总计 136 项全过。

## v3.5d (2026-08-01)

### 提升 — 回测→IC→调权闭环 + 文件拆分 (便于 AI 维护)

**调权闭环打通**
- `scoring/score_new.py` 新增权重持久化 (`load_factor_weights`/`save_factor_weights`,
  文件 `${TEMP}/stock_scanner_cache/score_new_weights.json`), 生产与回测共用同一份
- 修复生产/回测权重断点: `scan_reversal` 未加载反转权重、`scan_dtqiaoban` 未加载
  翘板权重且未传 today_str (v2 位置因子生产端从未生效) — 现统一走
  `weight_manager.load_tab_weights(tab)` (新增统一入口, 历史函数保留兼容)

**前向验证优化器** (`scripts/optimize_weights_walkforward.py`)
- 前 20 交易日训练 / 后 10 交易日验证, 验证集 EV 提升才保存, 防过拟合
- 每轮回测在子进程执行 + 硬超时 (默认 480s), 网络挂起自动杀掉该轮
- 断点续跑: 每轮结果落盘 `_opt_{tab}.json`, 中断后重启自动跳过已算候选

**文件拆分 — backtest_engine.py (1870 → 917 行)**
- `backtest/backtest_tabs.py`   tab 常量/默认策略
- `backtest/backtest_metrics.py` 聚合统计/确定性成交/因子IC
- `backtest/backtest_pools.py`   信号池拉取 + 归档 fallback
- `backtest/backtest_ohlcv.py`   OHLCV 获取 + archive 构造兜底
- `backtest/backtest_scores.py`  6 tab 评分包装
- `backtest/backtest_engine.py`  主循环 facade, 全部历史符号保留
- 拆分脚本: `scripts/split_backtest_engine.py` (可重放)

**文档/测试**
- AGENTS.md 架构/评分/回测章节更新为现状
- `test_invariants.py` 新增 Section 12 "回测正确性回归" (13 项)

## v3.5c (2026-08-01)

### 正确性修复 — 回测引擎 + 评分体系 (审计驱动)

**P0-1 回测 OHLCV 数据源修复** (`backtest/backtest_engine.py`)
- `_get_daily_ohlcv_batch` 之前逻辑反了: 拒绝 today、却把 `stock_zh_a_spot_em`
  **今日实时快照**当作任意历史日期的 OHLCV 缓存, 导致历史回测买卖价全部变成
  今天的数据 (系统性失真)。现在: 只有当天才可能用快照, 历史日期一律走逐股历史
  API (腾讯/东财 `stock_zh_a_hist`); 同时 `_SPOT_DISABLED` 默认改为 True。

**P0-2 聚合指标修复** (`backtest/backtest_engine.py` + `backtest/t1_real_backtest.py`)
- `ev` 全赢样本不再恒为 0 (原 `if losses else 0` bug)
- `cumulative_ret` 改为**复利累计** `prod(1+r)-1` (原简单求和保留在 `cumulative_ret_sum`)
- `max_dd` 改为基于**复利资金曲线**的最大回撤 (原为收益求和曲线)
- 新增 `median_ret` 字段

**P0-3 回测可复现性修复**
- 尾盘买策略封单成交比 1.0~2.0 的 `random.random()` 改为 `(code,date)` 确定性哈希,
  同一配置两次运行结果完全一致

**P0-4 构造价交易严格跳过** (`strict_ohlcv` 默认 True)
- archive.db / stock_daily 构造的 OHLCV (buy_open≈信号日收盘、sell_open≈次日收盘)
  会系统性高估/失真收益, 默认跳过这类交易, 只统计真实历史 OHLCV;
  交易记录新增 `data_quality` 字段 (`real`/`constructed`)

**P1 评分体系修复**
- `plans/plan_a.py` alpha 因子死代码: 引用不存在的 `scoring_base`/`today_fmt`
  → NameError 被吞 → alpha 永远 5.0。现传入 `filtered + today_str`
- `scoring/scanner_factors.py:score_alpha_factors` 兼容 `YYYY-MM-DD` / `YYYYMMDD` 两种日期
- `_score_limit_up` 本金从硬编码 30000 改为透传 `run_tab_backtest(capital=)` (本金过滤
  与实盘一致)
- 尾盘买 (close-buy) 交易记录补录因子分列, 因子 IC 分析对涨停榜生效

**缓存版本**
- `_CACHE_VER` 14→15, `_RAW_CACHE_VERSION` 10→11, 回测缓存 key version 5→6
  (旧缓存自动失效, 按 AGENTS.md 约定)

**验证结果** (2026-07-01~07-31 30 交易日, 涨停榜 尾盘买 top3, min_score≥65, 真实 OHLCV)
- 36 笔 / 胜率 75.0% / EV +4.05% / 复利累计 +303.6% / 最大回撤 -9.85% / 完全可复现
- 因子 IC: 封板强度 +0.53, 封板时间 +0.30, 流通市值 +0.17, 连板数 +0.13
- 其余榜真实结果为负 (趋势 -2.62 / 炸板 -3.74 / 翘板 -2.19 / 反转 -2.51 EV),
  属真实信号质量, 留待评分体系优化

## v1.24.5 (2026-06-14)

### 修复 — 回测卖出日期错误 (2 项 BUG)
- **BUG 1 [高]**: `fc4859a` end 回退一天导致神剑股份 sell_date=6/11 应为 6/12
  - 根因: end 双重回退 → trade_dates[-1] 缩水 → d_sell 被 L733 兜底为 d_buy
  - 修法: 移除冗余的 end 回退, 改用 `_trading_date()` 直接取当前交易日
  - 未来函数防护已由 `_get_daily_ohlcv_batch` today 拦截负责
- **BUG 2 [中]**: d_sell 兜底时策略 A/B 产生同日买卖数据假象 (合锻智能 603011)
  - 根因: signal=6/11 → d_sell=6/15 超区间 → 兜底 d_sell=6/12 → A策略6/12同日买卖, buy=sell=32.64
  - 修法: 加 `d_sell_fallback` 标记, 跳过策略 A/B, 仅保留策略 C (信号日收盘买→次日卖, 有效 T+1)
- **API**: `/api/bt/{tab}/full` 新增 `force=true` 参数跳过 daily cache 强制重算

## v1.24.4 (2026-06-14)

### 移动端适配强化
- **viewport meta**: 加 `maximum-scale=1.0, user-scalable=no, viewport-fit=cover` (iOS 防止表单聚焦时缩放)
- **横向溢出防护**: `body { overflow-x: hidden }` + `.output-area` 局部限制
- **全局工具栏 2 行布局** (768px): 拉取/运行/本金/方案 各占 50% 宽, 自动换行
- **回测 panel 表格** 全部包 `.table-wrap` (横向滚动):
  - 权重对比表 (`min-width:260px`)
  - 因子相关性历史表 (`min-width:480px`)
  - TOP5/BOTTOM5 表格 (`min-width:380px`)
  - 全部交易明细表 (`min-width:680px`)
- **回测 panel 字号降级**: `[style*="font-size:20px"]` 在 768px→16px / 480px→14px
- **allTrades mini-width 元素放宽**: 150→110px, 160→110px, 140→100px (避免 flex 父不 wrap 挤压)
- **iOS 表单元素**: number/text 重置 `-webkit-appearance:none`, select 保留原生箭头
- **进度条 mobile 调紧凑**: padding 8px, track height 4px
- **板块标签 max-height:80px** + 纵向滚动 (避免 dashboard 顶部过高)
- **审计脚本**: `scripts/_mobile_audit.py` (CSS 解析 + 表格未包检测)

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
