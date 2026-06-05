#!/usr/bin/env python3
"""全模块回测验证 — 批量拉取历史数据，逐一验证胜率"""
import sys, warnings, time
warnings.filterwarnings('ignore')

import pandas as pd, numpy as np
from datetime import datetime, timedelta
from cache import _is_trading_day
from scanner import filter_non_main_board, score_seal_strength, score_tech_form, get_sector_score
import akshare as ak
from archiver import batch_fetch_history

cst = __import__('datetime').timezone(timedelta(hours=8))
dates = []
d = datetime.now(cst)
for _ in range(90):
    ds = d.strftime('%Y%m%d')
    if _is_trading_day(ds) and d.weekday() < 5:
        dates.append(ds)
        if len(dates) >= 16: break
    d -= timedelta(days=1)
dates.reverse()

# Step 1: Collect all unique stock codes
print("Step 1: Collecting stock codes...")
all_codes = set()
for sd in dates:
    try:
        pool = ak.stock_zt_pool_em(date=sd)
        if pool is not None and not pool.empty:
            cc = '代码' if '代码' in pool.columns else pool.columns[1]
            all_codes.update(str(c).strip().zfill(6) for c in pool[cc])
    except: pass
    try:
        prev = ak.stock_zt_pool_previous_em(date=sd)
        if prev is not None and not prev.empty:
            cc = prev.columns[1]
            all_codes.update(str(c).strip().zfill(6) for c in prev[cc])
    except: pass
    try:
        zb = ak.stock_zt_pool_zbgc_em(date=sd)
        if zb is not None and not zb.empty:
            cc = zb.columns[1]
            all_codes.update(str(c).strip().zfill(6) for c in zb[cc])
    except: pass

codes = sorted(all_codes)
print(f"  {len(codes)} unique codes to fetch")

# Step 2: Batch fetch with cache
print("Step 2: Fetching history (cached)...")
sys.stderr = open('NUL', 'w')
t0 = time.time()
hist = batch_fetch_history(codes, dates[0], dates[-1], max_workers=8)
sys.stderr = sys.__stderr__
print(f"  Done in {time.time()-t0:.0f}s, got {len(hist)} codes")

# Step 3: Validate each module
results = {}

# ---- Limit-up Scan ----
print("\n=== Limit-up Scan ===")
lu = []
for i in range(len(dates)-1):
    sd, vd = dates[i], dates[i+1]
    try:
        pool = ak.stock_zt_pool_em(date=sd)
        if pool is None or pool.empty: continue
        df = filter_non_main_board(pool)
        if df.empty: continue
        seal_s = score_seal_strength(df)
        tech_s = score_tech_form(df)
        sec_s = get_sector_score(df)
        for idx in df.index:
            code = str(df.loc[idx, df.columns[1]]).strip().zfill(6)
            nxt = (hist.get(code) or {}).get(vd)
            if nxt is None or np.isnan(nxt): continue
            s = seal_s[idx]*22/28 + tech_s[idx]*6/10 + sec_s[idx]*12/12 + 10*12/20 + 5*4/6 + 5*9/10 + 5*6/10
            lu.append({'sc': s/71*100, 'nxt': nxt})
    except: continue

rdf = pd.DataFrame(lu)
rdf['g'] = pd.qcut(rdf['sc'], 5, labels=['E','D','C','B','A'], duplicates='drop')
c1 = rdf['sc'].corr(rdf['nxt']); a1 = rdf[rdf['g']=='A']['nxt'].mean(); e1 = rdf[rdf['g']=='E']['nxt'].mean()
print(f"  N={len(rdf)} corr={c1:+.3f} A={a1:+.1f}% E={e1:+.1f}% spread={a1-e1:+.1f}%")
results['涨停扫描'] = (len(rdf), c1, a1, e1, a1-e1)

# ---- Reversal Scan ----
print("\n=== Reversal Scan ===")
rv = []
for i in range(len(dates)-1):
    sd, vd = dates[i], dates[i+1]
    try:
        prev = ak.stock_zt_pool_previous_em(date=sd)
        if prev is None or prev.empty: continue
        df = filter_non_main_board(prev)
        if df.empty: continue
        cc = df.columns[1]; tc = df.columns[9]; sc = df.columns[14] if len(df.columns)>14 else None
        df['chg'] = df[df.columns[3]].astype(float)
        pb = df[(df['chg']>=-7)&(df['chg']<=1)]
        for _, row in pb.iterrows():
            code = str(row[cc]).strip().zfill(6)
            to = float(row[tc]) if pd.notna(row[tc]) else 0
            chg = round(float(row['chg']),1)
            raw = str(row[sc]) if sc and pd.notna(row[sc]) else ''
            lb = int(raw.split('/')[1]) if '/' in raw else 0
            nxt = (hist.get(code) or {}).get(vd)
            if nxt is None or np.isnan(nxt): continue
            s = (40 if to>25 else 32 if to>=15 else 20 if to>=8 else 14 if to>=5 else 8 if to>=3 else 4 if to>=1 else 0) + \
                (35 if lb==3 else 28 if lb==2 else 15 if lb>=4 else 10 if lb==1 else 8) + \
                (15 if -3<=chg<=0.5 else 12 if -5<=chg<-3 else 8) + 10
            rv.append({'sc': s, 'nxt': nxt})
    except: continue

