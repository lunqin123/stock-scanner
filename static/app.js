/* ═══════════════════════════════════════
   选股扫描器 — 主应用逻辑（性能优化版）
   ═══════════════════════════════════════ */

// ─── DOM 引用缓存（避免重复查询） ───
const $ = (id) => document.getElementById(id);
const _dom = {
    output: () => $('output'),
    progress: () => $('progress-bar'),
    fill: () => $('progress-fill'),
    txt: () => $('progress-text'),
    pageTitle: () => $('page-title'),
    modulePage: () => document.querySelector('.module-page'),
};

// P6: 当前选中的回测 tab + 调权参数 (localStorage 持久化)
let _btTab = localStorage.getItem('btTab') || 'trend';  // 默认趋势(52.2%胜率)
// 各 tab 推荐 TOP-N (用户1把梭3万, 全 tab 统一 TOP1 + 3万本金)
const _tabDefaultTopN = { 'limit-up': 1, 'zhaban': 1, 'trend': 1, 'dtqiaoban': 1, 'reversal': 1 };
let _btTopN = parseInt(localStorage.getItem('btTopN')) || _tabDefaultTopN[_btTab] || 1;
let _btCapital = parseInt(localStorage.getItem('btCapital')) || (_btTopN * 30000);
let _btStrategy = localStorage.getItem('btStrategy') || '';
// 回测天数（服务端归档最多到109天，默认60天不过载）
const _BT_DAYS = 20;  // archive.db 有20天数据，全量覆盖
// v3.3h: 各tab策略参数 — 严格对齐后端 _TAB_BUY_TIME + TAB_DEFAULT_BT_PARAMS
//   close策略(limit-up/zhaban): T日尾盘买→T+1开盘卖, sell_n参数无效(恒为1)
//   open策略(trend/reversal/dtqiaoban): T+1开盘买→T+N开盘卖, sell_n有效
var _TAB_BUY_MODE = {
    'limit-up': 'close', 'zhaban': 'open',    // v3.3i: 等次日确认不接飞刀
    'trend': 'open', 'reversal': 'open', 'dtqiaoban': 'open',
};
var _TAB_DEFAULT_SELL_N = {
    'trend': 1, 'limit-up': 3, 'zhaban': 1, 'reversal': 1, 'dtqiaoban': 1,  // v3.3n: 优化结果
};
var _TAB_DEFAULT_MIN_SCORE = {
    'trend': 50, 'limit-up': 50, 'zhaban': 70, 'reversal': 50, 'dtqiaoban': 50,  // v3.3i: 70分过滤
};
var _btSellNs = (function() {
    var stored;
    try { stored = JSON.parse(localStorage.getItem('btSellNs') || '{}'); } catch(e) { stored = {}; }
    for (var k in _TAB_DEFAULT_SELL_N) {
        if (stored[k] === undefined) stored[k] = _TAB_DEFAULT_SELL_N[k];
    }
    return stored;
})();
// v3.3h: close策略tab(limit-up/zhaban) sell_n恒为1, open策略用真实T+N
(function() {
    var sellNsVer = localStorage.getItem('btSellNs_v10');
    if (sellNsVer !== 'v10') {
        _btSellNs = {};
        for (var k in _TAB_DEFAULT_SELL_N) { _btSellNs[k] = _TAB_DEFAULT_SELL_N[k]; }
        localStorage.setItem('btSellNs', JSON.stringify(_btSellNs));
        localStorage.setItem('btSellNs_v10', 'v10');
        _btMinScores = {};
        for (var k2 in _TAB_DEFAULT_MIN_SCORE) { _btMinScores[k2] = _TAB_DEFAULT_MIN_SCORE[k2]; }
        localStorage.setItem('btMinScores', JSON.stringify(_btMinScores));
    }
})();
function _getSellN(tab) { return _btSellNs[tab] !== undefined ? _btSellNs[tab] : (_TAB_DEFAULT_SELL_N[tab] || 3); }
function _setSellN(tab, val) { _btSellNs[tab] = val; localStorage.setItem('btSellNs', JSON.stringify(_btSellNs)); }

var _btMinScores = (function() {
    var stored;
    try { stored = JSON.parse(localStorage.getItem('btMinScores') || '{}'); } catch(e) { stored = {}; }
    // 用推荐默认值填充尚未设置的 tab
    for (var k in _TAB_DEFAULT_MIN_SCORE) {
        if (stored[k] === undefined) stored[k] = _TAB_DEFAULT_MIN_SCORE[k];
    }
    return stored;
})();
function _getMinScore(tab) { return _btMinScores[tab] !== undefined ? _btMinScores[tab] : 0; }
function _setMinScore(tab, val) { _btMinScores[tab] = val; localStorage.setItem('btMinScores', JSON.stringify(_btMinScores)); }

function _saveBacktestParams() {
    localStorage.setItem('btTab', _btTab);
    localStorage.setItem('btTopN', _btTopN);
    localStorage.setItem('btCapital', _btCapital);
    localStorage.setItem('btStrategy', _btStrategy);
    // btMinScores 已通过 _setMinScore 持久化到 btMinScores
}
// 重置回默认 (1把梭3万, 全部 TOP1)
function _resetBacktestParams() {
    if (!confirm('重置回测参数为默认值?\n(全部 tab 用 TOP1 + 3万本金, 切回默认趋势 tab)')) return;
    localStorage.removeItem('btTab');
    localStorage.removeItem('btTopN');
    localStorage.removeItem('btCapital');
    localStorage.removeItem('btStrategy');
    localStorage.removeItem('btMinScores');
    localStorage.removeItem('btSellNs');
    _btStrategy = '';
    _btCache = {};
    location.reload();
}
window._resetBacktestParams = _resetBacktestParams;

// P6: 前端缓存层 — 按 (tab, days, top_n, capital) 缓存 data 对象,切回秒显示
// 关键修复:
//   1. 缓存 data 而不是 html(避免以后加 generated_at 等动态字段时缓存陈旧)
//   2. _btLoadToken 防竞态:慢请求完成后丢弃(避免切tab时旧请求覆盖新loading)
var _btCache = {};  // v3.3e: unused, nocache
var _btLoadToken = 0;

function _btCacheKey(tab, days, topN, capital, strategy, minScore, sellN) {
    return tab + '_' + days + '_' + topN + '_' + capital + '_' + (strategy || '') + '_' + (minScore || 0) + '_' + (sellN || 3);
}

