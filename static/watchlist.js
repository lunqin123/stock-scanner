const _watchlistKey = 'stock_watchlist';


function getWatchlist() {
    try { return JSON.parse(localStorage.getItem(_watchlistKey)) || []; }
    catch { return []; }
}

function saveWatchlist(list) {
    localStorage.setItem(_watchlistKey, JSON.stringify(list));
}

function toggleWatchlist(code, name) {
    let list = getWatchlist();
    const idx = list.findIndex(s => s.code === code);
    if (idx >= 0) list.splice(idx, 1);
    else list.unshift({ code, name, addedAt: new Date().toISOString().slice(0,10) });
    saveWatchlist(list);
    // 刷新侧边栏计数
    document.getElementById('watchlist-count').textContent = list.length;
    return idx < 0; // true=已添加, false=已移除
}

function isWatched(code) {
    return getWatchlist().some(s => s.code === code);
}

function updateWatchlistBadge() {
    const el = document.getElementById('watchlist-count');
    if (el) el.textContent = getWatchlist().length;
}

function toggleWatchBtn(el) {
    const code = el.dataset.code;
    const name = el.dataset.name;
    const added = toggleWatchlist(code, name);
    el.textContent = added ? '✓' : '+';
    el.classList.toggle('watched', added);
}

function renderWatchlistPage() {
    const output = document.getElementById('output');
    const list = getWatchlist();
    if (!list.length) {
        output.innerHTML = '<span class="loading">自选股为空 — 在扫描结果中点击「+」添加</span>';
        return;
    }
    let html = '<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">';
    html += '<button class="btn btn-sm" onclick="copyAllWatchlist()">📋 一键复制全部</button>';
    html += '<button class="btn btn-orange btn-sm" onclick="clearWatchlist()">🗑️ 清空自选</button>';
    html += '</div><div class="card-list">';
    for (const s of list) {
        const url = `https://stockpage.10jqka.com.cn/${s.code}/`;
        html += `<div class="stock-card" style="cursor:default;animation:none">`;
        html += `<div class="card-header">`;
        html += `<span class="card-rank">☆</span>`;
        html += `<span class="card-name">${escapeHtml(s.name)}</span>`;
        html += `<span class="card-code">${escapeHtml(s.code)}</span>`;
        html += `<span style="font-size:11px;color:var(--text-muted)">${s.addedAt || ''}</span>`;
        html += `<span style="margin-left:auto;display:flex;gap:6px">`;
        html += `<button class="btn btn-sm" onclick="event.stopPropagation();copyText('${s.code} ${s.name}')">📋</button>`;
        html += `<button class="btn btn-orange btn-sm" onclick="event.stopPropagation();removeWatchlistStock('${s.code}')">✕</button>`;
        html += `<a href="${url}" target="_blank" class="btn btn-sm" style="text-decoration:none">🔗</a>`;
        html += `</span></div></div>`;
    }
    html += '</div>';
    output.innerHTML = html;
}

function copyAllWatchlist() {
    const list = getWatchlist();
    if (!list.length) return;
    const text = list.map(s => `${s.code} ${s.name}`).join('\n');
    copyText(text);
}

function copyText(text) {
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.querySelector('.quick-actions .btn:first-child') || document.querySelector('.btn-sm');
        if (btn) { const t = btn.textContent; btn.textContent = '✅ 已复制'; setTimeout(() => btn.textContent = t, 1500); }
    }).catch(() => alert('复制失败'));
}

function removeWatchlistStock(code) {
    let list = getWatchlist().filter(s => s.code !== code);
    saveWatchlist(list);
    updateWatchlistBadge();
    renderWatchlistPage();
}

function addAllToWatchlist() {
    const btns = document.querySelectorAll('#output .watchlist-btn:not(.watched)');
    if (!btns.length) { return; }
    let list = getWatchlist();
    const existing = new Set(list.map(s => s.code));
    btns.forEach(el => {
        const code = el.dataset.code;
        if (!existing.has(code)) {
            list.unshift({ code, name: el.dataset.name, addedAt: new Date().toISOString().slice(0,10) });
            existing.add(code);
        }
        el.textContent = '✓';
        el.classList.add('watched');
    });
    saveWatchlist(list);
    updateWatchlistBadge();
}

function showAddAllBtn() {
    const el = document.getElementById('btn-add-all');
    if (el) el.style.display = document.querySelectorAll('#output .watchlist-btn').length ? '' : 'none';
}

function clearWatchlist() {
    if (!confirm('确定清空所有自选股？')) return;
    saveWatchlist([]);
    updateWatchlistBadge();
    renderWatchlistPage();
}