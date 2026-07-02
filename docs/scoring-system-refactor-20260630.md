# 评分系统重构总览 (2026-06-30)

5 轮修复, 12 个 BUG, 5 个 commit, 12 个文件, +186/-106 行。

## Commit 链

```
bb3ef09 fix: BUG-2/3/4/5 — REV_FACTOR_NAMES 重复/lhb_bonus 损失/Sina 采样偏差/bare except
54fe03e fix: BUG-1 (principal_score 因子无法调权) + BUG-6 (cache 版本号未 bump)
7212261 fix(score): P2 回测窗口/调权逻辑/可调权因子 三处升级
b60612f fix(score): P1 修复 sector 压缩/危险信号过罚/north_flow 双计费
d1f5c92 fix(score): P0-3 修复评分系统 double-dipping 与作用域 bug
```

## 修复一览 (12 BUG)

### P0 (评分核心) — 3 个 Critical
| BUG | 现象 | 修复 |
|---|---|---|
| **P0-1** | `stock_sentiment` 因子重复计费: 资金 (money) 和板块 (sector) 被三重计算 | 重写为只反映"连板位置 + 流通市值分档", 与 8 因子完全正交 |
| **P0-2** | `score_danger_signals` 规则 7 (股性差/超跌反弹) 实际从未触发, 因 `parts` 变量 NameError 被 except 吞掉 | 合并规则 3+7 到统一 try 块 |
| **P0-3** | 舆情分数 `fillna(stock_sent)` 导致 30% 权重失效 (`x*0.7 + x*0.3 = x`) | 改 `fillna(5.0)` (中性分) |

### P1 (评分辅助) — 3 个 Important
| BUG | 现象 | 修复 |
|---|---|---|
| **P1-1** | sector 因子 33% 权重被压缩: `(sector_res + sector_mom)/2.0` 让 15 分满分压到 11.5 | 直接用 `sector_mom` (superset) |
| **P1-2** | 危险信号加性 clip(-30) 让烂票"撞上限"后失去区分度 | 改乘性 `base * (1 + penalty/100)`, 范围 0.7~1.0 |
| **P1-3** | north_flow 在 sentiment 北向修正 (±0.06 倍) + 加权 5% 双计费 | 删 sentiment 北向累加, 只走 5% 加权 |

### P2 (调权系统) — 3 个 Improvement
| BUG | 现象 | 修复 |
|---|---|---|
| **P2-1** | ROLLING_WINDOW=5 天, IC 标准误 0.045, 噪声阈值 0.02 几乎不触发 | 扩到 20 天 (IC 标准误 0.022) |
| **P2-2** | Plan A delta 驱动只取 signed IC, 易被单日噪声主导 | 改 ICIR + EMA 平滑 (50%), 与 Plan B 一致 |
| **P2-3** | BACKTEST_FACTORS 只 4 因子 (seal/tech/sector/history), 4 因子 (money/stock_sentiment/principal_score/north_flow) 锁死 | 扩到 8 因子, 全加权系统可调权 |

### BUG-1/6 (调权数据流) — 2 个 Critical
| BUG | 现象 | 修复 |
|---|---|---|
| **BUG-1** | `scanner_backtest.py` 的 `fkey.replace('_score', '')` 把 `principal_score` → `principal`, 但 BACKTEST_FACTORS 用 `principal_score`, **principal_score 因子永远无法被 daily_adjust_weights 识别** (P2-3 实际只 7 因子生效) | 引入 `_FKEY_TO_WEIGHT_KEY` 显式映射表 (11 条目) |
| **BUG-6** | `_CACHE_VER` (8→9) 和 `_RAW_CACHE_VERSION` (6→7) 未在 P0/P1/P2 评分大改时同步 bump, 旧 daily 缓存里的 total_score 仍是旧公式算的 | 同步 bump, 旧缓存自动失效 |

### BUG-2/3/4/5 (代码质量) — 4 个 Low
| BUG | 现象 | 修复 |
|---|---|---|
| **BUG-2** | `REV_FACTOR_NAMES` 在 L519 (4 因子) 和 L612 (5 因子) 重复定义, 前者是 dead code | 删 L519 旧定义 |
| **BUG-3** | `lhb_bonus` 加到 money 后 `clip(20)`: money=20 + lhb=5 → 25 → 20, 损失 5 分 lhb 信号 | lhb_bonus 独立计分, 缩放 50%, 在 base 上加性叠加 |
| **BUG-4** | Sina 全市场涨跌采样只取 4 page (1,15,30,45) = 400 只, 95% CI 误差 ±3% | 改 12 page 等距采样 (1200 只), 误差 ±2%, 并行度 4→8 |
| **BUG-5** | 10 处 bare `except: pass` 吞掉 KeyboardInterrupt/SystemExit (Python 反模式) | 全部改为 `except Exception: pass` |

## 改动文件

```
app.py              |   7 +--
archiver.py         |   3 +-
cache.py            |   4 +-
plans/datasource.py |   6 ++-
plans/plan_a.py     |  27 +++++++++---
plans/plan_b.py     |   6 ++-
scanner_backtest.py |  26 ++++++++++-
scanner_factors.py  | 123 +++++++++++++++++++++++++++++-----------------------
scanner_scans.py    |   6 +--
scanner_scoring.py  |   6 ++-
weight_manager.py   |  61 ++++++++++++++++----------
weight_scheduler.py |  17 ++++----
12 files changed, 186 insertions(+), 106 deletions(-)
```

## 关键设计改动