async function loadBacktestTab(tab, days, topN, capital) {
    _btTab = tab; _btTopN = topN; _btCapital = capital;
    _saveBacktestParams();
    var contentEl = document.getElementById('btTabContent');
    if (!contentEl) return;
    contentEl.innerHTML = '<div class="loading">⏳ 加载中...</div>';

    _btLoadToken++;
    var myToken = _btLoadToken;
    try {
        var ctrl = new AbortController();
        var tid = setTimeout(function() { ctrl.abort(); }, 60000);
        var url = '/api/bt/' + tab + '/full?days=' + days + '&top_n=' + topN + '&min_score=' + _getMinScore(tab) + '&sell_n=' + _getSellN(tab) + '&capital=' + capital + '&force=true';
        if (_btStrategy) url += '&strategy=' + encodeURIComponent(_btStrategy);
        var resp = await fetch(url, { signal: ctrl.signal, cache: 'no-store' });
        clearTimeout(tid);
        var data = await resp.json();
        if (myToken !== _btLoadToken) return;
        if (!data) {
            contentEl.innerHTML = '<div class="error-text">回测加载失败 - 服务端返回空 (请刷新重试)</div>';
            return;
        }
        contentEl.innerHTML = renderBacktestTabFull(data);
    } catch (e) {
        if (myToken !== _btLoadToken) {
            // token 变了, 不报错(让新请求主导), 但至少 console 一下
            console.warn('[回测] 过期错误被丢弃:', tab, e.message);
            return;
        }
        var msg = e.name === 'AbortError' ? '请求超时 (60s)' : e.message;
        console.error('[回测] 加载失败:', tab, 'msg=', msg, e);
        contentEl.innerHTML = '<div class="error-text">❌ 加载失败: ' + msg + '</div>';
    }
}

// 后台预加载所有回测 tab 的数据（当前 tab 跳过，其他静默缓存）
function _prefetchBacktestTabs(currentTab, days, topN, capital) {
    var allTabs = ['trend', 'limit-up', 'zhaban', 'reversal', 'dtqiaoban'];
    allTabs.forEach(function(t) {
        if (t === currentTab) return;
        var key = _btCacheKey(t, days, topN, capital, _btStrategy, _getMinScore(t), _getSellN(t));
        if (_btCache[key]) return;
        var url = '/api/bt/' + t + '/full?days=' + days + '&top_n=' + topN + '&min_score=' + _getMinScore(t) + '&sell_n=' + _getSellN(t) + '&capital=' + capital;
        if (_btStrategy) url += '&strategy=' + encodeURIComponent(_btStrategy);
        fetch(url).then(function(r) { return r.json(); }).then(function(d) {
            if (d && typeof d === 'object' && d.ok) {
                _btCache[key] = { data: d, ts: Date.now() };
            }
        }).catch(function() {});
    });
}

// P6: 切换回测 tab
async function switchBacktestTab(tab, days) {
    _btTab = tab;
    _saveBacktestParams();
    var topSel = document.getElementById('btTopN');
    var capInput = document.getElementById('btCapital');
    var msInput = document.getElementById('btMinScore');
    if (topSel) topSel.value = _btTopN;
    if (capInput) capInput.value = _btCapital;
    if (msInput) msInput.value = _getMinScore(tab);
    // 更新 tab 按钮高亮
    var bar = document.getElementById('btTabBar');
    if (bar) {
        bar.querySelectorAll('button').forEach(function(btn) {
            var isActive = btn.textContent.trim() === ({'trend':'趋势','limit-up':'涨停','zhaban':'炸板','reversal':'反转','dtqiaoban':'跌停'})[tab];
            btn.style.background = isActive ? 'var(--accent)' : 'var(--bg-secondary)';
            btn.style.color = isActive ? '#fff' : 'var(--text)';
        });
    }
    await loadBacktestTab(tab, days, _btTopN, _btCapital);
}

// TOP-N / 本金 切换
// 注:days 固定 30(回测页 UI 没有 days 选择器,见 v2 任务清单决策记录)
function onBacktestParamChange() {
    var topSel = document.getElementById('btTopN');
    var capInput = document.getElementById('btCapital');
    var newTopN = parseInt(topSel ? topSel.value : 3) || 3;
    var newCap = parseInt(capInput ? capInput.value : 90000) || 90000;

    // 如果只改了本金（TOP-N 没变），直接从缓存重算盈亏，不调 API
    if (newTopN === _btTopN && newCap !== _btCapital) {
        var oldKey = _btCacheKey(_btTab, _BT_DAYS, _btTopN, _btCapital, _btStrategy, _getMinScore(_btTab), _getSellN(_btTab));
        var cached = _btCache[oldKey];
        if (cached && cached.data && cached.data.ok) {
            var newData = JSON.parse(JSON.stringify(cached.data)); // 深拷贝
            // 重算每条交易的 pnl
            var bt = newData.backtest;
            if (bt) {
                var cmp = bt.comparison || {};
                ['open_buy'].forEach(function(k) {
                    var group = cmp[k];
                    if (group && group.trades) {
                        group.trades.forEach(function(t) {
                            var ret = parseFloat(t.net_ret_pct) || 0;
                            t.pnl = Math.round(newCap * ret / 100);
                        });
                    }
                });
                // 重算 summary
                ['summary', 'summary_30d'].forEach(function(sk) {
                    var s = bt[sk];
                    if (s && s.trade_count) {
                        var totalPnl = 0;
                        var src = cmp.open_buy && cmp.open_buy.trades ? cmp.open_buy.trades : [];
                        src.forEach(function(t) { totalPnl += (t.pnl || 0); });
                        s.total_pnl = totalPnl;
                    }
                });
                bt.config.capital = newCap;
            }
            // 存入新缓存键
            _btCapital = newCap;
            _saveBacktestParams();
            var newKey = _btCacheKey(_btTab, _BT_DAYS, _btTopN, _btCapital, _btStrategy, _getMinScore(_btTab), _getSellN(_btTab));
            _btCache[newKey] = { data: newData, ts: Date.now() };
            var el = document.getElementById('btTabContent');
            if (el) el.innerHTML = renderBacktestTabFull(newData);
            return;
        }
    }

    _btTopN = newTopN;
    _btCapital = newCap;
    _saveBacktestParams();
    loadBacktestTab(_btTab, _BT_DAYS, _btTopN, _btCapital);
}

// 2026-07-05: 删除 strategy preset, 该函数已废 (UI 元素不再存在)
// 保留空函数以防外部模板调用, 不实际生效
function onBacktestStrategyChange() { /* no-op */ }

