#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据准确性核查脚本 - 对比 akshare 与同花顺逻辑"""
import sys
import pandas as pd
pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 220)
pd.set_option('display.unicode.east_asian_width', True)
pd.set_option('display.precision', 3)

sys.path.insert(0, '.')
import akshare as ak
from scanner import score_seal_strength, get_money_flow_scores

# ═══════ 1. 涨停池字段 ═══════
print('='*100)
print('  1. 涨停池字段 (ak.stock_zt_pool_em)')
print('='*100)
df = ak.stock_zt_pool_em(date='20260611')
print('总行数:', len(df))
print('列:', list(df.columns))
print()

# 涨跌幅范围
print('--- 涨跌幅分布 ---')
print('min:', df['涨跌幅'].min(), 'max:', df['涨跌幅'].max(), 'mean:', df['涨跌幅'].mean())
print('涨幅 <9.5 的:')
print(df[df['涨跌幅'] < 9.5][['代码', '名称', '涨跌幅', '最新价']].to_string())
print('涨幅 >= 19.5 的 (创业板/科创板):')
print(df[df['涨跌幅'] >= 19.5][['代码', '名称', '涨跌幅', '最新价']].to_string())
print()

# 封单资金
print('--- 封单资金 (亿) ---')
fund_yi = df['封板资金'] / 1e8
print('min:', fund_yi.min(), 'max:', fund_yi.max(), 'sum:', fund_yi.sum())
print('封单为0 的:')
print(df[df['封板资金'] == 0][['代码', '名称', '涨跌幅']].to_string())
print()

# 换手率
print('--- 换手率 ---')
print('min:', df['换手率'].min(), 'max:', df['换手率'].max(), 'mean:', df['换手率'].mean())
print('换手率 > 50% 的:')
print(df[df['换手率'] > 50][['代码', '名称', '换手率', '涨跌幅']].to_string())
print('换手率 < 1% 的:')
print(df[df['换手率'] < 1][['代码', '名称', '换手率', '涨跌幅', '首次封板时间', '最后封板时间']].to_string())
print()

# ═══════ 2. 封单时间分布 ═══════
print('='*100)
print('  2. 首次封板时间分布')
print('='*100)
df['_first_hour'] = df['首次封板时间'].astype(str).str[:2]
print(df.groupby('_first_hour').size())
print()

# ═══════ 3. 跑 score_seal_strength 验证逻辑 ═══════
print('='*100)
print('  3. score_seal_strength 实跑结果')
print('='*100)
df['_seal_score'] = score_seal_strength(df)
print('评分分布:')
print(df['_seal_score'].describe())
print()
print('评分 Top10:')
print(df.nlargest(10, '_seal_score')[['代码', '名称', '涨跌幅', '换手率', '封板资金', '首次封板时间', '炸板次数', '连板数', '_seal_score']].to_string())
print()
print('评分 Bottom10:')
print(df.nsmallest(10, '_seal_score')[['代码', '名称', '涨跌幅', '换手率', '封板资金', '首次封板时间', '炸板次数', '连板数', '_seal_score']].to_string())
print()

# ═══════ 4. 资金流数据列名核对 ═══════
print('='*100)
print('  4. 资金流数据 (ak.stock_fund_flow_individual) 列名核对')
print('='*100)
fund_df = ak.stock_fund_flow_individual()
print('总行数:', len(fund_df))
print('列:', list(fund_df.columns))
print()
# 涨停池取一只票, 看资金流匹配
test_codes = df['代码'].astype(str).str.zfill(6).head(3).tolist()
for c in test_codes:
    row = fund_df[fund_df['股票代码'].astype(str).str.zfill(6) == c]
    if not row.empty:
        r = row.iloc[0]
        print(f'  {c}: 最新价={r["最新价"]}, 涨跌幅={r["涨跌幅"]}, 换手率={r["换手率"]}, 流入={r["流入资金"]}, 流出={r["流出资金"]}, 净额={r["净额"]}, 成交额={r["成交额"]}')
    else:
        print(f'  {c}: 资金流中无此代码')
print()

# ═══════ 5. 资金流匹配率 ═══════
print('='*100)
print('  5. 资金流匹配率 (涨停池 vs 资金流)')
print('='*100)
zt_codes = set(df['代码'].astype(str).str.zfill(6))
fund_codes = set(fund_df['股票代码'].astype(str).str.zfill(6))
matched = zt_codes & fund_codes
print(f'涨停池: {len(zt_codes)}, 资金流: {len(fund_codes)}, 交集: {len(matched)}')
miss_zt = zt_codes - fund_codes
print(f'涨停池中资金流缺失: {len(miss_zt)}')
if miss_zt:
    miss_df = df[df['代码'].astype(str).str.zfill(6).isin(miss_zt)]
    print(miss_df[['代码', '名称']].head(10).to_string())
print()
