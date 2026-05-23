# UI 美化设计方案 — Nexus Dark

## 目标
美化 A 股超短线选股扫描器 Web 界面，提升视觉品质和交互体验。

## 改动范围

### 1. Dashboard 市场概览
- 单行 flex 栏 → 独立统计卡片组（毛玻璃效果）
- 每张卡片：图标 + 大数字 + 标签
- 情绪卡片：glow 边框按级别着色（高潮红/活跃黄/正常绿/低迷灰/冰点蓝）
- 热门板块标签：圆角药丸样式 + hover 高亮

### 2. 动画系统
- 数字滚动：JS `requestAnimationFrame` 从 0 递增到实际值
- 卡片入场：CSS `@keyframes fadeInUp` + stagger `animation-delay`
- 评分条：`transition: width 0.8s cubic-bezier` 弹性展开
- 页面切换：`.module-page` fadeIn 0.25s
- 评分圆环：总分用 SVG circle stroke-dashoffset 替代纯数字
- Hover 微交互：`transform: translateY(-2px)` + shadow 浮起
- 按钮点击：CSS ripple 效果

### 3. 视觉增强
- 侧边栏：激活项发光渐变边框，hover 轻微右移
- 自定义滚动条（暗色细条 6px）
- 毛玻璃面板：主卡片区 `backdrop-filter: blur(12px)`
- 评分条渐变色（0-100 从红到黄到绿）
- 情绪横幅毛玻璃效果

### 4. 技术方案
- 纯 CSS + 原生 JS，零外部依赖
- `@keyframes` + `transition` 驱动动画
- `requestAnimationFrame` 驱动数字滚动
- `IntersectionObserver` / 时间触发卡片入场

### 5. 文件改动
| 文件 | 改动 |
|------|------|
| `static/style.css` | 重写 Dashboard 样式 + 添加全部动画/玻璃/滚动条/卡片效果 |
| `templates/index.html` | 更新 `loadDashboard()` HTML 结构 + 添加数字滚动 JS + 评分圆环 JS |

### 6. 成功标准
- 页面加载后 Dashboard 数字平滑滚动到目标值
- 切换模块时内容淡入
- 扫描完成后卡片逐张滑入
- 所有 hover 交互有反馈
- 暗色主题统一、不刺眼
