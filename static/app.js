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
const _navItems = () => document.querySelectorAll('.nav-item');

let currentPage = '';
const _outputCache = {};

const PAGES = {
    'scan-limit':   { title: '🛡️ 涨停扫描',   api: '/api/scan/limit-up/cards', textApi: '/api/scan/limit-up' },
    'scan-trend':   { title: '📊 趋势扫描',   api: '/api/scan/trend/cards',   textApi: '/api/scan/trend' },
    'scan-sector':  { title: '🧩 板块热度',   api: '/api/scan/sector/cards',  textApi: '/api/scan/sector' },
    'scan-zhaban':  { title: '💥 炸板分析',   api: '/api/scan/zhaban/cards',  textApi: '/api/scan/zhaban' },
    'scan-dtqiaoban':{title:'📉 跌停翘板',   api: '/api/scan/dtqiaoban/cards',textApi: '/api/scan/dtqiaoban' },
    'indicators':   { title: '🏆 龙虎榜分析', api: '/api/indicators/cards', textApi: '/api/indicators', streamApi: '/api/indicators/stream' },
    'community':    { title: '💬 舆情监测',   api: '/api/community/cards', textApi: '/api/community', streamApi: '/api/community/stream' },
    'sentiment':    { title: '🌡️ 市场情绪',   api: '/api/sentiment/cards', textApi: '/api/sentiment' },
    'backtest':     { title: '⏱️ 回测系统',   api: '/api/backtest' },
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
            _outputCache[currentPage] = outputEl.innerHTML;
        }

        currentPage = page;
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

        if (_outputCache[page]) {
            outputEl.innerHTML = _outputCache[page];
        } else if (info.api) {
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

async function runCurrent() {
    const info = PAGES[currentPage];
    if (!info) return;
    delete _outputCache[currentPage];
    var url = info.api + '?principal=' + getPrincipal();
    await callApi(url, currentPage);
}

async function refreshCurrent() {
    const info = PAGES[currentPage];
    if (!info) return;
    delete _outputCache[currentPage];
    var url = info.api + '?refresh=1&principal=' + getPrincipal();
    await callApi(url, currentPage);
}

async function callApi(apiUrl, pageKey) {
    const output = _dom.output();
    delete _outputCache[pageKey];

    const info = PAGES[pageKey] || PAGES['scan-limit'];

    if (info.textApi) {
        await loadCardView(output, pageKey, apiUrl);
    } else if (info.streamApi) {
        await loadTextViewStream(output, pageKey, apiUrl);
    } else {
        await loadTextView(output, pageKey, apiUrl);
    }
    _outputCache[pageKey] = output.innerHTML;
}

async function loadTextView(output, pageKey, apiUrl) {
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
            output.innerHTML = `<span class="error-text">❌ 错误：</span>\n${escapeHtml(data.error || '未知错误')}\n\n${escapeHtml(data.output || '')}`;
        } else {
            output.innerHTML = renderStyledText(data.output);
        }
    } catch (err) {
        clearInterval(estInterval);
        output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
    hideProgress();
}

// ─── 文本流式加载（龙虎榜/舆情，SSE 实时进度） ───
async function loadTextViewStream(output, pageKey, apiUrl) {
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
                            output.innerHTML = msg.output
                                ? renderStyledText(msg.output)
                                : '<span class="loading">暂无数据</span>';
                            _outputCache[pageKey] = output.innerHTML;
                            return;
                        } else if (msg.type === 'error') {
                            if (bar) bar.style.display = 'none';
                            output.innerHTML = `<span class="error-text">❌ ${escapeHtml(msg.text)}</span>`;
                            _outputCache[pageKey] = output.innerHTML;
                            return;
                        }
                    } catch (_) {}
                }
            }
        }
        if (bar) bar.style.display = 'none';
        output.innerHTML = '<span class="loading">连接中断</span>';

    } catch (err) {
        if (bar) bar.style.display = 'none';
        output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
}

async function loadCardView(output, pageKey, apiUrl) {
    const info = PAGES[pageKey] || PAGES['scan-limit'];
    const url = apiUrl || info.api;

    if (pageKey === 'scan-limit') {
        await loadCardViewStream(output, pageKey, apiUrl);
        return;
    }

    try {
        const resp = await fetch(url);
        const data = await resp.json();

        const items = data.stocks || data.items || [];
        // 市场情绪数据结构特殊（无 items，有 score/level）
        if (pageKey === 'sentiment' && data.score !== undefined) {
            // handled below
        } else if (!data.ok || !items.length) {
            output.innerHTML = '<span class="loading">暂无数据</span>';
            return;
        }

        const s = data.sentiment || {};
        let html = '';
        if (s.level) {
            html += '<div class="sentiment-banner">';
            html += `📊 市场情绪: <strong>${s.level || '未知'}</strong>`;
            if (s.multiplier) html += ` ｜ 评分乘数 ×${s.multiplier}`;
            html += '</div>';
        }
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
            } else if (pageKey === 'scan-dtqiaoban') { html += renderDtqiaobanCards(items); } else if (pageKey === 'scan-zhaban') { html += renderZhabanCards(items); } else if (pageKey === 'scan-trend') { html += renderTrendCards(items);
            } else {
                html += renderSimpleCards(items, pageKey);
            }
        } catch (renderErr) {
            html += `<div class="error-text">❌ 渲染错误: ${renderErr.message}</div>`;
            console.error('[Render Error]', pageKey, renderErr);
        }
        output.innerHTML = html;
    } catch (err) {
        output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
    _outputCache[pageKey] = output.innerHTML;
    hideProgress();
}

