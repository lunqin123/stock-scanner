# CLAUDE.md

A股超短线选股扫描器。基于 akshare 的多因子评分系统，含 Web 面板 + CLI + 回测 + 自动调权。

## 启动

```bash
cd C:\Users\16689\Desktop\stock-scanner
python app.py          # Web 服务 (FastAPI, 端口 8080)
python scanner.py      # CLI 扫描
python scanner.py --trend   # 趋势扫描（盘中可买）
python scanner.py --zhaban  # 炸板反包
python scanner.py --sector  # 板块联动
python scanner.py --dtqiaoban  # 跌停翘板
python scanner.py --backtest  # 回测
python test_invariants.py    # 64项不变性测试
```

## 架构

```
app.py              Web API + 扫描管道 + 缓存 + 收盘调度
scanner.py          评分函数 + CLI入口 + 回测系统
plans/plan_a.py     评分方案 (9因子加权 + 危险信号 + 龙头检测)
weight_manager.py   权重管理 + 每日自动调权 (lr=0.02, 近5日均值)
cache.py            缓存系统 (daily JSON + 2h pickle + 交易日历)
community.py        舆情监测 (股吧+千股千评+新闻)
indicators.py       龙虎榜增强指标
data_manager.py     数据持久化 + 每日总结
static/app.js       前端主逻辑 (选项卡切换/SSE流/缓存)
static/cards.js     卡片渲染 (涨停/趋势/炸板/翘板/板块/回测面板)
test_invariants.py  64项不变性测试
```

## 评分体系

9因子加权 (100分制，`weight_manager.py:15-26`):
- seal(6)+tech(11)+sector_res(8)+sector_mom(12)+history(8)+money(3)+buyability(12)+stock_sentiment(9)+principal(6)
- stock_sentiment 含舆情分数 30% 权重
- 大盘情绪 sentiment 是乘法系数 (×0.85~1.15)

## 关键流程

**"拉取"按钮**: `/api/scan/fetch-all` → `_scan_limit_up_data()` → `plan.score()` → 保存 `raw_scan_data.pkl` (含 pool/scoring_base)
**"运行"按钮**: `runCurrentFromCache()` → 流式端点 → `_scan_from_raw_cache()` → `plan.score()` (复用缓存数据，换本金重新评分)
**tab切换**: `switchPage()` → `_outputCache` 命中展示 / 未命中调 card API

## 缓存系统

- daily缓存: `daily_{date}_{key}_v{_CACHE_VER}.json` (全天有效，次日过期)
- 原始缓存: `raw_scan_data.pkl` (含 version 字段，`_RAW_CACHE_VERSION=2`)
- 流端点缓存: 统一走 `daily_get`/`daily_set`，缓存键不带后缀
- 改格式时: `_CACHE_VER += 1` 自动全局失效 / `_RAW_CACHE_VERSION += 1` 原始缓存失效

## 回测 + 调权

- `auto_verify_backtest()` 在每次扫描后 daemon 线程运行 → 保存因子相关性到 `rolling_correlations.json`
- `daily_adjust_weights()` 取近5天滚动均值，每天调权 (lr=0.02)
- 可调权因子: seal/sector_mom/tech/sector_res/history (5个)
- 权重软钳制: 0.5x~1.5x 默认值
- 模拟交易排除次日一字板 (换手<1%+涨>9.5%)
- 回测面板: `/api/backtest/dashboard`

## 核心约定

- 盘中默认趋势扫描，盘后默认涨停评分 (`get_market_status()`)
- 数据格式: akshare 时间 "092502" (无冒号)，换手率为字符串需转换
- 板块过滤: `filter_non_main_board()` 统一排除 ST/科创/北交/创业板
- 前端 SSE 流: `loadCardViewStream` 处理 `msg.items`，`loadTextViewStream` 处理 `msg.output`
- 编码: Windows GBK 终端避免 emoji，用纯中文
- 无参运行自动检测市场状态 (`get_default_mode()`)
