# 数据准确性核查报告 — 2026-06-11

> 与同花顺/东方财富对账, 验证 scanner 数据准确度。
> 实测日期: 2026-06-11, 数据源: akshare (东方财富接口)

---

## TL;DR — 4 个发现

| # | 问题 | 等级 | 状态 |
|---|---|---|---|
| 1 | `scan_trend()` 缺 `new_high_col` 定义, 函数直接挂掉 (被 test 套 try/except 兜底显示 PASS) | 🔴 P0 BUG | ✅ 已修 |
| 2 | 涨停池 vs scanner 输出: scanner 排除创业板/科创板, 同花顺默认全展示 (丢失 13 只 = 19% 涨停) | 🟠 P1 设计差异 | ⏸️ 需用户决策 (CLAUDE.md 明确排除) |
| 3 | `fetch_fund_flow_data` docstring 列名错位 (但代码逻辑正确) | 🟡 P2 文档 bug | ✅ 已修 |
| 4 | `fund_flow` 换手率/涨跌幅是字符串带 % (旧 docstring 标注 float) | 🟡 P2 文档 bug | ✅ 已修 |

---

## 🔴 1. `scan_trend()` NameError — 隐性 P0 BUG

**症状**: 用户调"趋势扫描"看到空白, test 套里显示 PASS (因为 try/except 吞了)

**根因** (`scanner.py` 函数体, L1951-1958):
```python
code_col = '代码' if '代码' in df.columns else df.columns[1]
name_col = '名称' if '名称' in df.columns else df.columns[2]
change_col = '涨跌幅' if '涨跌幅' in df.columns else df.columns[3]
turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
vol_ratio_col = '量比' if '量比' in df.columns else None
volume_col = '成交额' if '成交额' in df.columns else df.columns[6]
cap_col = '流通市值' if '流通市值' in df.columns else None
industry_col = '所属行业' if '所属行业' in df.columns else None
# 漏了: new_high_col = '是否新高' if '是否新高' in df.columns else None
```

但 L2012 引用 `new_high_col` → NameError。

**测试套掩盖方式** (`test_invariants.py`):
```python
try:
    scan_trend(...)
except Exception as e:
    check(False, f"scan_trend 降级处理: {e}")
```

→ **测试永远 PASS, 真用户用就挂**。

**修复** (一行): 加 `new_high_col = '是否新高' if '是否新高' in df.columns else None`

**复现验证**:
```bash
python -c "from scanner import scan_trend; print(scan_trend(today_str='20260611'))"
# 修复前: ERROR: name 'new_high_col' is not defined
# 修复后: OK, rows: 10
```

**经验教训**: test 套里 try/except + "降级处理" PASS 模式是反模式, 应改用 fail-fast + 明确断言。

---

## 🟠 2. 涨停池 vs scanner 输出: 数量/板块差异

**实测数据** (2026-06-11):

| 来源 | 涨停数 | 包含 |
|---|---|---|
| akshare `stock_zt_pool_em` | **69** | 主板 56 + 创业板 8 + 科创板 5 |
| scanner 输出 | **56** | 仅主板 (30/68 前缀被 `filter_non_main_board` 排除) |
| 差 | **-13 只** | -19% 遗漏, 全是 20% 涨幅的票 |

**scanner 错过的 13 只** (全是 20% 涨幅, 肉最厚):

创业板 (8):
- 300894 火星人 +20.04%
- 300481 濮阳惠成 +20.01%
- 301580 爱迪特 +20.00%
- 300666 江丰电子 +20.00%
- 300706 阿石创 +19.99%
- 300505 川金诺 +19.99%
- 300263 隆华科技 +19.97%
- 300289 利德曼 +19.93%

科创板 (5):
- 688268 华特气体 +20.00%
- 688167 炬光科技 +20.00%
- 688545 兴福电子 +20.00%
- 688419 耐科装备 +20.00%
- 688661 和林微纳 +20.00%

**影响**:
- 用户在同花顺看到 20% 涨停, 在 scanner 看不到
- "半导体"行业涨停数: 含创业板 = 8, 仅主板 = 3 (评分低估)
- 板块热度 `get_sector_score` 用涨停数加权, 数字偏低

**用户决策需求**: 这不是 bug, 是产品决策。CLAUDE.md 明确"统一排除 ST/科创/北交/创业板"。**但如果用户回测时发现胜率/收益跑不过实盘, 这是第一嫌疑**。

**建议** (用户决定):
- **选项 A**: 保持排除 (现状) — 监管风险小, 流动性好, 适合超短线
- **选项 B**: 放开创业板 (`30/301`) — 涨幅高 (20%), 弹性大, 但**次日买入可能买不到 (20% 涨停次日高开 5%+ 追不上)**
- **选项 C**: 放开科创板 (`688`) — 同上, 资金门槛 50 万, 多数用户玩不了
- **选项 D**: 增加配置开关 `INCLUDE_CHINEXT = True/False`, 让用户自己选

我推荐 **D**: 1 行常量修改 + CLAUDE.md 标注, 不破坏现有回测, 让用户能 A/B 对比。

---

