# 回测引擎架构 (2026-08-01)

## 模块地图

```
backtest/
  backtest_tabs.py    6 tab 常量 + 默认买入策略 + 自取池标记
  backtest_metrics.py 聚合统计 (复利累计/资金曲线回撤/EV) + 确定性成交 + 因子IC
  backtest_pools.py   信号池拉取 + 本地归档 fallback + 持久缓存
  backtest_ohlcv.py   OHLCV 获取 (spot 快照只允许当日; 历史走逐股历史 API)
  backtest_scores.py  6 tab 评分包装 + SCORE_FUNCS / SCORE_COLUMNS
  backtest_engine.py  主循环 + run_tab_backtest / run_tab_backtest_auto / run_t1_backtest
```

根目录 `backtest_engine.py` 是兼容 shim (`from backtest.backtest_engine import *`)。

## 一次回测的数据流

```
run_tab_backtest(tab, start, end, top_n, min_score, sell_n, capital, ...)
  ├─ _detect_available_days(tab)           # 本地归档可用天数 (上限 120)
  ├─ _trading_dates_in_range(start, end)   # 交易日序列
  ├─ 逐信号日:
  │   ├─ fetcher(d_signal)                 # SIGNAL_POOL_FETCHERS[tab]
  │   │    akshare → 失败 → 本地 pickle 归档 → 持久缓存 (engine_{pool}_{date})
  │   ├─ score_fn(pool, date, capital)     # SCORE_FUNCS[tab]
  │   │    涨停: plan_a 因子 + score_new 覆盖排名 (权重可调)
  │   │    其它: 各 tab 评分 + load_tab_weights(tab) 权重
  │   ├─ 评分列过滤: df[score_col] >= min_score → 排序取 top_n
  │   ├─ OHLCV: 批量/逐股历史 API → archive.db 构造兜底 (_fallback 标记)
  │   ├─ 严格模式 (strict_ohlcv=True): 买卖价含构造数据 → 跳过该笔
  │   ├─ 可买性: gap>=9.5% 一字板跳过; 5%~9.5% 排队模拟价; 炸板隔夜崩>3% 跳过
  │   └─ 成交模拟: close-buy 封单成交比 1.0~2.0 用确定性哈希 (可复现)
  ├─ _aggregate(records)                   # 复利累计 + 资金曲线回撤 + EV
  ├─ _compute_factor_ics(records, tab)     # 因子列 × net_ret_pct 相关系数
  └─ 缓存 (version=6) + 持久化 backtest_results.json
```

## 正确性约定 (2026-08-01)

1. **OHLCV 只用真实历史**: `stock_zh_a_spot_em` 是今日实时快照, 对历史日期一律
   `return {}` 降级到逐股历史 API (腾讯/东财)。`_SPOT_DISABLED=True`。
2. **构造价不进统计**: archive.db / stock_daily 构造的 OHLCV 标记 `_fallback`,
   默认严格模式跳过; 交易记录带 `data_quality` (`real`/`constructed`)。
3. **聚合口径**: `ev` 恒为 胜率×平均盈利+败率×平均亏损 (全赢样本不再为 0);
   `cumulative_ret` 复利; `max_dd` 基于资金曲线; `cumulative_ret_sum` 保留简单求和。
4. **可复现**: 成交模拟用 `(code,date)` 哈希, 同一配置两次运行逐笔一致。
5. **生产/回测权重同源**: 统一走 `weight_manager.load_tab_weights(tab)`;
   score_new 权重走 `scoring.score_new.load_factor_weights()`。

## 调权闭环

- `scripts/optimize_weights_walkforward.py --tab limit-up --save`:
  前 20 交易日训练 / 后 10 交易日验证; 每轮回测在子进程离线执行 (akshare 禁用,
  只吃缓存), 硬超时 480s; 验证集 EV 提升才保存权重。
- 断点: `_opt_{tab}.json` 记录每轮结果, 中断后重启自动跳过已算候选。

## 改回测代码的检查清单

1. 改评分/口径 → 升 `make_key("bt","result",version=N)` + `_CACHE_VER` +
   `_RAW_CACHE_VERSION` (见 AGENTS.md)
2. 跑 `python utils/test_invariants.py` (118+ 项, Section 12 覆盖回测正确性回归)
3. 同配置跑两次确认可复现
