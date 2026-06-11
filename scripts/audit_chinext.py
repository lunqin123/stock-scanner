#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据准确性核查: 创业板/科创板 vs 同花顺"""
import sys
import pandas as pd
pd.set_option('display.unicode.east_asian_width', True)
pd.set_option('display.width', 220)

import akshare as ak

# 涨停池
df_zt = ak.stock_zt_pool_em(date='20260611')
print('=== 涨停池总数:', len(df_zt), '===')
print()

# 按代码前缀分
def prefix_type(code):
    code = str(code).zfill(6)
    if code.startswith(('60', '00', '002')): return '主板(沪+深+中小)'
    if code.startswith(('30', '301')):      return '创业板'
    if code.startswith('68'):                return '科创板'
    if code.startswith(('8', '92', '94')):   return '北交所'
    return '其它'

df_zt['_type'] = df_zt['代码'].apply(prefix_type)
print(df_zt['_type'].value_counts())
print()
print('=== 创业板 (300/301) 涨停 ===')
cy = df_zt[df_zt['_type'] == '创业板'].sort_values('涨跌幅', ascending=False)
print(f'共 {len(cy)} 只')
print(cy[['代码', '名称', '涨跌幅', '换手率', '封板资金', '连板数', '所属行业']].to_string())
print()

print('=== 科创板 (688) 涨停 ===')
kc = df_zt[df_zt['_type'] == '科创板'].sort_values('涨跌幅', ascending=False)
print(f'共 {len(kc)} 只')
print(kc[['代码', '名称', '涨跌幅', '换手率', '封板资金', '连板数', '所属行业']].to_string())
print()

# 主板
print('=== 主板涨停 ===')
zb = df_zt[df_zt['_type'].str.startswith('主板')]
print(f'共 {len(zb)} 只 (这是 scanner 当前展示的)')

# 关键: 涨幅对比
print()
print('=== 涨幅分布 (按板块类型) ===')
for t in df_zt['_type'].unique():
    sub = df_zt[df_zt['_type'] == t]
    print(f'  {t}: 涨幅 {sub["涨跌幅"].min():.2f} ~ {sub["涨跌幅"].max():.2f} (mean {sub["涨跌幅"].mean():.2f})')
print()

# 行业分布
print('=== 涨停股所属行业分布 (主板 only) ===')
print(zb['所属行业'].value_counts().head(10))
print()
print('=== 涨停股所属行业分布 (全部含创业板) ===')
print(df_zt['所属行业'].value_counts().head(10))
print()