function onBacktestMinScoreInput() {
    var inp = document.getElementById('btMinScore');
    var val = parseInt(inp ? inp.value : 0) || 0;
    var scoreEl = document.getElementById('tab-score-display');
    if (scoreEl) {
        var mode = _TAB_BUY_MODE[_btTab] === 'close' ? '尾盘买T+1卖' : ('T+' + _getSellN(_btTab) + '开盘卖');
        scoreEl.innerHTML = '最低分 <b style=\"color:var(--accent)\">' + val + '</b> · ' + mode;
    }
    // 输入即保存+跑回测(去抖500ms)
    clearTimeout(window._msDebounce);
    window._msDebounce = setTimeout(function() {
        _setMinScore(_btTab, val);
        loadBacktestTab(_btTab, _BT_DAYS, _btTopN, _btCapital);
    }, 500);
}
function onBacktestMinScoreChange() {
    // oninput 已处理，这里仅兜底
    onBacktestMinScoreInput();
}

async function loadTomorrowSignals() {
    var el = document.getElementById('tomorrowSignals');
    if (!el) return;
    el.style.display = 'block';
    el.innerHTML = '<div class="loading">⏳ 正在分析明日信号...</div>';
    try {
        var _sigTopN = _btTopN || 3;
        var _sigCap = _btCapital || 30000;
        // 把 5 tab 全部参数传给后端, 让明日信号里的 EV 跟回测面板对齐
        var url = '/api/signal/tomorrow'
            + '?top_n=' + _btTopN
            + '&capital=' + _sigCap
            + '&limit_up_min_score=' + _getMinScore('limit-up')
            + '&limit_up_sell_n=' + _getSellN('limit-up')
            + '&trend_min_score=' + _getMinScore('trend')
            + '&trend_sell_n=' + _getSellN('trend')
            + '&reversal_min_score=' + _getMinScore('reversal')
            + '&reversal_sell_n=' + _getSellN('reversal')
            + '&zhaban_min_score=' + _getMinScore('zhaban')
            + '&zhaban_sell_n=' + _getSellN('zhaban')
            + '&dtqiaoban_min_score=' + _getMinScore('dtqiaoban')
            + '&dtqiaoban_sell_n=' + _getSellN('dtqiaoban');
        var resp = await fetch(url, { cache: 'no-store' });
        if (!resp.ok) {
            var errText = await resp.text();
            el.innerHTML = '<span style="color:#ef4444">❌ 服务端错误 (' + resp.status + '): ' + escapeHtml(errText.slice(0,200)) + '</span>';
            return;
        }
        var data = await resp.json();
        if (!data.ok) { el.innerHTML = '<span style="color:#ef4444">❌ ' + (data.error || '未知错误') + '</span>'; return; }
        // 头部信息
        var h = '<div style="font-weight:600;font-size:13px;margin-bottom:8px">📡 明日买入信号 · 信号日 ' + data.date + ' ' + data.weekday + ' → 买入日 ' + (data.buy_date || data.date) + ' ' + (data.buy_weekday || data.weekday) + '</div>';

        // 🏆 顶部「今日首推」卡片：综合打分最高那一只（= data.best）
        // 服务端已经按 recommendation_score 降序排过，candidates[0]/best 是首推
        if (data.best && data.best.name) {
            var b = data.best;
            var isLimitUp = b.tab === 'limit-up';
            var accent = isLimitUp ? '#ef4444' : '#22c55e';
            h += '<div style="position:relative;padding:12px 14px;margin:6px 0 12px 0;'
              + 'background:linear-gradient(135deg,' + accent + '22,' + accent + '08);'
              + 'border:2px solid ' + accent + ';border-radius:10px;'
              + 'box-shadow:0 2px 8px ' + accent + '33;">';
            h += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
              + '<span style="font-size:22px">🏆</span>'
              + '<span style="font-weight:800;font-size:15px;color:' + accent + '">今日首推</span>'
              + '<span style="margin-left:auto;font-size:10px;padding:2px 8px;border-radius:10px;'
              + 'background:' + accent + ';color:#fff;font-weight:600">综合分 ' + (b.recommendation_score || 0).toFixed(0) + '</span>'
              + '</div>';
            h += '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px">'
              + '<span style="font-weight:800;font-size:20px">' + b.name + '</span>'
              + '<span style="font-size:12px;color:var(--text-muted)">' + b.code + '</span>'
              + '<span style="font-size:11px;padding:3px 8px;border-radius:4px;background:' + accent + ';color:#fff">' + b.tab + '</span>'
              + '<span style="font-size:11px">当日评分 <b>' + (b.score || 0).toFixed(0) + '</b></span>'
              + '<span style="font-size:11px">历史 EV <b style="color:' + (b.expected_pnl_per_trade > 0 ? '#22c55e' : '#ef4444') + '">' + (b.expected_pnl_per_trade > 0 ? '+' : '') + (b.expected_pnl_per_trade || 0) + '</b> 元/笔</span>'
              + (b.filter_passed ? '<span style="font-size:11px;padding:2px 6px;border-radius:3px;background:#22c55e22;color:#22c55e">✓ 已通过实盘 filter</span>' : '<span style="font-size:11px;padding:2px 6px;border-radius:3px;background:#f59e0b22;color:#f59e0b">⚠ filter 未达标(取首推兜底)</span>')
              + '</div>';
            h += '<div style="font-size:11px;color:var(--text-muted);margin-top:6px">'
              + '样本 ' + (b.sample_size || '?') + ' | 置信 ' + (b.confidence_factor || '?') + ' | 稀有加成 ' + (b.rare_event_boost || '?')
              + ' | 策略: ' + (b.strategy_note || '-')
              + '</div>';
            h += '</div>';
            h += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;font-weight:600">📋 5 tab 全部候选（信号日 ' + data.date + ' ' + data.weekday + '）</div>';
        }

        if (data.alerts && data.alerts.length > 0) {
            h += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">';
            data.alerts.forEach(function(a) { h += '⏭ ' + a + '<br>'; });
            h += '</div>';
        }
        if (data.signals && data.signals.length > 0) {
            data.signals.forEach(function(s) {
                // 标记"被首推"的那张票（在候选明细里加 ⭐）
                var isBest = data.best && data.best.code === s.code && data.best.tab === s.tab;
                var rowBg = isBest ? '#fbbf2422' : (s.tab==='limit-up' ? '#ef444411' : '#22c55e11');
                var rowBd = isBest ? '#fbbf24' : (s.tab==='limit-up' ? '#ef4444' : '#22c55e');
                var tagBg = isBest ? '#fbbf24' : 'var(--bg-secondary)';
                var tagFg = isBest ? '#000' : 'var(--text)';
                h += '<div style="display:flex;align-items:center;gap:8px;padding:8px;margin:4px 0;background:' + rowBg + ';border-radius:6px;border-left:3px solid ' + rowBd + '">';
                if (isBest) h += '<span style="font-size:14px" title="今日首推">⭐</span>';
                h += '<span style="font-weight:700;font-size:14px">' + s.name + '</span>';
                h += '<span style="font-size:11px;color:var(--text-muted)">' + s.code + '</span>';
                h += '<span style="font-size:11px;padding:2px 6px;border-radius:3px;background:' + tagBg + ';color:' + tagFg + '">' + s.tab_cn + '</span>';
                h += '<span style="font-size:11px">评分 ' + s.score.toFixed(0) + '</span>';
                h += '<span style="font-size:10px;color:var(--text-muted)">买入日 ' + s.buy_date + '</span>';
                h += '</div>';
                h += '<div style="font-size:10px;color:var(--text-muted);margin-left:' + (isBest ? '24' : '8') + 'px;margin-bottom:6px">' + s.reason + '</div>';
            });
        } else {
            h += '<div style="color:var(--text-muted);font-size:12px">今日无符合规则的买入信号 (' + data.summary + ')</div>';
        }
        el.innerHTML = h;
    } catch(e) {
        el.innerHTML = '<span style="color:#ef4444">❌ 信号分析失败: ' + e.message + '</span>';
    }
}
window.loadTomorrowSignals = loadTomorrowSignals;
const _navItems = () => document.querySelectorAll('.nav-item');

