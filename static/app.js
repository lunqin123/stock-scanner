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

// P6: 当前选中的回测 tab
let _btTab = 'limit-up';

// P6: 切换回测 tab
async function switchBacktestTab(tab, days) {
    _btTab = tab;
    const contentEl = document.getElementById('btTabContent');
    if (!contentEl) return;
    contentEl.innerHTML = '<div class="loading">⏳ 加载中...</div>';
    // 更新 tab 按钮高亮
    const bar = document.getElementById('btTabBar');
    if (bar) {
        bar.querySelectorAll('button').forEach(btn => {
            const isActive = btn.textContent.trim() === ({'limit-up':'涨停','trend':'趋势','zhaban':'炸板','dtqiaoban':'翘板','reversal':'反转','sector':'板块'})[tab];
            btn.style.background = isActive ? 'var(--accent)' : 'var(--bg-secondary)';
            btn.style.color = isActive ? '#fff' : 'var(--text)';
        });
    }
    try {
        // P1.2.1: 加 60s 超时
        const ctrl = new AbortController();
        const tid = setTimeout(() => ctrl.abort(), 60000);
        const resp = await fetch('/api/bt/' + tab + '?days=' + days + '&top_n=3&capital=30000', { signal: ctrl.signal });
        clearTimeout(tid);
        const data = await resp.json();
        contentEl.innerHTML = renderT1BacktestPanel(data);
    } catch (e) {
        const msg = e.name === 'AbortError' ? '请求超时 (60s)' : e.message;
        contentEl.innerHTML = '<div class="error-text">❌ 加载失败: ' + msg + '</div>';
    }
}
const _navItems = () => document.querySelectorAll('.nav-item');

let currentPage = '';
let _outputCache = {};  // {html: string} - 服务端注入 _CACHED_RANKING 的临时展示(scan-limit fallback)
// nav 切 tab 永远重拉(见 switchPage),此处仅供 _CACHED_RANKING fallback 路径用

function _getCachedPage(page) {
    var c = _outputCache[page];
    return c ? c.html : null;
}
function _setCachedPage(page, html) {
    _outputCache[page] = html;
}
let _lastUrl = {};  // 跟踪每个页面最后一次请求的 URL
let _pageToken = 0; // 页面切换令牌，切换时+1，异步渲染前校验——防止慢响应串台

