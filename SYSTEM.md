# Stock Scanner 系统架构文档

> 最后更新: 2026-07-05

## 一、系统定位

A股超短线选股扫描器，5个tab（涨停/趋势/反转/炸板/跌停翘板）各自扫描→评分→回测→明日信号推荐。

## 二、核心数据流

```
akshare API → scanner_data.py(拉数据) → scanner_filters.py(过滤)
→ scanner_factors.py(算因子) → plans/plan_a.py(9因子加权评分)
→ scanner_scoring.py(各tab专用评分) → backtest_engine.py(回测验证)
→ app.py(API输出) → 前端渲染
```

## 三、文件职责（按层级）

### 基础层
| 文件 | 职责 | 大小 |
|------|------|------|
| `config.py` | 全局常量（手续费/阈值/时间） | 4KB |
| `cache.py` | 2h短期+每日持久缓存 | 13KB |
| `ak_utils.py` | akshare重试封装 | 2KB |

### 数据层
| 文件 | 职责 |
|------|------|
| `scanner_data.py` | 拉涨停池/资金流（带降级） |
| `scanner_filters.py` | 过滤（板块/除权/价格/一字板） |
| `archiver.py` | 每日数据归档到SQLite+pickle |
| `scanner_utils.py` | 纯工具函数 |

### 评分层
| 文件 | 职责 |
|------|------|
| `scanner_factors.py` | 15+个评分因子纯函数（封板/资金/板块/技术等） |
| `scanner_scoring.py` | 5个tab专用评分函数（调用factors） |
| `plans/plan_a.py` | 涨停tab的9因子加权+危险信号 |
| `plans/factors_v2.py` | v2因子（持续性+回撤位置） |
| `weight_manager.py` | 权重管理+IC驱动自动调权 |
| `weight_scheduler.py` | 盘后自动调权调度 |

### 回测层
| 文件 | 职责 |
|------|------|
| `backtest_engine.py` | **主回测引擎**：多tab统一入口，策略A(开盘买) |
| `t1_real_backtest.py` | 旧版T+1回测（limit-up专用，被backtest_engine兼容包装） |
| `scanner_backtest.py` | 旧版回测+评分验证（CLI入口） |
| `compare_strategies.py` | 多方案对比框架 |
| `strategy_filters_v2.py` | v2硬过滤器 |

### 信号层
| 文件 | 职责 |
|------|------|
| `signal_tomorrow.py` | 明日买入信号决策 |
| `recommendation_tracker.py` | 推荐追踪+次日胜率统计 |

### 辅助层
| 文件 | 职责 |
|------|------|
| `premarket.py` | 盘前多空信号（美股/A50/汇率） |
| `market_regime.py` | 市场状态分类（5种） |
| `north_flow_tracker.py` | 北向资金追踪 |
| `community.py` | 舆情+新闻聚合 |
| `indicators.py` | 增强指标（封成比/龙虎榜） |

### 入口层
| 文件 | 职责 |
|------|------|
| `app.py` | **FastAPI后端**（158KB，所有API路由） |
| `scanner.py` | CLI入口+公共API re-export |
| `scanner_scans.py` | 5种扫描模式主流程 |
| `scanner_format.py` | 文本输出格式化 |
| `data_manager.py` | 数据持久化+总结 |

## 四、回测系统现状

### 4.1 策略
- **策略A（开盘买）**：D+1开盘买入，D+N开盘卖出（唯一保留的策略）
- 已删除策略B（尾盘买）和策略C（休盘+止损）

### 4.2 参数（2026-07-05 当前值）
| tab | min_score | sell_n | 回测结果(26天,top1,3w) |
|-----|-----------|--------|----------------------|
| 涨停 | 50 | 3 | +3044 ✅ 66.7% |
| 炸板 | 50 | 5 | +3778 ✅ 60% |
| 跌停 | 50 | 3 | +4575 ✅ 46.2% |
| 反转 | 50 | 3 | -4568 ❌ 41.7% |
| 趋势 | 50 | 3 | -9289 ❌ 33.3% |

### 4.3 评分系统
- **plan_a 9因子**：封板/资金/板块/技术/买入性/情绪/本金/历史/北向
- **IC = -0.036**（弱负相关，但3/5 tab盈利，暂不重构）
- **数据驱动修正已回退**（价格驱动评分导致炸板25%胜率，已删除）

### 4.4 明日信号排序
- `compute_recommendation_score`：40%历史EV + 60%当日评分
- tab加权：dtqiaoban 1.5x, limit-up 0.5x, zhaban 0.3x, reversal 0.3x, trend 0.2x

## 五、数据存储
| 位置 | 内容 |
|------|------|
| `data/cache/` | 1770+个pkl（engine池缓存+OHLCV缓存） |
| `data/cache/archive_pools/` | 按日pickle归档（各tab信号池） |
| `data/archive.db` | SQLite（daily_stocks 20天, stock_daily） |
| `data/backtest_results.json` | 历史回测结果（追加写入） |
| `data/recommendations/` | 推荐追踪记录 |

## 六、部署
- 服务器：134.175.231.8（ubuntu）
- 服务：systemd `stock-scanner`（uvicorn 8080端口）
- 反代：nginx（token认证）→ uvicorn
- 部署方式：GitHub webhook（zip下载→覆盖→systemctl restart）
- 手动部署：`scp file ubuntu@ip:/home/ubuntu/stock-scanner/`

## 七、已知问题
1. `app.py` 158KB过大，API路由+业务逻辑混杂
2. 三个回测文件（engine/t1/scanner_backtest）职责重叠
3. `backtest_results.json` 追加写入导致JSON解析需要raw_decode
4. 前端localStorage版本管理（v1~v8）混乱
5. 服务器git HEAD与实际文件不同步（webhook用zip覆盖不用git pull）