let currentPage = '';
let _outputCache = {};  // {html, ts} - 服务端注入 _CACHED_RANKING 的临时展示(scan-limit fallback)
// v2 改造 (2026-07-03) 加 30s 过期机制: 防止旧版 HTML 被切回 tab 时复用
// nav 切 tab 永远重拉(见 switchPage),此处仅供 _CACHED_RANKING fallback 路径用
const _OUTPUT_CACHE_TTL_MS = 30 * 1000;  // 30s 内有效, 超过视为过期强制重拉

function _getCachedPage(page) { return null; }  // v3.3e: nocache
function _setCachedPage(page, html) {}  // v3.3e: nocache
let _lastUrl = {};  // 跟踪每个页面最后一次请求的 URL
let _pageToken = 0; // 页面切换令牌，切换时+1，异步渲染前校验——防止慢响应串台

const PAGES = {
    'scan-limit':   { title: '🛡️ 涨停扫描',   api: '/api/scan/limit-up/cards', textApi: '/api/scan/limit-up' },
    'scan-trend':   { title: '📊 趋势扫描',   api: '/api/scan/trend/cards',   textApi: '/api/scan/trend' },
    'scan-sector':  { title: '🧩 板块概览',   api: '/api/scan/sector/cards',  textApi: '/api/scan/sector' },
    'scan-zhaban':  { title: '💥 炸板分析',   api: '/api/scan/zhaban/cards',  textApi: '/api/scan/zhaban' },
    'scan-reversal':{title:'🔄 反转扫描',   api: '/api/scan/reversal/cards',textApi: '/api/scan/reversal' },
    'scan-dtqiaoban':{title:'📉 跌停翘板',   api: '/api/scan/dtqiaoban/cards',textApi: '/api/scan/dtqiaoban' },
    'backtest':     { title: '⏱️ 回测追踪',   api: '/api/backtest/dashboard', textApi: '/api/backtest' },
    'weights':      { title: '⚖️ 权重调整',   api: '/api/weights/tab/limit-up', textApi: '/api/weights/tab/limit-up' },
};

function showProgress(text, pct) {
    const bar = _dom.progress(), fill = _dom.fill(), txt = _dom.txt();
    if (!bar || !fill || !txt) return;
    bar.style.display = 'block';
    fill.style.width = (pct || 10) + '%';
    txt.textContent = text || '加载中...';
}

function hideProgress() {
    const bar = _dom.progress();
    if (bar) bar.style.display = 'none';
}

function switchPage(page) {
    try {
        const outputEl = _dom.output();
        if (currentPage && outputEl) {
            _setCachedPage(currentPage, outputEl.innerHTML);
        }

        currentPage = page;
        _pageToken++;  // 旧请求的异步回调检测到 token 不匹配会丢弃结果
        _navItems().forEach(el => el.classList.toggle('active', el.dataset.page === page));

        const info = PAGES[page];
        if (!info) return;
        _dom.pageTitle().textContent = info.title;
        // 外部 tab 评分显示（已精简）
        var scoreEl = document.getElementById('tab-score-display');
        if (scoreEl) scoreEl.textContent = '';
        document.body.dataset.page = page;

        // 页面切换动画（用 opacity 避免 layout）
        const mp = _dom.modulePage();
        if (mp) {
            mp.style.opacity = '0';
            requestAnimationFrame(() => {
                mp.style.transition = 'opacity 0.2s ease';
                mp.style.opacity = '1';
                setTimeout(() => mp.style.transition = '', 250);
            });
        }

        // nav 切 tab 永远重拉（用户明确意图 = 期望最新数据）
        // 缓存机制保留给"拉取/运行"按钮的瞬时回填，避免完全砍掉
        if (info.api) {
        showProgress('正在加载...', 20);
        outputEl.innerHTML = '';
        setTimeout(() => runCurrent(), 80);
    } else {
        outputEl.innerHTML = '<span class="loading">输出结果</span>';
    }
    } catch (e) {
        console.error('[switchPage error]', e);
        const out = _dom.output();
        if (out) out.innerHTML = `<span class="error-text">⚠️ 页面切换错误: ${e.message}</span>`;
    }
}

function getPrincipal() {
    var el = document.getElementById('principal-input');
    return el ? el.value || '20000' : '20000';
}
function savePrincipal() {
    var el = document.getElementById('principal-input');
    if (el) localStorage.setItem('_principal', el.value);
}
// 2026-07-05: 仅剩 Plan A, 所有 getPlan/savePlan/loadPlans 改为永远返 'A'
function getPlan() { return 'A'; }
function savePlan() { /* no-op */ }
function loadPlans() {
    var sel = document.getElementById('plan-select');
    if (sel) {
        sel.innerHTML = '<option value="A" selected>⭐ Plan A — 9因子加权 + 危险信号 + 龙头检测</option>';
    }
}
// 回测面板分页按钮（事件委托）
document.addEventListener('click', function(e) {
    var btn = e.target.closest('.corr-page-btn');
    if (!btn) return;
    var uid = btn.dataset.uid;
    var delta = parseInt(btn.dataset.delta);
    var total = parseInt(btn.dataset.total);
    if (uid && window.corrPage) window.corrPage(uid, delta, total);
});
// 加载时恢复上次本金和方案
document.addEventListener('DOMContentLoaded', function() {
    var el = document.getElementById('principal-input');
    var saved = localStorage.getItem('_principal');
    if (saved && el) el.value = saved;
    if (el) el.addEventListener('change', savePrincipal);
    loadPlans();
});