const PAGES = {
    'scan-limit':   { title: '🛡️ 涨停扫描',   api: '/api/scan/limit-up/cards', textApi: '/api/scan/limit-up' },
    'scan-trend':   { title: '📊 趋势扫描',   api: '/api/scan/trend/cards',   textApi: '/api/scan/trend' },
    'scan-sector':  { title: '🧩 板块热度',   api: '/api/scan/sector/cards',  textApi: '/api/scan/sector' },
    'scan-zhaban':  { title: '💥 炸板分析',   api: '/api/scan/zhaban/cards',  textApi: '/api/scan/zhaban' },
    'scan-reversal':{title:'🔄 反转扫描',   api: '/api/scan/reversal/cards',textApi: '/api/scan/reversal' },
    'scan-dtqiaoban':{title:'📉 跌停翘板',   api: '/api/scan/dtqiaoban/cards',textApi: '/api/scan/dtqiaoban' },
    'indicators':   { title: '🏆 龙虎榜分析', api: '/api/indicators/cards', textApi: '/api/indicators', streamApi: '/api/indicators/stream' },
    'community':    { title: '💬 舆情监测',   api: '/api/community/cards', textApi: '/api/community', streamApi: '/api/community/stream' },
    'sentiment':    { title: '🌡️ 市场情绪',   api: '/api/sentiment/cards', textApi: '/api/sentiment' },
    'backtest':     { title: '⏱️ 回测追踪',   api: '/api/backtest/dashboard', textApi: '/api/backtest' },
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
function getPlan() {
    var el = document.getElementById('plan-select');
    return el ? el.value || '' : '';
}
function savePlan() {
    var el = document.getElementById('plan-select');
    if (el) localStorage.setItem('_plan', el.value || '');
}
function loadPlans() {
    fetch('/api/plans')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.ok) return;
            var sel = document.getElementById('plan-select');
            if (!sel) return;
            sel.innerHTML = '';
            data.plans.forEach(function(p) {
                var opt = document.createElement('option');
                opt.value = p.name;
                opt.textContent = (p.is_default ? '⭐ ' : '') + 'Plan ' + p.name + ' — ' + p.description;
                sel.appendChild(opt);
            });
            var saved = localStorage.getItem('_plan');
            if (saved && sel.querySelector('option[value="' + saved + '"]')) {
                sel.value = saved;
            }
        })
        .catch(function() {});
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
    var url = info.api + '?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '') + '&_r=' + Math.random().toString(36).slice(2);
    if (_lastUrl[currentPage] === url && _getCachedPage(currentPage)) {
        return;
    }
    _lastUrl[currentPage] = url;

    // 页面刷新后 localStorage 有缓存 → 瞬间展示，不重复请求
    if (!_getCachedPage(currentPage)) {
        // QQ 浏览器等无法依赖 localStorage → 用服务端注入的数据
        if (currentPage === 'scan-limit' && window._CACHED_RANKING) {
            var cr = window._CACHED_RANKING;
            if (cr.stocks && cr.stocks.length) {
                var rankingHtml = renderStockCards(cr.stocks, cr);
                var s = cr.sentiment || {};
                rankingHtml = (cr.fetched_at ? '<div style="font-size:11px;color:var(--text-muted);padding:4px 14px 0;text-align:right">数据拉取: ' + esc(cr.fetched_at) + '</div>' : '') +
                    rankingHtml;
                _setCachedPage(currentPage, rankingHtml);
                _dom.output().innerHTML = rankingHtml;
                hideProgress();
                return;
            }
        }
    }

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
    await fetchAllRawData();
}

function updateCacheStatus() {
    var el = document.getElementById('cache-status');
    if (!el) return;
    el.textContent = '⏳ 检查中...';
    var plan = getPlan();
    fetch('/api/scan/limit-up/run?principal=' + getPrincipal() + (plan ? '&plan=' + plan : '') + '&_t=' + Date.now())
        .then(function(r) {
            // SSE流返回200=有缓存或服务正常
            el.textContent = '✅ 缓存就绪';
            el.style.color = '#4ade80';
        })
        .catch(function() {
            el.textContent = '⚠ 需拉取数据';
            el.style.color = '#fbbf24';
        });
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
    // 注入各 tab 胜率追踪徽章
    _injectTrackerBadge(pageKey, output);
}

// ─── 各 Tab 胜率徽章 ───
var _trackerCache = null;
var _trackerFetching = false;
var _trackerFetchers = [];

