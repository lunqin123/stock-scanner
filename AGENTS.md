# AGENTS.md

A股超短线选股扫描器 (akshare 多因子评分, Web 面板 + CLI + 回测 + 自动调权)。

## 启动

```bash
cd C:\Users\16689\Desktop\stock-scanner
python app.py                    # Web 服务 (FastAPI, 8080)
python scanner.py                # CLI 扫描
python scanner.py --trend|--zhaban|--sector|--dtqiaoban|--backtest
python -m pytest                 # 全部测试 (联网集成测试默认跳过)
python -m pytest -m network      # 含真实行情数据的完整不变性验证
python utils/test_invariants.py  # 旧命令兼容 (等价 -m network 全量)
```

## 架构

```
app.py        Web API + 扫描管道 + 缓存 + 收盘调度
scanner.py    CLI 入口 + 兼容 re-export
core/         cache/config/scanner_utils/scanner_filters
data_layer/   scanner_scans 扫描主流程 + 数据/格式化
scoring/      scanner_factors 因子 + scanner_scoring 5模式
              + score_new(涨停排名) + weight_manager/weight_scheduler
plans/        plan_a + factors_v2 + datasource
signals/      舆情/龙虎榜/北向/明日信号/市场状态
backtest/     引擎子模块 (tabs/metrics/pools/ohlcv/scores/engine)
utils/        test_invariants.py (pytest 化, 联网项打 network 标记)
static/       app.js / cards.js
```

根目录 `*.py` 均为兼容 shim (1行 re-export, 保持旧 import), 勿删。

## 评分体系

- 涨停: plan_a 10因子 (seal 28/tech 8/sector 17/money 17/history 6/stock_sentiment 13/
  principal 8/north_flow 5/alpha 8/crash_resistance 5, 情绪为乘性系数); 排名由
  **score_new 覆盖** (封成比/封板时间/换手/连板/炸板/市值/板块/价格, 权重和=100,
  文件 `score_new_weights.json` 可调)
- 趋势/炸板/反转/翘板: 各 tab 独立权重, 统一入口 `load_tab_weights(tab)`
- 权重文件均在 `${TEMP}/stock_scanner_cache/`, 生产扫描与回测共用同一份
- 明日信号排序: `compute_recommendation_score` = 40% 历史 EV + 60% 当日评分;
  tab 加权 dtqiaoban 1.5x / limit-up 0.5x / zhaban 0.3x / reversal 0.3x / trend 0.2x

## 关键流程

- **"拉取"按钮**: `/api/scan/fetch-all` → `_scan_limit_up_data()` → `plan.score()` → 保存 `raw_scan_data.pkl`
- **"运行"按钮**: `runCurrentFromCache()` → 流式端点 → `_scan_from_raw_cache()` → `plan.score()` (换本金重新评分)
- **tab切换**: `switchPage()` → `_outputCache` 命中展示 / 未命中调 card API

## 缓存系统

- daily缓存: `daily_{date}_{key}_v{_CACHE_VER}.json` (全天有效, 次日过期)
- 原始缓存: `raw_scan_data.pkl` (当前 `_RAW_CACHE_VERSION=11`)
- **⚠️ 改评分逻辑必须同时升 `_CACHE_VER` + `_RAW_CACHE_VERSION` + 回测缓存 version**,
  否则旧缓存永不失效; 回测结果缓存: `make_key("bt","result",version=6,...)`

## 回测 + 调权

- 主入口: `run_tab_backtest(tab, ...)` / `run_tab_backtest_auto(tab)` (6 tab)
- **正确性约定 (2026-08-01)**: OHLCV 只用真实历史 (spot 快照对历史日期禁用);
  `strict_ohlcv=True` 默认跳过构造价; EV=胜率×均值, 累计收益复利, 回撤基于资金曲线;
  成交确定性 hash 可复现
- 权重优化: `python scripts/optimize_weights_walkforward.py --tab limit-up --save`
  (前20天训练 + 后10天验证, 验证集提升才保存)
- `weight_scheduler.py` 盘后自动调权: `_run_close_scan()` 完成后触发;
  盘中 `get_market_status() in ('trading','lunch')` 跳过; 互斥锁 + `data/weight_adjust.lock`
- 状态查询: `GET /api/weights/status`; 手动: `POST /api/weights/run?force=bool`
  或 `python weight_scheduler.py --force`
- 可调权因子: seal/sector_mom/tech/sector_res/history; 软钳制 0.5x~1.5x 默认值
- 模拟交易排除次日一字板 (gap≥9.5%); 回测面板: `/api/backtest/dashboard`

## 数据存储

```
data/cache/            缓存 (pkl/json/交易日历)
data/archive.db        SQLite (daily_stocks/stock_daily)
data/recommendations/  推荐追踪记录
daily_data/            按日快照
```

## 核心约定

- 盘中默认趋势扫描, 盘后默认涨停评分 (`get_market_status()` / `get_default_mode()`)
- akshare 时间 "092502" (无冒号); 换手率为字符串需转换
- 板块过滤: `filter_non_main_board()` 统一排除 ST/科创/北交/创业板;
  开关 `config.INCLUDE_CHINEXT` (默认 False, True 保留 30/301 创业板 + 688 科创板)
- 前端 SSE: `loadCardViewStream` 处理 `msg.items`, `loadTextViewStream` 处理 `msg.output`
- 编码: Windows GBK 终端避免 emoji, 用纯中文

## 服务器部署

生产: `134.175.231.8` (Ubuntu, nginx → FastAPI:8080), 密钥 `qqChatBot.pem`。

```bash
ssh -i qqChatBot.pem ubuntu@134.175.231.8
# 项目路径 /home/ubuntu/stock-scanner; 服务 systemctl restart stock-scanner
# Webhook 自动部署: POST /webhook (GitHub push 触发)
sudo journalctl -u stock-scanner -f   # 日志
```

> 改前端 JS/CSS 推送后: 服务器重启 + 浏览器 Ctrl+Shift+R。
> nginx 层 token 认证, 联系管理员获取。

## 已知问题

- `app.py` 过大, API 路由与业务逻辑混杂 (有待拆分)
- 回测文件职责部分重叠 (engine/t1/scanner_backtest)
- 服务器 webhook 用 zip 覆盖部署, git HEAD 与实际文件可能不同步