async function runCurrent() {
    const info = PAGES[currentPage];
    if (!info) return;
    savePrincipal();
    var plan = getPlan();
    // 切tab时自动拉取最新数据（refresh=1 绕过 daily_get 缓存）
    var url = info.api + '?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '') + '&refresh=1&_t=' + Date.now();
    await callApi(url, currentPage);
}

var _busy = false;  // 防连点锁

// 「拉取」—— 全局：一次性拉取所有板块原始数据并缓存
async function fetchAllRawData() {
    if (_busy) { console.log('[拉取] 忙碌中，忽略连点'); return; }
    _busy = true;
    try {
        savePrincipal();
        var plan = getPlan();
        var t = Date.now();
        var url = '/api/scan/fetch-all?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '') + '&_t=' + t;
        _lastUrl = {}; _outputCache = {};
        // 拉取数据始终去涨停扫描页（fetch-all只拉涨停相关数据）
        if (currentPage !== 'scan-limit') {
            currentPage = 'scan-limit';
            _navItems().forEach(el => el.classList.toggle('active', el.dataset.page === 'scan-limit'));
            _dom.pageTitle().textContent = PAGES['scan-limit'].title;
            document.body.dataset.page = 'scan-limit';
        }
        await callApi(url, 'scan-limit');
        updateCacheStatus();
    } finally { _busy = false; }
}

// 「运行」—— 所有板块统一走流式端点，非流式 tab 也强制刷新
async function runCurrentFromCache() {
    if (_busy) { console.log('[运行] 忙碌中，忽略连点'); return; }
    _busy = true;
    try {
        const info = PAGES[currentPage];
        if (!info) { console.error('[运行] 无当前页', currentPage); return; }
        savePrincipal();
        var plan = getPlan();
        showProgress('正在运行...', 5);
        var t = Date.now();
        var streamMap = {
            'scan-limit':   '/api/scan/limit-up/run',
            'scan-zhaban':  '/api/scan/zhaban/stream',
            'scan-trend':   '/api/scan/trend/stream',
            'scan-dtqiaoban':'/api/scan/dtqiaoban/stream',
            'scan-sector':  '/api/scan/sector/stream',
        };
        var base = streamMap[currentPage] || info.api;
        var params = '?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '') + '&_t=' + t + '&refresh=1';
        var url = base + params;
        _lastUrl[currentPage] = ''; _outputCache[currentPage] = '';
        await callApi(url, currentPage);
    } finally { _busy = false; }
}

async function refreshCurrent() {
    if (_busy) { console.log('[刷新] 忙碌中'); return; }
    _busy = true;
    try {
        savePrincipal();
        var plan = getPlan();
        var info = PAGES[currentPage];
        if (!info || !info.api) { console.error('[刷新] 无当前页'); return; }
        // 刷新当前 tab（不跳页），加 refresh=1 强制拉新数据
        var url = info.api + '?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '') + '&refresh=1&_t=' + Date.now();
        _lastUrl[currentPage] = ''; _outputCache[currentPage] = '';
        await callApi(url, currentPage);
        updateCacheStatus();
    } finally { _busy = false; }
}

function updateCacheStatus() {
    var el = document.getElementById('cache-status');
    if (el) { el.textContent = '✅'; el.title = '实时数据'; el.style.color = '#4ade80'; }
}

async function callApi(apiUrl, pageKey) {
    const output = _dom.output();

    const info = PAGES[pageKey] || PAGES['scan-limit'];
    _lastUrl[pageKey] = apiUrl || '';

    // SSE 流式端点 → 走卡片流式加载（scan 板块统一）
    var isStream = apiUrl && (apiUrl.indexOf('/run') >= 0 || apiUrl.indexOf('fetch-all') >= 0 || (apiUrl.indexOf('/scan/') >= 0 && apiUrl.indexOf('/stream') >= 0));

    if (isStream) {
        await loadCardViewStream(output, pageKey, apiUrl);
    } else if (info.textApi) {
        await loadCardView(output, pageKey, apiUrl);
    } else if (info.streamApi) {
        await loadTextViewStream(output, pageKey, apiUrl);
    } else {
        await loadTextView(output, pageKey, apiUrl);
    }
    _setCachedPage(pageKey, output.innerHTML);
    // P1.2.2: 胜率徽章已与下方"系统状态"重复,禁用注入
    // _injectTrackerBadge(pageKey, output);
}

// ─── 各 Tab 胜率徽章 ───
var _trackerCache = null;
var _trackerFetching = false;
var _trackerFetchers = [];

async function _fetchTrackerStats() {
    if (_trackerFetching) {
        return new Promise(function(resolve) { _trackerFetchers.push(resolve); });
    }
    _trackerFetching = true;
    try {
        var resp = await fetch('/api/tracker/stats?_r=' + Math.random().toString(36).slice(2), { cache: 'no-store' });
        var data = await resp.json();
        _trackerFetchers.forEach(function(f) { f(data); });
        _trackerFetchers = [];
        return data;
    } catch(e) {
        _trackerFetchers.forEach(function(f) { f(null); });
        _trackerFetchers = [];
        return null;
    } finally {
        _trackerFetching = false;
    }
}

function _pageKeyToTrackerTab(pageKey) {
    var map = {
        'scan-limit': 'limit-up',
        'scan-trend': 'trend',
        'scan-zhaban': 'zhaban',
        'scan-dtqiaoban': 'dtqiaoban',
        'scan-sector': 'sector',
        'scan-reversal': 'reversal',
        'indicators': 'indicators',
        'community': 'community',
    };
    return map[pageKey] || pageKey.replace('scan-', '');
}

async function _injectTrackerBadge(pageKey, outputEl) {
    // P1.2.2: 用户反馈 — 各 tab 右上角的胜率徽章与下方"系统状态"区重复,已隐藏
    return;
}