rdf2 = pd.DataFrame(rv)
rdf2['g'] = pd.qcut(rdf2['sc'], 5, labels=['E','D','C','B','A'], duplicates='drop')
c2 = rdf2['sc'].corr(rdf2['nxt']); a2 = rdf2[rdf2['g']=='A']['nxt'].mean(); e2 = rdf2[rdf2['g']=='E']['nxt'].mean()
print(f"  N={len(rdf2)} corr={c2:+.3f} A={a2:+.1f}% E={e2:+.1f}% spread={a2-e2:+.1f}%")
results['反转扫描'] = (len(rdf2), c2, a2, e2, a2-e2)

# ---- Trend Scan ----
print("\n=== Trend Scan ===")
tr = []
for i in range(len(dates)-1):
    sd, vd = dates[i], dates[i+1]
    try:
        prev = ak.stock_zt_pool_previous_em(date=sd)
        if prev is None or prev.empty: continue
        df = filter_non_main_board(prev)
        if df.empty: continue
        cc = df.columns[1]; tc = df.columns[9]
        df['chg'] = df[df.columns[3]].astype(float)
        trend = df[(df['chg']>=2)&(df['chg']<9)]
        for _, row in trend.iterrows():
            code = str(row[cc]).strip().zfill(6)
            chg = round(float(row['chg']),1)
            to = float(row[tc]) if pd.notna(row[tc]) else 0
            nxt = (hist.get(code) or {}).get(vd)
            if nxt is None or np.isnan(nxt): continue
            s = chg * 2.5 + min(to, 25) * 0.4 + (5 if 5<=to<=15 else 2)
            tr.append({'sc': s, 'nxt': nxt})
    except: continue

rdf3 = pd.DataFrame(tr)
rdf3['g'] = pd.qcut(rdf3['sc'], 5, labels=['E','D','C','B','A'], duplicates='drop')
c3 = rdf3['sc'].corr(rdf3['nxt']); a3 = rdf3[rdf3['g']=='A']['nxt'].mean(); e3 = rdf3[rdf3['g']=='E']['nxt'].mean()
print(f"  N={len(rdf3)} corr={c3:+.3f} A={a3:+.1f}% E={e3:+.1f}% spread={a3-e3:+.1f}%")
results['趋势扫描'] = (len(rdf3), c3, a3, e3, a3-e3)

# ---- Zhaban Scan ----
print("\n=== Zhaban Scan ===")
zb = []
for i in range(len(dates)-1):
    sd, vd = dates[i], dates[i+1]
    try:
        zbdf = ak.stock_zt_pool_zbgc_em(date=sd)
        if zbdf is None or zbdf.empty: continue
        df = filter_non_main_board(zbdf)
        if df.empty: continue
        cc = df.columns[1]; tc = df.columns[9] if len(df.columns)>9 else None
        for _, row in df.iterrows():
            code = str(row[cc]).strip().zfill(6)
            to = float(row[tc]) if tc and pd.notna(row[tc]) else 0
            nxt = (hist.get(code) or {}).get(vd)
            if nxt is None or np.isnan(nxt): continue
            s = 15 if 5<=to<=15 else (10 if 3<=to<5 or 15<to<=20 else 5)
            zb.append({'sc': s, 'nxt': nxt})
    except: continue

rdf4 = pd.DataFrame(zb)
if len(rdf4) > 50:
    rdf4['g'] = pd.qcut(rdf4['sc'], 5, labels=['E','D','C','B','A'], duplicates='drop')
    c4 = rdf4['sc'].corr(rdf4['nxt']); a4 = rdf4[rdf4['g']=='A']['nxt'].mean(); e4 = rdf4[rdf4['g']=='E']['nxt'].mean()
    print(f"  N={len(rdf4)} corr={c4:+.3f} A={a4:+.1f}% E={e4:+.1f}% spread={a4-e4:+.1f}%")
    results['炸板分析'] = (len(rdf4), c4, a4, e4, a4-e4)
else:
    print(f"  N={len(rdf4)} insufficient")

# ═══ Summary ═══
print(f"\n{'='*70}")
print(f"  ALL MODULES VALIDATION ({len(dates)-1} trading days, {len(hist)} stocks)")
print(f"{'='*70}")
print(f"{'Module':<12s} {'N':>6s} {'Corr':>8s} {'A_avg':>8s} {'E_avg':>8s} {'Spread':>7s} {'Verdict':>8s}")
print('-'*65)
for name, (n, c, a, e, sp) in results.items():
    v = 'VALID' if sp > 1 else ('WEAK' if sp > 0 else 'BROKEN')
    print(f"{name:<12s} {n:6d} {c:+7.3f} {a:+7.1f}% {e:+7.1f}% {sp:+6.1f}% {v:>8s}")
