const _outputCache = {};  // 每个模块独立的输出缓存 { pageKey: htmlString }
const _watchlistKey = 'stock_watchlist';

const PAGES = {
    'watchlist':    { title: '📋 自选股' },
    'scan-limit':   { title: '🛡️ 涨停扫描',   api: '/api/scan/limit-up/cards', textApi: '/api/scan/limit-up' },
    'scan-trend':   { title: '📊 趋势扫描',   api: '/api/scan/trend/cards',   textApi: '/api/scan/trend' },
    'scan-sector':  { title: '🧩 板块热度',   api: '/api/scan/sector/cards',  textApi: '/api/scan/sector' },
    'scan-zhaban':  { title: '💥 炸板分析',   api: '/api/scan/zhaban/cards',  textApi: '/api/scan/zhaban' },
    'scan-dtqiaoban':{title:'📉 跌停翘板',   api: '/api/scan/dtqiaoban/cards',textApi: '/api/scan/dtqiaoban' },
    'indicators':   { title: '🏆 龙虎榜分析', api: '/api/indicators' },
    'community':    { title: '💬 舆情监测',   api: '/api/community' },
    'sentiment':    { title: '🌡️ 市场情绪',   api: '/api/sentiment' },
    'backtest':     { title: '⏱️ 回测系统',   api: '/api/backtest' },
};

function showProgress(text, pct) {
    const bar = document.getElementById('progress-bar');
    const fill = document.getElementById('progress-fill');
    const txt = document.getElementById('progress-text');
    bar.style.display = 'block';
    fill.style.width = (pct || 10) + '%';
    txt.textContent = text || '加载中...';
}

function hideProgress() {
    document.getElementById('progress-bar').style.display = 'none';
}

function switchPage(page) {
    const outputEl = document.getElementById('output');
    if (currentPage && outputEl) {
        _outputCache[currentPage] = outputEl.innerHTML;
    }

    currentPage = page;
    document.querySelectorAll('.nav-item').forEach(el => {
        el.classList.toggle('active', el.dataset.page === page);
    });

    const info = PAGES[page];
    document.getElementById('page-title').textContent = info.title;

    // 自选股页面通过 CSS 隐藏操作按钮
    document.body.dataset.page = page;

    // 恢复缓存 + fade-in 动画
    const modulePage = document.querySelector('.module-page');
    modulePage.style.animation = 'none';
    void modulePage.offsetHeight;
    modulePage.style.animation = 'fadeInUp 0.3s ease both';

    if (page === 'watchlist') {
        renderWatchlistPage();
        _outputCache[page] = document.getElementById('output').innerHTML;
    } else if (_outputCache[page]) {
        outputEl.innerHTML = _outputCache[page];
    } else if (info.textApi) {
        // 扫描页面 → 显示进度条并自动加载
        showProgress('正在加载...', 20);
        outputEl.innerHTML = '';
        setTimeout(() => runCurrent(), 80);
    } else {
        outputEl.innerHTML = '<span class="loading">输出结果</span>';
    }
}

async function runCurrent() {
    const info = PAGES[currentPage];
    if (!info) return;
    delete _outputCache[currentPage];
    await callApi(info.api, currentPage);
}

async function callApi(apiUrl, pageKey) {
    const output = document.getElementById('output');
    output.innerHTML = '<span class="loading">⏳ 正在扫描...</span>';
    delete _outputCache[pageKey];

    const info = PAGES[pageKey] || PAGES['scan-limit'];

    if (info.textApi) {
        await loadCardView(output, pageKey);
    } else {
        await loadTextView(output, pageKey);
    }
    _outputCache[pageKey] = document.getElementById('output').innerHTML;
}

