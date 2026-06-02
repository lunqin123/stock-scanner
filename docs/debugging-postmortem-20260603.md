# 回测面板"暂无数据"调试复盘

**日期**: 2026-06-03 | **耗时**: ~2小时 | **最终修复**: 1行代码

## 一、问题表象

回测追踪面板点击后始终显示"暂无数据"，无论重启服务器还是强制刷新浏览器都不生效。

## 二、错误诊断链（为什么绕了这么远）

### 第1步：以为是缓存没迁移 ✓（但不是根因）

卡片端点从 `cache_put` 迁移到 `daily_set`，旧 `.pkl` 文件和新 `.json` 文件路径不同 → 缓存读不到。**修了，不是根因。**

### 第2步：以为是 `loadCardView` 的 items 检查 ✓（但不是根因）

`loadCardView` 里 `items = data.stocks || data.items || []` 对回测端点永远是空数组 → 触发 `!items.length` → "暂无数据"。加了 `backtest` 例外跳过检查。**修了，不是根因。**

### 第3步：以为是服务器没重启

每次改完代码推送后用户说"还是一样"，反复怀疑 webhook 没触发、git pull 失败、`systemctl restart` 没执行。实际服务器确实重启了多次。

### 第4步：以为是浏览器缓存

加了 `Cache-Control: no-cache`、改了版本号 `?v=1.15.0→1.17.5`、让用户清缓存硬刷新开无痕——仍然"暂无数据"。

### 第5步：以为是 `renderBacktestDashboard` 不存在

怀疑 `cards.js` 没有部署到服务器，或者函数有 JS 错误。SSH 上去 `grep` 确认代码在磁盘上，端点返回正确 JSON。

### 第6步（真正根因）：**路由走错了函数**

回到第一性原理——逐行追踪 `callApi()` 的路由逻辑：

```javascript
// app.js:272-283
if (isStream) {
    await loadCardViewStream(...);     // SSE 流
} else if (info.textApi) {
    await loadCardView(...);          // 卡片 JSON ← 所有修复都在这里
} else if (info.streamApi) {
    await loadTextViewStream(...);
} else {
    await loadTextView(...);          // ← backtest 走到了这里！
}
```

`PAGES.backtest` **没有 `textApi` 字段**：

```javascript
// 错误配置
'backtest': { title: '⏱️ 回测追踪', api: '/api/backtest/dashboard' }

// 正确配置
'backtest': { title: '⏱️ 回测追踪', api: '/api/backtest/dashboard', textApi: '/api/backtest' }
```

没有 `textApi` → 不进 `loadCardView` → 我的所有修复（backtest 例外、renderBacktestDashboard）永远不会被执行。

实际走的是 `loadTextView` → 期待 `data.output`（文本）→ 端点返回 `data.weights`（卡片）→ `renderStyledText(undefined)` → 什么都不显示（看起来像"暂无数据"）。

## 三、为什么一开始找不到根因

| 错误假设 | 真实情况 |
|----------|----------|
| "暂无数据"是从 loadCardView 的 `!items.length` 来的 | 实际是从 loadTextView 的路径来的，根本不进 loadCardView |
| textApi 是可选的展示字段 | textApi 是路由分发的关键开关，缺了会走到完全不同的渲染器 |
| 所有 tab 的渲染路径都一样 | 每个 tab 根据 PAGES 配置走四种不同渲染路径之一 |
| 看到"暂无数据"就是数据问题 | "暂无数据"是通用 fallback，多个函数里都有，不能定位 |

**核心教训：从入口函数开始逐行 trace，不要跳到中间函数。**

## 四、调试方法论改进

1. **先确认代码路径，再修代码内容。** 看到 bug 时第一反应应该是"这段代码被执行了吗？"而不是"这段代码逻辑有误"。

2. **"暂无数据"是烟雾弹。** 系统里有至少 4 个地方输出这个字符串（`loadCardView`、`loadCardViewStream`、`loadTextView`、`loadTextViewStream`），不能假设它来自哪个函数。

3. **PAGES 配置是隐式路由表。** `textApi`/`streamApi` 不是可选的展示字段，而是决定函数分发的关键开关。新增 tab 时必须检查这四个字段。

4. **浏览器缓存会让问题定位延迟一轮。** 每次改 JS 都需要改版本号 `?v=x` 才能确认用户跑的是新代码。应该把版本号自动化和代码提交绑定。

5. **SSH 上去直接 curl 端点 + grep 源码。** 比反复问用户"刷新了吗"有效得多。

## 五、代码层面的预防措施

```javascript
// PAGES 配置检查清单（新增 tab 时必须全部填写）：
{
    api: '...',        // 必填：默认 API 端点
    textApi: '...',    // 路由到 loadCardView 的标记（缺了会走 loadTextView）
    streamApi: '...',  // 路由到 loadTextViewStream 的标记（SSE 文本流）
    title: '...',      // 显示名称
}
```

已经在 `CLAUDE.md` 中记录了这条规则。