async function loadTextView(output, pageKey, apiUrl) {
    const token = _pageToken;
    const info = PAGES[pageKey];
    const url = apiUrl || info.api;
    // 显示进度条动画（非流式页面用估算进度）
    showProgress('正在加载...', 15);
    let estPct = 15;
    const estInterval = setInterval(() => {
        estPct = Math.min(85, estPct + Math.random() * 8);
        const fill = _dom.fill();
        if (fill) fill.style.width = estPct + '%';
    }, 300);

    try {
        const resp = await fetch(url, { cache: 'no-store' });
        const data = await resp.json();
        clearInterval(estInterval);
        showProgress('加载完成', 100);
        if (data.ok === false) {
            if (_pageToken === token) output.innerHTML = `<span class="error-text">❌ 错误：</span>\n${escapeHtml(data.error || '未知错误')}\n\n${escapeHtml(data.output || '')}`;
        } else {
            if (_pageToken === token) output.innerHTML = renderStyledText(data.output);
        }
    } catch (err) {
        clearInterval(estInterval);
        if (_pageToken === token) output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
    hideProgress();
}

// ─── 文本流式加载（龙虎榜/舆情，SSE 实时进度） ───
async function loadTextViewStream(output, pageKey, apiUrl) {
    const token = _pageToken;
    const info = PAGES[pageKey];
    const url = apiUrl || info.streamApi;
    const bar = _dom.progress(), fill = _dom.fill(), txt = _dom.txt();
    if (bar) bar.style.display = 'block';
    if (fill) fill.style.width = '10%';
    if (txt) txt.textContent = '正在加载...';
    output.innerHTML = '<span class="loading">⏳ 正在扫描...</span>';

    await new Promise(r => setTimeout(r, 40));

    try {
        const resp = await fetch(url, { cache: 'no-store' });
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';

        while (true) {
            const {done, value} = await reader.read();
            if (done) break;

            buf += dec.decode(value, {stream: true});
            const parts = buf.split('\n\n');
            buf = parts.pop() || '';

            for (const part of parts) {
                for (const line of part.split('\n')) {
                    if (!line.startsWith('data: ')) continue;
                    try {
                        const msg = JSON.parse(line.slice(6));

                        if (msg.type === 'progress') {
                            if (txt) txt.textContent = msg.text;
                            // 进度条逐步推进：每收到一条 progress，涨 2-5%
                            const curW = parseFloat(fill ? fill.style.width : '10') || 10;
                            const step = msg.text.includes('第5步') || msg.text.includes('评分') ? 8 :
                                         msg.text.includes('第4步') || msg.text.includes('资金') ? 6 : 3;
                            const newW = Math.min(90, curW + step);
                            if (fill) fill.style.width = newW + '%';
                        } else if (msg.type === 'complete') {
                            if (bar) bar.style.display = 'none';
                            if (_pageToken !== token) return;
                            output.innerHTML = msg.output
                                ? renderStyledText(msg.output)
                                : '<span class="loading">暂无数据</span>';
                            _setCachedPage(pageKey, output.innerHTML);
                            return;
                        } else if (msg.type === 'error') {
                            if (bar) bar.style.display = 'none';
                            if (_pageToken !== token) return;
                            output.innerHTML = `<span class="error-text">❌ ${escapeHtml(msg.text)}</span>`;
                            _setCachedPage(pageKey, output.innerHTML);
                            return;
                        }
                    } catch (_) {}
                }
            }
        }
        if (bar) bar.style.display = 'none';
        if (_pageToken === token) output.innerHTML = '<span class="loading">连接中断</span>';

    } catch (err) {
        if (bar) bar.style.display = 'none';
        if (_pageToken === token) output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
}

async function loadCardView(output, pageKey, apiUrl) {
    const token = _pageToken;
    const info = PAGES[pageKey] || PAGES['scan-limit'];
    const url = apiUrl || info.api;

    // 非涨停扫描也显示加载状态
    // 注: scan-limit 的流式路径在 callApi 中通过 isStream 判断走 loadCardViewStream
    //     这里的 loadCardView 处理 JSON 卡片端点（如 /cards），不要重定向到流解析器
    showProgress('正在拉取数据...', 30);
    output.innerHTML = '<span class="loading">⏳ 正在扫描...</span>';

    try {
        const resp = await fetch(url, { cache: 'no-store' });
        const data = await resp.json();

        const items = data.stocks || data.items || [];
        // 特殊页面：情绪(score/level)、回测(weights/corr_history)、权重(factors)
        if ((pageKey === 'sentiment' && data.score !== undefined) ||
            (pageKey === 'backtest' && data.weights !== undefined) ||
            (pageKey === 'weights' && data.factors !== undefined)) {
            // handled below
        } else if (!data.ok || !items.length) {
            output.innerHTML = '<span class="loading">暂无数据</span>';
            hideProgress();
            return;
        }

        let html = '';
        if (data.fetched_at) html += `<div style="font-size:11px;color:var(--text-muted);padding:4px 14px 0;text-align:right">数据拉取: ${escapeHtml(data.fetched_at)}</div>`;

        try {
            if (pageKey === 'sentiment') {
                html += renderSentimentCards(data);
            } else if (pageKey === 'scan-sector') {
                html += renderSectorCards(items);
            } else if (pageKey === 'scan-limit') {
                html += renderStockCards(items, data);
            } else if (pageKey === 'indicators') { html += renderIndicatorsCards(items); } else if (pageKey === 'community') {
                html += renderCommunityCards(items);
            } else if (pageKey === 'backtest') {
                // P6: 多 tab T+1 真实回测面板 — 带 tab 切换器
                const tabs = [
                    { key: 'trend', label: '趋势', days: _BT_DAYS },
                    { key: 'limit-up', label: '涨停', days: _BT_DAYS },
                    { key: 'zhaban', label: '炸板', days: _BT_DAYS },
                    { key: 'reversal', label: '反转', days: _BT_DAYS },
                    { key: 'dtqiaoban', label: '跌停', days: _BT_DAYS },
                ];
                // 用户1把梭3万, 全 tab 统一 TOP1
                var defaultTopN = { 'limit-up': 1, 'zhaban': 1, 'trend': 1, 'dtqiaoban': 1, 'reversal': 1 };
                const activeTab = (typeof _btTab !== 'undefined' && _btTab) || 'trend';
                html += '<div id="tabWeightsArea" style="margin:16px 16px 0 16px"></div>';
                html += '<div style="margin:16px;padding:12px;background:var(--card-bg);border-radius:8px">'
                      + '<div style="font-size:13px;color:var(--text-muted);margin-bottom:4px">📊 多 Tab 真实回测</div>'

                      + '<div id="btTabBar" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">';
                tabs.forEach(t => {
                    const active = t.key === activeTab ? 'background:var(--accent);color:#fff' : 'background:var(--bg-secondary);color:var(--text)';
                    html += '<button class="btn" style="font-size:11px;padding:6px 10px;' + active + '" onclick="switchBacktestTab(\'' + t.key + '\',' + t.days + ')">' + t.label + '</button>';
                });
                // TOP-N 调权 + 本金输入
                html += '<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;font-size:12px">'
                    + '<span style="color:var(--text-muted)">买</span>'
                    + '<select id="btTopN" onchange="onBacktestParamChange()" style="padding:4px 8px;border-radius:4px;background:var(--bg-secondary);color:var(--text);border:1px solid var(--border)">'
                    + [1,2,3,5,10].map(n => '<option value="' + n + '"' + (n === _btTopN ? ' selected' : '') + '>TOP' + n + '</option>').join('')
                    + '</select>'
                    + '<input id="btCapital" type="number" value="' + _btCapital + '" onchange="onBacktestParamChange()" style="width:80px;padding:4px 8px;border-radius:4px;background:var(--bg-secondary);color:var(--text);border:1px solid var(--border)" step="5000" min="10000">'
                    + '<span style="color:var(--text-muted)">元</span>'
                    + '<span style="color:var(--text-muted);margin-left:4px">(单只本金, 单笔 EV 跟回测对齐)</span>'
                    // 2026-07-05: 删除 strategy preset 下拉 (limit-prime/trend-elite/limit-sweet)
                    // 唯一过滤逻辑 = plan_a 评分 + min_score 阈值 + sell_n 卖出日.

                    + '<button onclick="window._resetBacktestParams()" title="重置回默认 TOP1+3万" style="margin-left:8px;padding:3px 8px;font-size:11px;background:var(--bg-secondary);color:var(--text-muted);border:1px solid var(--border);border-radius:4px;cursor:pointer">↻ 重置</button>'
                    + '<button onclick="loadTomorrowSignals()" title="明日买入信号(盘前规则)" style="margin-left:4px;padding:3px 8px;font-size:11px;background:#f59e0b;color:#fff;border:1px solid #f59e0b;border-radius:4px;cursor:pointer;font-weight:600">📡 明日信号</button>'
                    + '</div>'
                    + '<div id="tomorrowSignals" style="display:none;margin:8px 0;padding:12px;background:var(--card-bg);border:1px solid #f59e0b;border-radius:8px"></div>';
                html += '</div><div id="btTabContent"><div class="loading">⏳ 加载中...</div></div>';
                html += '</div></div>';
            } else if (pageKey === 'scan-dtqiaoban') { html += renderDtqiaobanCards(items); } else if (pageKey === 'scan-zhaban') { html += renderZhabanCards(items); } else if (pageKey === 'scan-trend') { html += renderTrendCards(items); } else if (pageKey === 'scan-reversal') { html += renderReversalCards(items);
            } else if (pageKey === 'weights') { html += renderWeightsPanel(data);
            } else {
                html += renderSimpleCards(items, pageKey);
            }
        } catch (renderErr) {
            html += `<div class="error-text">❌ 渲染错误: ${renderErr.message}</div>`;
            console.error('[Render Error]', pageKey, renderErr);
        }
        if (_pageToken === token) { output.innerHTML = html; _setCachedPage(pageKey, output.innerHTML); }
        // 回测页面: 异步加载策略权重 + 当前tab回测数据
        if (pageKey === 'backtest') {
            fetch('/api/backtest/tab-weights')
                .then(r => r.json())
                .then(d => {
                    if (d.ok && d.weights && _pageToken === token) {
                        var wa = document.getElementById('tabWeightsArea');
                        if (wa) wa.innerHTML = renderTabWeights(d.weights);
                    }
                })
                .catch(function() {});
            // 加载当前tab的回测数据
            setTimeout(function() { loadBacktestTab(_btTab, _BT_DAYS, _btTopN, _btCapital); }, 100);
        }
    } catch (err) {
        if (_pageToken === token) output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
    hideProgress();
}

// ─── SSE 流式加载（防抖 + RAF 优化） ───
async function loadCardViewStream(output, pageKey, apiUrl) {
    const token = _pageToken;
    const bar = _dom.progress();
    const fill = _dom.fill();
    const txt = _dom.txt();

    bar.style.display = 'block';
    fill.style.width = '3%';
    txt.textContent = '正在扫描...';
    output.innerHTML = '<span class="loading">⏳ 正在扫描...</span>';

    const STEP_PCT = { '第1步':5, '第2步':10, '第3步':40, '第4步':55, '第5步':65, '第6步':80, '第7步':90 };
    let currentPct = 3;

    // 防抖累积：在下一个 RAF 中一次性应用
    let _pendingUpdate = null;
    const flushProgress = () => {
        if (_pendingUpdate) {
            txt.textContent = _pendingUpdate;
            _pendingUpdate = null;
        }
    };
    const scheduleProgress = (text) => {
        _pendingUpdate = text;
        if (!window._rafScheduled) {
            window._rafScheduled = true;
            requestAnimationFrame(() => {
                window._rafScheduled = false;
                flushProgress();

                // 同时更新进度条
                for (const [kw, pct] of Object.entries(STEP_PCT)) {
                    if (txt.textContent.includes(kw)) {
                        currentPct = Math.max(currentPct, pct);
                        break;
                    }
                }
                fill.style.width = Math.min(95, currentPct) + '%';
            });
        }
    };

    await new Promise(r => setTimeout(r, 40));  // 给浏览器一点时间渲染初始状态

    try {
        // 从 apiUrl 提取基础路径和参数（不再硬编码端点）
        var qIdx = (apiUrl || '').indexOf('?');
        var streamUrl = qIdx >= 0 ? apiUrl.substring(0, qIdx) : (apiUrl || '/api/scan/limit-up/stream');
        var params = [];
        if (qIdx >= 0) {
            var qs = apiUrl.substring(qIdx + 1);
            qs.split('&').forEach(function(p) {
                var kv = p.split('=');
                if (kv[0] !== '_t') params.push(p);  // 去掉旧时间戳
            });
        }
        params.push('_t=' + Date.now());  // 新时间戳防缓存
        streamUrl += '?' + params.join('&');
        const resp = await fetch(streamUrl, { cache: 'no-store' });
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';

        while (true) {
            const {done, value} = await reader.read();
            if (done) break;

            buf += dec.decode(value, {stream: true});
            const parts = buf.split('\n\n');
            buf = parts.pop() || '';

            for (const part of parts) {
                for (const line of part.split('\n')) {
                    if (!line.startsWith('data: ')) continue;
                    try {
                        const msg = JSON.parse(line.slice(6));

                        if (msg.type === 'progress') {
                            scheduleProgress(msg.text);
                            if (msg.text.includes('命中本地缓存')) currentPct = Math.max(currentPct, 48);
                            else if (msg.text.includes('同花顺资金流')) currentPct = Math.max(currentPct, 18);

                        } else if (msg.type === 'complete') {
                            // 先 flush 剩余进度更新
                            flushProgress();
                            bar.style.display = 'none';

                            // 使用 RAF 批量渲染卡片
                            const fet = msg.fetched_at || '';
                            let html = '';
                            if (fet) html += `<div style="font-size:11px;color:var(--text-muted);padding:4px 14px 0;text-align:right">数据拉取: ${escapeHtml(fet)}</div>`;
                            // 按 tab 类型渲染对应卡片
                            var items = msg.stocks || msg.items || [];
                            if (pageKey === 'scan-zhaban') {
                                html += renderZhabanCards(items);
                            } else if (pageKey === 'scan-trend') {
                                html += renderTrendCards(items);
                            } else if (pageKey === 'scan-dtqiaoban') {
                                html += renderDtqiaobanCards(items);
                            } else if (pageKey === 'scan-sector') {
                                html += renderSectorCards(items);
                            } else {
                                html += renderStockCards(msg.stocks || [], msg);
                            }

                            // 用 requestAnimationFrame + 微任务分离 DOM 操作和渲染
                            requestAnimationFrame(() => {
                                if (_pageToken !== token) return;
                                output.innerHTML = html;
                                _lastUrl[pageKey] = apiUrl || '';
                                _setCachedPage(pageKey, output.innerHTML);
                            });
                            return;

                        } else if (msg.type === 'error') {
                            bar.style.display = 'none';
                            if (_pageToken !== token) return;
                            output.innerHTML = `<span class="error-text">❌ ${escapeHtml(msg.text)}</span>`;
                            _setCachedPage(pageKey, output.innerHTML);
                            return;
                        }
                    } catch (_) {}
                }
            }
        }
        bar.style.display = 'none';
        output.innerHTML = '<span class="loading">连接中断</span>';

    } catch (err) {
        bar.style.display = 'none';
        output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
}

// ─── 工具函数 ───


function clearOutput() { const el = _dom.output(); el.innerHTML = '<span class="loading">输出结果</span>'; delete _outputCache[currentPage]; }
function exportOutput() {
    // 从卡片中提取股票代码+名称
    var cards = _dom.output().querySelectorAll('.stock-card');
    var lines = [];
    cards.forEach(function(card) {
        var code = card.querySelector('.card-rank');
        var name = card.querySelector('.card-name');
        if (code && name) {
            lines.push(code.textContent.trim() + ' ' + name.textContent.trim());
        }
    });
    var txt = lines.join('\n') || _dom.output().textContent.trim();
    if (navigator.clipboard) {
        navigator.clipboard.writeText(txt).then(() => {
            var btn = document.querySelector('.quick-actions .btn:first-child');
            if (btn) {
                var orig = btn.textContent;
                btn.textContent = '✅ 已复制';
                setTimeout(function() { btn.textContent = orig; }, 1500);
            }
        }).catch(function() { alert('复制失败'); });
    } else {
        // HTTP 环境降级方案
        const ta = document.createElement('textarea');
        ta.value = txt;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand('copy');
            const btn = document.querySelector('.quick-actions .btn:nth-child(2)');
            const orig = btn.textContent;
            btn.textContent = '✅ 已复制';
            setTimeout(() => btn.textContent = orig, 1500);
        } catch (e) {
            alert('复制失败，请手动选择文本后 Ctrl+C');
        }
        document.body.removeChild(ta);
    }
}
async function runAll() {
    const pages = ['scan-trend','scan-limit','scan-sector','scan-zhaban','scan-dtqiaoban','indicators','community','sentiment','backtest'];
    for (const key of pages) {
        location.hash = key;
        await new Promise(r => setTimeout(r, 500));
    }
}

// ─── 市场模式 ───
var _marketStatus = 'closed';
var _marketLabels = {
    'trading': { icon: '⚡', text: '盘中', cls: 'mode-trading' },
    'closed': { icon: '🌙', text: '盘后', cls: 'mode-closed' },
    'weekend': { icon: '🎉', text: '休市', cls: 'mode-weekend' },
    'lunch': { icon: '☕', text: '午休', cls: 'mode-lunch' },
    'holiday': { icon: '🎌', text: '假日', cls: 'mode-weekend' },
};

async function loadMarketStatus() {
    try {
        var resp = await fetch('/api/market-status', { cache: 'no-store' });
        var d = await resp.json();
        if (d.ok) _marketStatus = d.status;
    } catch(e) {}
    updateMarketUI();
}

function updateMarketUI() {
    var info = _marketLabels[_marketStatus] || _marketLabels['closed'];
    // 侧边栏底部
    var modeEl = document.getElementById('market-mode');
    if (!modeEl) {
        modeEl = document.createElement('div');
        modeEl.id = 'market-mode';
        modeEl.style.cssText = 'padding:6px 20px;font-size:11px;display:flex;align-items:center;gap:6px;border-top:1px solid var(--border-light)';
        document.querySelector('.sidebar').appendChild(modeEl);
    }
    modeEl.innerHTML = '<span>' + info.icon + '</span><span>' + info.text + '</span>';
    // 移动端顶部标题栏
    var titleEl = document.querySelector('.mobile-title');
    if (titleEl) {
        titleEl.innerHTML = '选股扫描器 <span style="font-size:10px;color:var(--text-muted);font-weight:400">' + info.icon + ' ' + info.text + '</span>';
    }

    // 盘中模式：隐藏盘中不相关的页面
    var hiddenInTrading = { 'scan-trend': true, 'indicators': true, 'backtest': true };
    var hiddenInLunch = { 'scan-trend': true, 'indicators': true, 'backtest': true };
    document.querySelectorAll('.nav-item[data-page]').forEach(function(el) {
        var page = el.dataset.page;
        if ((_marketStatus === 'trading' && hiddenInTrading[page]) || (_marketStatus === 'lunch' && hiddenInLunch[page])) {
            el.style.display = 'none';
        } else {
            el.style.display = '';
        }
    });
}

// ─── 移动端侧边栏 ───
function toggleSidebar() {
    document.querySelector('.sidebar').classList.toggle('open');
    document.getElementById('sidebar-overlay').classList.toggle('open');
}
function closeSidebar() {
    document.querySelector('.sidebar').classList.remove('open');
    document.getElementById('sidebar-overlay').classList.remove('open');
}

// ─── 初始化 ───
document.addEventListener('DOMContentLoaded', () => {
    updateCacheStatus();
    loadMarketStatus();
    loadDashboard(true);  // v3.4d: 强制刷新，避免 daily_get 缓存旧数据

    document.querySelectorAll('.nav-item').forEach(el => {
        el.addEventListener('click', () => { location.hash = el.dataset.page; closeSidebar(); });
    });

    function onHash() {
        const page = location.hash.slice(1) || 'scan-trend';
        if (PAGES[page]) switchPage(page);
    }
    window.addEventListener('hashchange', onHash);

    const initPage = location.hash.slice(1) || 'scan-trend';
    if (PAGES[initPage]) {
        _navItems().forEach(el => el.classList.toggle('active', el.dataset.page === initPage));
        switchPage(initPage);
    }

    document.addEventListener('keydown', e => {
        if (e.key === 'Enter' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'SELECT')
            refreshCurrent();
    });
});