### 1. 因子边界 (P0-1 重构后)
| 因子 | 权重 | 满分 | 反映 | 与其他因子正交 |
|---|---|---|---|---|
| seal | 28 | 28 | 封板时间+封单+炸板+黄金奖励 | ✓ |
| money | 17 | 20 | 主力净流入阶梯 | ✓ |
| sector | 17 | 15 | 板块涨停数+资金一致性+ETF共振 | ✓ |
| tech | 8 | 10 | 换手率博弈区间 | ✓ |
| history | 6 | 6 | 历史涨停频率 | ✓ |
| stock_sentiment | 13 | 10 | **连板位置 + 流通市值分档** (新) | ✓ |
| principal_score | 8 | 10 | 本金可买手数 + 流动性 | ✓ |
| north_flow | 5 | 10 | 北向资金 (市场级) | ✓ |
| **加权和** | **102** | | | |

### 2. 调权系统 (P2-2 重构后)
- **Plan A**: ICIR 加权 (按 |IC|/σ 比例) + EMA 50% 平滑 + 钳制 [0, 1.5×default]
- **Plan B**: ICIR 加权 (与 Plan A 一致, 无 EMA 平滑)
- **8 因子全部纳入调权** (P2-3)
- **20 天窗口** 让噪声阈值真正起作用
- **数据流**: auto_verify_backtest → 归档 fkey 映射 → save_daily_correlations → daily_adjust_weights

### 3. 危险信号 (P1-2 重构后)
- 旧: 加性 `total = (base + penalty).clip(lower=0)`, 累加 clip(-30) 让烂票"撞上限"
- 新: 乘性 `total = base * (1 + penalty/100)`, 范围 0.7~1.0, 保留 30% 区分度
- 7 条规则, 每条贡献固定扣分 (-5 ~ -15), 累乘不累加

### 4. Cache 失效
- `_CACHE_VER`: 8 → 9 (daily JSON 缓存)
- `_RAW_CACHE_VERSION`: 6 → 7 (raw_scan_data.pkl)
- 旧 v6/v8 缓存自动检测 + 删除 + 重新拉取

## 集成测试覆盖

每个 commit 都有针对性测试:
- **P0**: stock_sentiment 独立性验证 (money 全 0 时 score 不变), 规则 7 触发验证, 舆情 fillna 5.0 验证
- **P1**: sector_mom vs merged 对比, 乘性 vs 加性 total 差异, sentiment 不再含 north_bonus
- **P2**: ROLLING_WINDOW=20, BACKTEST_FACTORS 8 因子, ICIR+EMA 调权 8/8 钳制正确
- **BUG-1/6**: _FKEY_TO_WEIGHT_KEY 11 条目, v6 缓存自动失效
- **BUG-2/3/4/5**: REV_FACTOR_NAMES 单一定义, 0 个 bare except, lhb_adjust 独立, Sina 12 page

## 部署后预期

### 立即生效 (盘后扫描触发)
1. **cache 全失效**: 旧 v6/v8 daily 缓存被识别过期, 重新拉取
2. **principal_score 因子调权生效**: 之前锁死, 现在加入 ICIR 调权
3. **舆情分数真正起作用**: 之前 fillna 失效, 现在用中性分 5.0
4. **龙虎榜净买卖能影响分数**: 之前 money.clip(20) 吞掉, 现在 lhb_adjust 独立计分
5. **大盘情绪数据更准**: Sina 采样 400 → 1200 只, 误差 ±3% → ±2%

### 中期观察 (1 周)
1. **BACKTEST_FACTORS 8 因子**累计 20 天数据后, 噪声因子 (|IC|<0.02) 自动剔除
2. **Plan A 调权**开始基于更稳的 ICIR 估计
3. **seal 强 ICIR 因子** (历史 r=+0.126) 在 normalize 后占比 +3pp

### 长期效果 (2 周+)
1. 评分系统对市场 regime 变化反应更快
2. noise 因子 (stock_sentiment/principal) 被 ICIR 验证, 不再"陪跑"

## 后续建议

### 高优先级
- [ ] 监控 P0-1 新 stock_sentiment 表现: 是否真的反映"连板+市值"独立信号
- [ ] 监控 P1-1 sector 因子加权后, 高板块联动票是否真的涨得更好
- [ ] 监控 P2-3 principal_score 调权方向, 验证其 IC 估计

### 中优先级
- [ ] weight_scheduler 跑的时间从 5 天延长到 20 天, 监控延迟 (可能 30s → 90s)
- [ ] daily 缓存首次重算会触发全量拉取, 服务器首次 webhook 后注意 CPU
- [ ] 8 因子 IC 估计在 noise 因子 (|IC|<0.02) 剔除后, 实际生效的是 5-6 因子, 是否需要进一步扩窗口

### 低优先级
- [ ] BACKTEST_FACTORS_B (Plan B 14 因子) 也可以考虑扩到 Plan A 同等 8 因子覆盖
- [ ] seal_time_score (单值版) 和 _vectorized_seal_time_score (向量化版) 可以考虑统一
- [ ] _sina/_em 重试机制可以更鲁棒 (现在 4-5 个 try 块分散)

## 相关文件

- `plans/plan_a.py` — Plan A 评分主入口 (含 3 处 P0/P1 修复)
- `scanner_factors.py` — 因子层 (含 4 处 P0/P1/BUG 修复)
- `weight_manager.py` — 权重管理 (含 3 处 P2 + 1 处 BUG 修复)
- `weight_scheduler.py` — 调权调度 (含 1 处 P2 修复)
- `scanner_backtest.py` — 回测系统 (含 1 处 BUG 修复)
- `cache.py` / `app.py` — Cache 版本号 (含 1 处 BUG 修复)
- `app.py` / `archiver.py` / `plans/datasource.py` / `plans/plan_b.py` / `scanner_fans.py` / `scanner_scoring.py` — BUG-5 bare except 修复
