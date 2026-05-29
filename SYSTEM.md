# stock-scanner 系统逻辑文档

## 1. 架构总览

```
浏览器 / QQ内置浏览器
    │
    ▼ http://134.175.231.8
  nginx (token认证 + /webhook 放行)
    │
    ▼ proxy_pass 127.0.0.1:8080
  FastAPI (app.py)
    │
    ├── 涨停扫描 ─── _scan_limit_up_data() ─── 7因子加权 → TOP10
    ├── 板块热度 ─── akshare 涨停池行业分布 → 评分卡片
    ├── 趋势扫描 ─── 昨日涨停今日续强 → 动量卡片
    ├── 炸板分析 ─── 独立评分模型 → 反包潜力
    ├── 跌停翘板 ─── 独立评分模型 → 翘板信号
    ├── 龙虎榜 ─── indicators.py → 封成比/仓位/龙虎榜
    ├── 舆情监测 ─── community.py → 新闻/股吧/千股千评
    ├── 市场情绪 ─── detect_market_sentiment() → 0-10分
    ├── 市场概览 ─── api_dashboard() → 情绪+涨跌停+板块
    └── 回测系统 ─── backtest_score_prev + 模拟交易
```

## 2. 评分管线（核心）

### 2.1 因子体系

| # | 因子 | 原始分范围 | 权重 | 贡献 |
|---|------|-----------|------|------|
| 1 | 封板强度 seal | 0-25 | 14 | 封板时间+封单+换手+炸板次数 |
| 2 | 量价结构 tech | 0-10 | 16 | 换手率区间+封单/成交额比+涨停频率 |
| 3 | 板块共振 sector_res | 0-8 | 10 | 同板块今日涨停数 |
| 4 | 市场情绪 sentiment | 0-10 | 25 | 昨日涨停溢价+全市场涨跌比+炸板率 |
| 5 | 晋级预期 sector_mom | 0-12 | 15 | 板块内资金一致性 |
| 6 | 历史股性 history | 0-6 | 12 | 近期涨停频率(次数/天数) |
| 7 | 资金驱动 money | 0-20 | 8 | 主力净流入+龙虎榜加分 |

**权重和 = 100**

### 2.2 评分公式

```python
weighted = seal × 14/25 + tech × 16/10 + sector_res × 10/8
         + sentiment × 25/10 + sector_mom × 15/12
         + history × 12/6 + money × 8/20
total = weighted / 100 × 100  → 0-100
```

**本金过滤**（不参与评分）：
- `<10万` → 梭哈1只，能买<0.5手排除
- `≥10万` → 分3份，每份能买<0.5手排除

### 2.3 评分流程

```
涨停池(akshare) → 过滤ST/688/8xx/一字板/市值>200亿/晚封
    → 资金流(同花顺) → 股价过滤(>60元排除)
    → 7因子评分 → 本金过滤 → apply_weights → TOP10
```

## 3. 缓存系统

### 3.1 缓存层次

| 缓存 | TTL | 存储 | 用途 |
|------|-----|------|------|
| 每日缓存 `daily_*` | 跨交易日 | JSON文件 | 涨停排行、市场概览、情绪 |
| 短期缓存 `cache_*` | 2小时 | Pickle | 板块/趋势/炸板/翘板卡片 |
| 资金流缓存 | 盘中5分钟/盘后0 | Pickle | 同花顺资金流数据 |
| 交易日历 | 7天 | TXT文件 | akshare交易日历 |

### 3.2 冻结机制

- **盘后 15:00 → 次日 9:30**：每日缓存冻结，`refresh=1` 不覆盖
- **收盘 15:05**：自动扫描 `force=True` 写入最终快照
- **节假日**：同周末处理，冻结缓存
- **凌晨 0:00-9:30**：`_today_trading()` 归为前一个交易日

### 3.3 交易日历

`_load_trading_calendar()` 从 akshare 拉取 8797 天交易日历，缓存 7 天。所有日期函数均通过 `_is_trading_day()` 校验。

## 4. 市场情绪算法

