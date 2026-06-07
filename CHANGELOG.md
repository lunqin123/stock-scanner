# Changelog

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