// ─── SSE 流式加载（防抖 + RAF 优化） ───
async function loadCardViewStream(output, pageKey, apiUrl) {
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
        var streamUrl = '/api/scan/limit-up/stream';
        var params = [];
        if (apiUrl && apiUrl.indexOf('refresh=1') >= 0) params.push('refresh=1');
        var pMatch = apiUrl && apiUrl.match(/principal=(\d+)/);
        if (pMatch) params.push('principal=' + pMatch[1]);
        if (params.length) streamUrl += '?' + params.join('&');
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
                            const s = msg.sentiment || {};
                            let html = '';
                            if (s.level) {
                                html += '<div class="sentiment-banner">';
                                html += `📊 市场情绪: <strong>${escapeHtml(s.level)}</strong>`;
                                if (s.multiplier) html += ` ｜ 评分乘数 ×${s.multiplier}`;
                                html += '</div>';
                            }
                            if (fet) html += `<div style="font-size:11px;color:var(--text-muted);padding:4px 14px 0;text-align:right">数据拉取: ${escapeHtml(fet)}</div>`;
                            html += renderStockCards(msg.stocks, msg);

                            // 用 requestAnimationFrame + 微任务分离 DOM 操作和渲染
                            requestAnimationFrame(() => {
                                output.innerHTML = html;
                                _outputCache[pageKey] = output.innerHTML;
                            });
                            return;

                        } else if (msg.type === 'error') {
                            bar.style.display = 'none';
                            output.innerHTML = `<span class="error-text">❌ ${escapeHtml(msg.text)}</span>`;
                            _outputCache[pageKey] = output.innerHTML;
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


// ─── 背景管理 ───
function setupBackground() { const s = localStorage.getItem('customBg'); if (s) applyBg(s); }
function setBgPreset(gradient) { localStorage.setItem('customBg', gradient); applyBg(gradient); }
function setBackground(input) {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = e => { const r = e.target.result; localStorage.setItem('customBg', r); applyBg(r); };
    reader.readAsDataURL(file);
    input.value = '';
}
function clearBackground() { localStorage.removeItem('customBg'); document.body.style.background = ''; document.body.classList.remove('has-bg'); }
function applyBg(val) {
    if (val.startsWith('linear-gradient')) document.body.style.background = val;
    else { document.body.style.backgroundImage = `url(${val})`; document.body.style.backgroundSize = 'cover'; document.body.style.backgroundPosition = 'center'; document.body.style.backgroundAttachment = 'fixed'; }
    document.body.classList.add('has-bg');
}
function clearOutput() { const el = _dom.output(); el.innerHTML = '<span class="loading">输出结果</span>'; delete _outputCache[currentPage]; }
function exportOutput() {
    const txt = _dom.output().textContent;
    if (navigator.clipboard) {
        navigator.clipboard.writeText(txt).then(() => {
            const btn = document.querySelector('.quick-actions .btn:nth-child(2)');
            const orig = btn.textContent;
            btn.textContent = '✅ 已复制';
            setTimeout(() => btn.textContent = orig, 1500);
        }).catch(() => alert('复制失败'));
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
    const pages = ['scan-limit','scan-trend','scan-sector','scan-zhaban','scan-dtqiaoban','indicators','community','sentiment'];
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
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;animation:fadeIn 0.2s ease';
    overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };

    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card,#1e293b);border:1px solid var(--border-light,#334155);border-radius:12px;padding:24px;max-width:420px;width:90%;max-height:70vh;overflow-y:auto;box-shadow:0 12px 40px rgba(0,0,0,0.5)';

    var html = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">';
    html += '<span style="font-size:20px">📦</span>';
    html += '<strong style="font-size:16px">更新日志 v' + esc(_versionData.version) + '</strong>';
    html += '<span style="color:var(--text-muted);font-size:12px">' + (_versionData.date || '') + '</span>';
    html += '<span id="modal-close" style="margin-left:auto;cursor:pointer;font-size:18px;color:var(--text-muted)">✕</span>';
    html += '</div><ul style="margin:0;padding:0 0 0 18px;color:var(--text-secondary);line-height:2;font-size:13px">';
    for (var i = 0; i < _versionData.changes.length; i++) {
        html += '<li>' + esc(_versionData.changes[i]) + '</li>';
    }
    html += '</ul><div style="margin-top:12px;text-align:right;font-size:11px;color:var(--text-muted)">点击空白处关闭</div>';
    box.innerHTML = html;

    box.querySelector('#modal-close').onclick = function() { overlay.remove(); };

    overlay.appendChild(box);
    document.body.appendChild(overlay);
}

// ─── 初始化 ───
document.addEventListener('DOMContentLoaded', () => {
    setupBackground();
    loadMarketStatus();
    loadDashboard();
    loadVersion();

    document.querySelectorAll('.nav-item').forEach(el => {
        el.addEventListener('click', () => { location.hash = el.dataset.page; closeSidebar(); });
    });

    function onHash() {
        const page = location.hash.slice(1) || 'scan-limit';
        if (PAGES[page]) switchPage(page);
    }
    window.addEventListener('hashchange', onHash);

    const initPage = location.hash.slice(1) || 'scan-limit';
    if (PAGES[initPage]) {
        _navItems().forEach(el => el.classList.toggle('active', el.dataset.page === initPage));
        switchPage(initPage);
    }

    document.addEventListener('keydown', e => {
        if (e.key === 'Enter' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'SELECT')
            runCurrent();
    });
});