```
detect_market_sentiment(today_str):
  1. 取"昨天"（回退找最近交易日）
  2. 获取昨日涨停池 → prev_limit
  3. 计算 avg_premium（昨日涨停今日平均涨幅）
  4. 计算 promo_rate（晋级率 = 今日仍涨停的比例）
  5. 基础分：avg_premium>3→9(高潮) >1→7(活跃) >-1→5(正常) >-3→3(低迷) else→1(冰点)
  6. 全市场涨跌比修正：涨占比<20%→-3分
  7. 今日涨停/跌停修正：涨停≥60→+1, 跌停>30→-1
  8. 炸板率修正：>40%→-2
  9. 返回 0-10 分
```

情绪是独立因子（25/100），不再作为乘数。

## 5. 回测系统

### 5.1 评分对齐

`backtest_score_prev()` 使用与实盘完全相同的 `apply_weights` 7 因子模型。

历史不可用的因子用默认值：
- money: 10.0（中性）
- sentiment: 5.0（中性）

### 5.2 模拟交易

`_simulate_trades()` 模拟 TOP10 买入次日卖出：
- 扣除佣金（万2.5双向）+ 滑点（0.1%）
- 输出：均收益、胜率、盈亏比、最大回撤

### 5.3 自动调权

每次扫描后后台线程运行 `auto_verify_backtest`：
- 周一至周四：保存因子相关性到 `rolling_correlations.json`
- 周五：调用 `weekly_adjust_weights`，基于本周均值调整 3 个回测因子权重

## 6. 前端

### 6.1 服务端注入

`index()` 函数将缓存排行注入 HTML：
```html
<script>window._CACHED_RANKING = {...}</script>
```
手机端无需 API 请求即可瞬间展示排行。

### 6.2 缓存机制

- localStorage 持久化本金和页面缓存
- `_outputCache` Proxy 自动同步到 localStorage
- `_lastUrl` 跟踪避免重复请求

### 6.3 市场状态

侧边栏底部显示当前状态：⚡盘中 / 🌙盘后 / ☕午休 / 🎉休市 / 🎌假日

## 7. 部署

### 7.1 Webhook

GitHub push → POST `/webhook` → nginx 放行 → 下载 zip → 解压替换文件 → `systemctl restart stock-scanner`

### 7.2 CI 门禁

`.github/workflows/ci.yml`：push/PR 时自动运行 `test_invariants.py` 55 项不变性测试。只改 UI/版本号时不触发。

## 8. 数据源

| 数据 | akshare 函数 | 说明 |
|------|-------------|------|
| 涨停池 | `stock_zt_pool_em` | 当日涨停股 |
| 昨日涨停 | `stock_zt_pool_previous_em` | 含今日涨跌幅 |
| 炸板池 | `stock_zt_pool_zbgc_em` | 炸板股 |
| 跌停池 | `stock_zt_pool_dtgc_em` | 跌停股 |
| 资金流 | `stock_fund_flow_individual` | 同花顺个股资金流 |
| 龙虎榜 | `stock_lhb_detail_em` | 龙虎榜明细 |
| 股吧排名 | `stock_hot_rank_em` | 人气排名 |
| 千股千评 | `stock_comment_em` | 综合评分 |
| 交易日历 | `tool_trade_date_hist_sina` | A股交易日 |
| 全市场涨跌 | 新浪API | 4页采样 |

## 9. 文件清单

| 文件 | 职责 |
|------|------|
| `app.py` | Web API、扫描管道、收盘调度、Webhook |
| `scanner.py` | 评分函数、回测、CLI入口 |
| `weight_manager.py` | 权重管理、`apply_weights`、周调权 |
| `cache.py` | 缓存系统、交易日历 |
| `community.py` | 舆情聚合（股吧+千股千评+新闻） |
| `indicators.py` | 龙虎榜增强指标 |
| `static/app.js` | 前端主逻辑 |
| `static/cards.js` | 卡片渲染 |
| `static/style.css` | UI 样式 |
| `static/dashboard.js` | 市场概览 |
| `test_invariants.py` | 55项不变性测试 |
| `version.json` | 版本号和更新日志 |
| `.github/workflows/ci.yml` | CI 门禁 |
