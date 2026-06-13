#!/usr/bin/env python3
"""快速移动端布局尺寸检查 (不需 chromium,纯 CSS 解析)
- 验证 mobile 768px / 480px 断点覆盖
- 列出有内联 min-width/font-size 可能挤压的元素
"""
import re, sys
from pathlib import Path

CSS = Path(r'C:\Users\16689\Desktop\stock-scanner\static\style.css')
JS_CARDS = Path(r'C:\Users\16689\Desktop\stock-scanner\static\cards.js')
JS_APP = Path(r'C:\Users\16689\Desktop\stock-scanner\static\app.js')

css = CSS.read_text(encoding='utf-8')

# 1. 收集所有 media query 断点
print("=" * 60)
print("1. 媒体断点")
print("=" * 60)
bps = re.findall(r'@media\s*\(\s*max-width:\s*(\d+)px\s*\)', css)
print(f"  断点列表: {sorted(set(int(b) for b in bps))}px")

# 2. 检查 viewport meta
print()
print("=" * 60)
print("2. viewport meta 检查")
print("=" * 60)
html = Path(r'C:\Users\16689\Desktop\stock-scanner\templates\index.html').read_text(encoding='utf-8')
m = re.search(r'<meta name="viewport" content="([^"]+)"', html)
if m:
    print(f"  viewport: {m.group(1)}")
    has_zoom = 'maximum-scale' in m.group(1) or 'user-scalable' in m.group(1)
    print(f"  防止 iOS 缩放: {'OK' if has_zoom else 'MISSING'}")
else:
    print("  MISSING viewport meta")

# 3. 检查关键 mobile 组件
print()
print("=" * 60)
print("3. 关键 mobile 组件")
print("=" * 60)
checks = [
    ('.mobile-header', '顶部 mobile header'),
    ('.hamburger', 'hamburger 按钮'),
    ('.sidebar-overlay', 'sidebar overlay 遮罩'),
    ('.sidebar.open', 'sidebar.open 显示'),
    ('table-wrap', 'table-wrap 横向滚动'),
    ('.table-wrap', 'table-wrap 类'),
    ('overflow-x: hidden', 'body 横向溢出防护'),
    ('@media (max-width: 768px)', '768px 断点'),
    ('@media (max-width: 480px)', '480px 断点'),
]
for needle, label in checks:
    has = needle in css or needle in html
    print(f"  {'[OK]' if has else '[!!]'} {label} ({needle})")

# 4. cards.js 内联 style 检查 (可能挤压 mobile 的元素)
print()
print("=" * 60)
print("4. cards.js 内联 style 中可能挤压 mobile 的元素")
print("=" * 60)
cards = JS_CARDS.read_text(encoding='utf-8')
# min-width 写死
min_widths = re.findall(r'min-width:\s*(\d+)px', cards)
min_width_counts = {}
for w in min_widths:
    min_width_counts[int(w)] = min_width_counts.get(int(w), 0) + 1
print(f"  min-width 写死统计: {sorted(min_width_counts.items())}")
# 找 >140px 的(在 375px 屏会强制换行)
big = [w for w in min_width_counts if w >= 140]
if big:
    print(f"  ⚠️  min-width >=140px 的有 {len(big)} 个阈值: {big}")
    print(f"     在 375px 屏会强制换行(若 flex 父是 wrap 则 OK, 否则挤压)")

# 5. font-size 写死
font_sizes = re.findall(r'font-size:\s*(\d+)px', cards)
fs_counts = {}
for w in font_sizes:
    fs_counts[int(w)] = fs_counts.get(int(w), 0) + 1
print(f"  font-size 写死统计: {sorted(fs_counts.items())}")
big_fs = [w for w in fs_counts if w >= 18]
if big_fs:
    print(f"  ⚠️  font-size >=18px 的有 {len(big_fs)} 个阈值: {big_fs}")
    print(f"     mobile 偏大, 已用 CSS [style*=font-size:20px] 降级")

# 6. cards.js 是否所有 <table> 都被 .table-wrap 包
print()
print("=" * 60)
print("5. cards.js 表格横滚检查")
print("=" * 60)
# 找所有 '<table' 出现位置
table_positions = [m.start() for m in re.finditer(r'<table\b', cards)]
table_wrap_positions = [m.start() for m in re.finditer(r'class="table-wrap"', cards)]
print(f"  <table> 出现次数: {len(table_positions)}")
print(f"  <table-wrap> 出现次数: {len(table_wrap_positions)}")
# 每个 <table> 前面 200 字符内是否有 .table-wrap
unwrapped = []
for tp in table_positions:
    pre = cards[max(0, tp - 300):tp]
    if 'table-wrap' not in pre:
        unwrapped.append(tp)
if unwrapped:
    print(f"  ⚠️  未包 .table-wrap 的 <table>: {len(unwrapped)} 个")
    for u in unwrapped:
        # 显示上下文
        ctx = cards[max(0, u - 80):u + 60].replace('\n', ' ')
        print(f"     @{u}: ...{ctx}...")
else:
    print("  [OK] 所有 <table> 都用 .table-wrap 包了")

# 7. allTrades 表格 min-width 检查
print()
print("=" * 60)
print("6. allTrades 表格 min-width (移动端需要) ")
print("=" * 60)
alltrades_idx = cards.find('allTrades.forEach')
if alltrades_idx > 0:
    after = cards[alltrades_idx:alltrades_idx + 800]
    has_min = 'min-width:' in after
    print(f"  allTrades 表格 {'有 min-width' if has_min else '无 min-width (mobile 会挤压)'}")