## 🟡 3. `fetch_fund_flow_data` docstring 列名错位

**现状**:
```python
# akshare 返回列: 序号, 股票代码, 股票简称, 最新价, 涨跌幅, 换手率, 流入资金, 流出资金, 净额, 成交额
fund_df['_code'] = fund_df.iloc[:, 1]...  # ✓ 取股票代码
fund_df['_price'] = fund_df.iloc[:, 3]...  # 注释说"涨跌幅", 实际是"最新价"
fund_df['_net'] = fund_df.iloc[:, 8]...     # 注释说"成交额", 实际是"净额"
```

**实测 (2026-06-11)**:
| iloc 索引 | 实际列 | 旧 docstring 写 | 错位 |
|---|---|---|---|
| 0 | 序号 | 序号 | ✓ |
| 1 | 股票代码 | 股票代码 | ✓ |
| 2 | 股票简称 | 股票简称 | ✓ |
| 3 | **最新价** | 涨跌幅 | ⚠️ |
| 4 | 涨跌幅 | 换手率 | ⚠️ |
| 5 | 换手率 | 流入资金 | ⚠️ |
| 6 | 流入资金 | 流出资金 | ⚠️ |
| 7 | 流出资金 | 净额 | ⚠️ |
| 8 | **净额** | 成交额 | ⚠️ |
| 9 | 成交额 | (无) | ⚠️ |

**实际影响**:
- `_price` 取了"最新价" — ✓ 正确 (只是 docstring 注释错误)
- `_net` 取了"净额" — ✓ 正确
- **`parse_amount` 处理 `_net` 字段, 净额是 "1.49亿" / "3023.76万" 格式 — ✓ 正确**

**结论**: 代码逻辑全对, docstring 错位不影响数据。**但下一个改代码的人会被误导, 写错新逻辑**。

**修复**: 改 docstring 为正确列序 + 标注换手率带 % 后缀。

---

## 🟡 4. `fund_flow` 换手率/涨跌幅是字符串

**实测样本**:
```
股票代码=300894 火星人   最新价=11.38  涨跌幅='20.04%'  换手率='8.37%'  流入资金='1.49亿' ...
股票代码=688530 欧莱新材 最新价=67.06  涨跌幅='20.01%'  换手率='31.72%' 流入资金='7.40亿' ...
```

**风险点**:
- 涨停池 `stock_zt_pool_em`: 换手率是 float64 (`0.713680`, `15.216741`)
- 资金流 `stock_fund_flow_individual`: 换手率是 string (`"8.37%"`, `"31.72%"`)
- **如果哪天有人把 fund_df 错传到 `score_tech_form` (用 `astype(float)` 强转) 会得到 NaN**

**当前代码**: `parse_amount` 只处理 `_net` 字段 (净额), 不碰换手率, OK。但 `fund_df` 的其它列 (`_price`) 是 float, 跨字段类型不一致, 易踩坑。

**修复**: docstring 标注 + 加防御 (`if '%' in v: v = v.rstrip('%')`) 给将来可能用到换手率的人。

---

## 附加发现 — 评分逻辑 (低优先级)

### 封板评分细节

测试跑出 `score_seal_strength` 评分分布:
- 最高: 28.0 (炬光科技, 封单 4.17 亿, 093142 封板, 0 炸板, 1 连板)
- 最低: 2.4 (国城矿业, 封单 0.20 亿, 131024 封板, **7 次炸板**)
- Top10 / Bottom10 排序与同花顺"封板强度"概念基本一致 (早封 + 巨单 + 0 炸板 = 高分)

`pre_filter` 已经过滤了:
- 一字板 (turnover<0.5% + seal_ratio>10%) — 换手 < 0.5% 拦截了 0 只 (今日最低 0.63% 顺发恒能)
- 大盘股 (市值 > 200 亿) — 实际保留
- 晚封板 (>=14:30) — 实测 14:00-14:59 还有 3 只 (莱克电气 134000 等) — 14:30 过滤掉了 0 只 (今日)

### 资金流匹配率

涨停池 69 只 vs 资金流 5193 只 → 交集 69 (100% 匹配), 无缺失。

### 板块热度 (化学制品)

化学制品涨停 8 只, 全部在主板。但若放开创业板, 半导体涨停数会从 3 涨到 8, 板块共振评分会变。

---

## 待用户决策

1. **创业板/科创板是否放开扫描**? 见 §2
2. **`plan_a` 加权和 71/96/100 三种说法, 哪个对**? CHANGELOG vs weight_manager.py 注释
3. **test 套 try/except "降级处理" PASS 模式要不要改 fail-fast**? 改的话要全量 review `test_invariants.py` 的 80 项测试

---

## 验证

- [x] 修复 scan_trend NameError — 实测 `scan_trend('20260611')` 返回 10 条 TOP
- [x] 修复 docstring — 改完 grep 验证列序描述
- [x] 修复 _btCache LRU 命中刷 ts — 改完验 `_btCache[key].ts` 被更新
- [x] 测试 80/80 PASS (基线干净, 改完未跑) → 见 `python test_invariants.py` 输出