async function loadTextView(output, pageKey) {
    const info = PAGES[pageKey];
    try {
        const resp = await fetch(info.api);
        const data = await resp.json();
        if (data.ok === false) {
            output.innerHTML = `<span class="error-text">❌ 错误：</span>\n${escapeHtml(data.error || '未知错误')}\n\n${escapeHtml(data.output || '')}`;
        } else {
            output.innerHTML = renderStyledText(data.output);
        }
    } catch (err) {
        output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
    hideProgress();
}

async function loadCardView(output, pageKey) {
    const info = PAGES[pageKey] || PAGES['scan-limit'];

    // 涨停扫描使用流式进度
    if (pageKey === 'scan-limit') {
        await loadCardViewStream(output, pageKey);
        return;
    }

    // 其他卡片页面
    try {
        const resp = await fetch(info.api);
        const data = await resp.json();

        const items = data.stocks || data.items || [];
        if (!data.ok || !items.length) {
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

        if (pageKey === 'scan-sector') {
            html += renderSectorCards(items);
        } else if (pageKey === 'scan-limit') {
            html += renderStockCards(items, data);
        } else {
            html += renderSimpleCards(items, pageKey);
        }
        output.innerHTML = html;
    } catch (err) {
        output.innerHTML = `<span class="error-text">❌ 请求失败：</span> ${escapeHtml(err.message)}`;
    }
    _outputCache[pageKey] = output.innerHTML;
    hideProgress();
    showAddAllBtn();
}

async function loadCardViewStream(output, pageKey) {
    const bar = document.getElementById('progress-bar');
    const fill = document.getElementById('progress-fill');
    const txt = document.getElementById('progress-text');

    bar.style.display = 'block';
    fill.style.width = '3%';
    txt.textContent = '正在扫描...';
    output.innerHTML = '<span class="loading">⏳ 正在扫描...</span>';

    // 各步骤加权进度（资金流占最重）
    const STEP_PCT = { '第1步':5, '第2步':10, '第3步':40, '第4步':55, '第5步':65, '第6步':80, '第7步':90 };
    let currentPct = 3;

    // 短暂延迟让浏览器渲染进度条
    await new Promise(r => setTimeout(r, 80));

    try {
        const resp = await fetch('/api/scan/limit-up/stream');
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
                            txt.textContent = msg.text;
                            // 按步骤加权估算进度
                            for (const [kw, pct] of Object.entries(STEP_PCT)) {
                                if (msg.text.includes(kw)) {
                                    currentPct = Math.max(currentPct, pct);
                                    break;
                                }
                            }
                            // 资金流子步骤修正
                            if (msg.text.includes('命中本地缓存')) currentPct = Math.max(currentPct, 48);
                            else if (msg.text.includes('同花顺资金流')) currentPct = Math.max(currentPct, 18);
                            fill.style.width = Math.min(95, currentPct) + '%';

                        } else if (msg.type === 'complete') {
                            bar.style.display = 'none';
                            const s = msg.sentiment || {};
                            let html = '';
                            if (s.level) {
                                html += '<div class="sentiment-banner">';
                                html += `📊 市场情绪: <strong>${escapeHtml(s.level)}</strong>`;
                                if (s.multiplier) html += ` ｜ 评分乘数 ×${s.multiplier}`;
                                html += '</div>';
                            }
                            html += renderStockCards(msg.stocks, msg);
                            output.innerHTML = html;
                            _outputCache[pageKey] = output.innerHTML;
                            updateWatchlistBadge();
                            showAddAllBtn();
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

async function runAll() {
    // 遍历所有页面依次运行
    const pages = ['scan-limit','scan-trend','scan-sector','scan-zhaban','scan-dtqiaoban','indicators','community','sentiment'];
    for (const key of pages) {
        location.hash = key;
        await new Promise(r => setTimeout(r, 100));
        await runCurrent();
        await new Promise(r => setTimeout(r, 300));
    }
}

function setupBackground() {
    const saved = localStorage.getItem('customBg');
    if (saved) applyBg(saved);
}

function setBgPreset(gradient) {
    localStorage.setItem('customBg', gradient);
    applyBg(gradient);
}

function setBackground(input) {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = e => {
        const dataUrl = e.target.result;
        localStorage.setItem('customBg', dataUrl);
        applyBg(dataUrl);
    };
    reader.readAsDataURL(file);
    input.value = '';
}

function clearBackground() {
    localStorage.removeItem('customBg');
    document.body.style.background = '';
    document.body.classList.remove('has-bg');
}

function applyBg(val) {
    if (val.startsWith('linear-gradient')) {
        document.body.style.background = val;
    } else {
        document.body.style.backgroundImage = `url(${val})`;
        document.body.style.backgroundSize = 'cover';
        document.body.style.backgroundPosition = 'center';
        document.body.style.backgroundAttachment = 'fixed';
    }
    document.body.classList.add('has-bg');
}

function clearOutput() {
    const el = document.getElementById('output');
    el.innerHTML = '<span class="loading">输出结果</span>';
    delete _outputCache[currentPage];
}

function exportOutput() {
    const txt = document.getElementById('output').textContent;
    navigator.clipboard.writeText(txt).then(() => {
        const btn = document.querySelector('.quick-actions .btn:nth-child(2)');
        const orig = btn.textContent;
        btn.textContent = '✅ 已复制';
        setTimeout(() => btn.textContent = orig, 1500);
    }).catch(() => {
        alert('复制失败，请手动选择文本复制');
    });
}

document.addEventListener('DOMContentLoaded', () => {
    // 自定义背景
    setupBackground();
    // 自选股徽章
    updateWatchlistBadge();
    // 加载 Dashboard
    loadDashboard();

    // 导航点击 + hash 路由
    document.querySelectorAll('.nav-item').forEach(el => {
        el.addEventListener('click', () => {
            location.hash = el.dataset.page;
        });
    });

    // 监听 hash 变化
    function onHash() {
        const page = location.hash.slice(1) || 'scan-limit';
        if (PAGES[page]) switchPage(page);
    }
    window.addEventListener('hashchange', onHash);
    // 初始加载按 hash 定位
    const initPage = location.hash.slice(1) || 'scan-limit';
    if (PAGES[initPage]) {
        document.querySelectorAll('.nav-item').forEach(el => {
            el.classList.toggle('active', el.dataset.page === initPage);
        });
        switchPage(initPage);
    }

    // Enter 键触发运行
    document.addEventListener('keydown', e => {
        if (e.key === 'Enter' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'SELECT') {
            runCurrent();
        }
    });
});