async function _fetchTrackerStats() {
    if (_trackerCache) return _trackerCache;
    if (_trackerFetching) {
        return new Promise(function(resolve) { _trackerFetchers.push(resolve); });
    }
    _trackerFetching = true;
    try {
        var resp = await fetch('/api/tracker/stats?_r=' + Math.random().toString(36).slice(2));
        var data = await resp.json();
        _trackerCache = data;
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
    var tab = _pageKeyToTrackerTab(pageKey);
    if (!tab) return;
    var statsData = await _fetchTrackerStats();
    if (!statsData || !statsData.ok) return;
    var tabsArr = statsData.tabs || [];
    var tabStats = null;
    // Handle both array and object responses
    if (Array.isArray(tabsArr)) {
        for (var i = 0; i < tabsArr.length; i++) {
            if (tabsArr[i].tab === tab) { tabStats = tabsArr[i]; break; }
        }
    }
    if (!tabStats || tabStats.count === 0) {
        // 暂无数据，显示一个轻提示
        var hint = document.createElement('div');
        hint.style.cssText = 'margin:8px 14px 0;padding:6px 12px;font-size:11px;color:var(--text-muted);border:1px dashed var(--border);border-radius:6px';
        hint.textContent = '追踪数据收集中，运行扫描后次日自动生成胜率';
        var first = outputEl.querySelector('.card') || outputEl.querySelector('.dashboard-grid') || outputEl.firstChild;
        if (first && first.parentNode) first.parentNode.insertBefore(hint, first);
        else outputEl.insertBefore(hint, outputEl.firstChild);
        return;
    }

    var wr = tabStats.win_rate || 0;
    var barW = Math.min(100, Math.max(0, wr));
    var color = wr >= 60 ? '#22c55e' : wr >= 45 ? '#f59e0b' : '#ef4444';
    var days = tabStats.days_count || '?';

    // 按排名胜率
    var rankHtml = '';
    var rs = tabStats.rank_stats || {};
    var rankKeys = Object.keys(rs).sort(function(a,b) { return parseInt(a) - parseInt(b); });
    for (var ri = 0; ri < rankKeys.length; ri++) {
        var rk = rankKeys[ri];
        var rd = rs[rk];
        var rwr = rd.count > 0 ? (rd.wins / rd.count * 100).toFixed(0) : 0;
        rankHtml += '<span style="color:var(--text-muted);margin-right:6px">TOP' + rk + '<b style="color:' + (rwr >= 60 ? '#22c55e' : rwr >= 45 ? '#f59e0b' : '#ef4444') + '">' + rwr + '%</b></span>';
    }

    var badge = document.createElement('div');
    badge.style.cssText = 'display:flex;align-items:center;gap:8px;margin:8px 14px 0;padding:8px 12px;border:1px solid ' + color + '44;border-radius:8px;background:var(--card-bg,#1a1f2e);font-size:12px';
    badge.innerHTML = '<span style="font-weight:600;font-size:13px;color:' + color + '">胜率 ' + wr.toFixed(1) + '%</span>'
        + '<div style="flex:1;max-width:120px;height:6px;background:var(--border,#333);border-radius:3px;overflow:hidden">'
        + '<div style="height:100%;width:' + barW + '%;background:' + color + ';border-radius:3px"></div></div>'
        + '<span style="color:var(--text-muted)">' + tabStats.wins + '/' + tabStats.count + '</span>'
        + (rankHtml ? '<span style="color:var(--text-muted)">| ' + rankHtml + '</span>' : '')
        + '<span style="color:var(--text-muted)">' + days + '天</span>';

    // 插入到第一个 card 之前
    var cards = outputEl.querySelector('.card') || outputEl.querySelector('.dashboard-grid');
    if (cards && cards.parentNode) {
        cards.parentNode.insertBefore(badge, cards);
    } else {
        outputEl.insertBefore(badge, outputEl.firstChild);
    }
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
        const resp = await fetch(url);
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
        const resp = await fetch(url);
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
        const resp = await fetch(url);
        const data = await resp.json();

        const items = data.stocks || data.items || [];
        // 特殊页面：情绪(score/level)、回测(weights/corr_history) — 无 items 字段
        if ((pageKey === 'sentiment' && data.score !== undefined) ||
            (pageKey === 'backtest' && data.weights !== undefined)) {
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
                html += renderBacktestDashboard(data);
                // P6: 多 tab T+1 真实回测面板 — 带 tab 切换器
                const tabs = [
                    { key: 'limit-up', label: '涨停', days: 5 },
                    { key: 'trend', label: '趋势', days: 5 },
                    { key: 'zhaban', label: '炸板', days: 5 },
                    { key: 'dtqiaoban', label: '翘板', days: 5 },
                    { key: 'reversal', label: '反转', days: 5 },
                    { key: 'sector', label: '板块', days: 5 },
                ];
                const activeTab = (typeof _btTab !== 'undefined' && _btTab) || 'limit-up';
                html += '<div style="margin:16px;padding:12px;background:var(--card-bg);border-radius:8px">'
                      + '<div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">📊 多 Tab T+1 真实回测</div>'
                      + '<div id="btTabBar" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">';
                tabs.forEach(t => {
                    const active = t.key === activeTab ? 'background:var(--accent);color:#fff' : 'background:var(--bg-secondary);color:var(--text)';
                    html += '<button class="btn" style="font-size:12px;padding:6px 12px;' + active + '" onclick="switchBacktestTab(\'' + t.key + '\',' + t.days + ')">' + t.label + '</button>';
                });
                html += '</div><div id="btTabContent">';
                try {
                    // P1.2.1: 加 60s 超时避免无限等
                    const ctrl = new AbortController();
                    const tid = setTimeout(() => ctrl.abort(), 60000);
                    const daysParam = 5;
                    const t1Resp = await fetch('/api/bt/' + activeTab + '?days=' + daysParam + '&top_n=3&capital=30000', { signal: ctrl.signal });
                    clearTimeout(tid);
                    const t1Data = await t1Resp.json();
                    html += renderT1BacktestPanel(t1Data);
                } catch (t1Err) {
                    const msg = t1Err.name === 'AbortError' ? '请求超时 (60s)' : t1Err.message;
                    html += '<div class="error-text">❌ T+1 回测加载失败: ' + escapeHtml(msg) + '</div>';
                }
                html += '</div></div>';
                // 加刷新按钮
                html += '<div class="card" style="margin:16px;padding:12px;text-align:center">'
                    + '<button class="btn btn-orange" onclick="refreshT1Backtest()" style="font-size:14px;padding:8px 20px">🔄 刷新 T+1 回测 (强制重跑, 约 1-2 分钟)</button>'
                    + '</div>';
            } else if (pageKey === 'scan-dtqiaoban') { html += renderDtqiaobanCards(items); } else if (pageKey === 'scan-zhaban') { html += renderZhabanCards(items); } else if (pageKey === 'scan-trend') { html += renderTrendCards(items); } else if (pageKey === 'scan-reversal') { html += renderReversalCards(items);
            } else {
                html += renderSimpleCards(items, pageKey);
            }
        } catch (renderErr) {
            html += `<div class="error-text">❌ 渲染错误: ${renderErr.message}</div>`;
            console.error('[Render Error]', pageKey, renderErr);
        }
        if (_pageToken === token) { output.innerHTML = html; _setCachedPage(pageKey, output.innerHTML); }
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
        const resp = await fetch(streamUrl);
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
        var resp = await fetch('/api/market-status');
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

// ─── 版本更新日志 ───
let _versionData = null;

async function loadVersion() {
    try {
        const resp = await fetch('/api/version');
        _versionData = await resp.json();
        if (_versionData.version) {
            document.getElementById('version-label').textContent = 'v' + _versionData.version;
        }
    } catch (e) { /* ignore */ }
}

function showChangelog() {
    if (!_versionData || !_versionData.changes || !_versionData.changes.length) {
        loadVersion().then(showChangelog);
        return;
    }
    var allVersions = _versionData.history || [];
    allVersions = [{ version: _versionData.version, date: _versionData.date, changes: _versionData.changes }].concat(allVersions);
    var SHOW_MAX = 5;  // 默认显示最近 5 个版本，更多需要展开

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;animation:fadeIn 0.2s ease';
    overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };

    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card,#1e293b);border:1px solid var(--border-light,#334155);border-radius:12px;padding:24px;max-width:460px;width:90%;max-height:75vh;overflow-y:auto;box-shadow:0 12px 40px rgba(0,0,0,0.5)';

    var html = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">';
    html += '<span style="font-size:20px">📦</span>';
    html += '<strong style="font-size:16px">更新日志</strong>';
    html += '<span id="modal-close" style="margin-left:auto;cursor:pointer;font-size:18px;color:var(--text-muted)">✕</span>';
    html += '</div>';

    var hasMore = allVersions.length > SHOW_MAX;
    var displayCount = hasMore ? SHOW_MAX : allVersions.length;

    for (var vi = 0; vi < displayCount; vi++) {
        var v = allVersions[vi];
        html += '<div style="margin:0 0 14px 0;padding:0 0 14px 0;border-bottom:1px solid var(--border-light,#2a3648)">';
        html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">';
        html += '<strong style="font-size:14px">v' + esc(v.version) + '</strong>';
        html += '<span style="color:var(--text-muted);font-size:11px">' + (v.date || '') + '</span>';
        html += '</div><ul style="margin:0;padding:0 0 0 16px;color:var(--text-secondary);line-height:1.9;font-size:13px">';
        for (var ci = 0; ci < v.changes.length; ci++) {
            html += '<li>' + esc(v.changes[ci]) + '</li>';
        }
        html += '</ul></div>';
    }

    if (hasMore) {
        var remaining = allVersions.length - SHOW_MAX;
        html += '<div id="ver-more" style="text-align:center;cursor:pointer;color:var(--accent,#4f8cff);font-size:13px;padding:4px 0 8px" onclick="expandOlderVersions()">展开更早 ' + remaining + ' 个版本 ▼</div>';
        // 隐藏的旧版本
        html += '<div id="ver-old" style="display:none">';
        for (var vi = displayCount; vi < allVersions.length; vi++) {
            var v = allVersions[vi];
            html += '<div style="margin:0 0 14px 0;padding:0 0 14px 0;border-bottom:1px solid var(--border-light,#2a3648)">';
            html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">';
            html += '<strong style="font-size:14px">v' + esc(v.version) + '</strong>';
            html += '<span style="color:var(--text-muted);font-size:11px">' + (v.date || '') + '</span>';
            html += '</div><ul style="margin:0;padding:0 0 0 16px;color:var(--text-secondary);line-height:1.9;font-size:13px">';
            for (var ci = 0; ci < v.changes.length; ci++) {
                html += '<li>' + esc(v.changes[ci]) + '</li>';
            }
            html += '</ul></div>';
        }
        html += '</div>';
    }

    html += '<div style="margin-top:4px;text-align:right;font-size:11px;color:var(--text-muted)">点击空白处关闭</div>';
    box.innerHTML = html;
    box.querySelector('#modal-close').onclick = function() { overlay.remove(); };
    overlay.appendChild(box);
    document.body.appendChild(overlay);
}

function expandOlderVersions() {
    var el = document.getElementById('ver-old');
    var btn = document.getElementById('ver-more');
    if (el) el.style.display = '';
    if (btn) btn.style.display = 'none';
}

// ─── 初始化 ───
document.addEventListener('DOMContentLoaded', () => {
    updateCacheStatus();
    loadMarketStatus();
    loadDashboard();
    loadVersion();

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

// T+1 真实回测刷新 (强制重跑, ~1-2 分钟)
async function refreshT1Backtest() {
    if (!confirm('T+1 回测强制重跑约 1-2 分钟, 是否继续?')) return;
    const output = document.getElementById('output');
    output.innerHTML = '<span class="loading">⏳ T+1 回测重跑中 (约 1-2 分钟) ...</span>';
    try {
        const resp = await fetch('/api/backtest/t1?days=30&top_n=3&capital=30000&refresh=true');
        const data = await resp.json();
        let html = renderBacktestDashboard(data.ok ? (await (await fetch('/api/backtest/dashboard')).json()) : {ok: true});
        html += renderT1BacktestPanel(data);
        html += '<div class="card" style="margin:16px;padding:12px;text-align:center">'
              + '<button class="btn btn-orange" onclick="refreshT1Backtest()" style="font-size:14px;padding:8px 20px">🔄 刷新 T+1 回测 (强制重跑, 约 1-2 分钟)</button>'
              + '</div>';
        output.innerHTML = html;
    } catch (err) {
        output.innerHTML = '<span class="error-text">❌ 刷新失败: ' + err.message + '</span>';
    }
}
window.refreshT1Backtest = refreshT1Backtest;