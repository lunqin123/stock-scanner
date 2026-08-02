function animateCount(el, target, duration) {
    const start = performance.now();
    function step(now) {
        const p = Math.min((now - start) / (duration || 800), 1);
        const eased = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(target * eased);
        if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
}

// ─── 市场概览骨架: 所有卡片立即渲染, 数值为占位, 数据到了逐个填充 ───
function _dashSkeletonHTML() {
    const weekdays = ['日','一','二','三','四','五','六'];
    const wd = weekdays[new Date().getDay()];
    return `
        <div class="dash-stat-card animate-fade-up stagger-1" onclick="location.hash='#scan-limit'" title="点击查看涨停扫描">
            <div class="dash-stat-icon">📅</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value accent" id="dash-date">—</div>
                <div class="dash-stat-sub" id="dash-week">星期${wd}</div>
            </div>
        </div>
        <div class="dash-stat-card animate-fade-up stagger-2" id="dash-sentiment-card" onclick="location.hash='#sentiment'" title="点击查看情绪详情">
            <div class="dash-stat-icon" id="dash-sentiment-icon">📊</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value" id="dash-sentiment" style="color:#60a5fa">⏳</div>
                <div class="dash-stat-sub" id="dash-sentiment-sub">市场情绪</div>
            </div>
        </div>
        <div class="dash-stat-card animate-fade-up stagger-3" onclick="location.hash='#scan-limit'" title="点击查看涨停列表">
            <div class="dash-stat-icon">📈</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value green"><span class="dash-count" id="dash-limit" data-target="0">—</span></div>
                <div class="dash-stat-label">涨停</div>
                <div class="dash-stat-sub" id="dash-prev-limit">上交易日 — 只</div>
            </div>
        </div>
        <div class="dash-stat-card animate-fade-up stagger-4" onclick="location.hash='#scan-dtqiaoban'" title="点击查看跌停翘板">
            <div class="dash-stat-icon">💥</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value"><span class="dash-count" id="dash-zbdt" data-target="0">—</span></div>
                <div class="dash-stat-label" id="dash-zbdt-label">炸板 — · 跌停 —</div>
                <div class="dash-stat-sub" id="dash-premium">溢价 — · 晋级 —</div>
            </div>
        </div>
        <div class="dash-stat-card sectors-card animate-fade-up stagger-5" onclick="location.hash='#scan-sector'" title="今日板块概览">
            <div class="dash-stat-icon" style="font-size:18px">🔥</div>
            <div class="dash-stat-body" style="flex:1">
                <div class="dash-sectors-wrap" id="dash-sectors"><span style="color:var(--text-muted,#8c6553);font-size:12px">板块加载中...</span></div>
            </div>
        </div>
        <!-- v2.0 第二行: 盘前信号 + 北向资金 + 市场状态 -->
        <div class="dash-stat-card animate-fade-up stagger-6" title="盘前多空信号 (A50+美股+汇率+流动性)">
            <div class="dash-stat-icon">🌅</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value" id="dash-pm" style="color:#94a3b8;font-size:18px">⏳</div>
                <div class="dash-stat-sub" id="dash-pm-sub" style="font-size:10px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">盘前信号</div>
            </div>
        </div>
        <div class="dash-stat-card animate-fade-up stagger-7" title="北向资金(外资通过沪/深股通)">
            <div class="dash-stat-icon">🌊</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value" id="dash-north" style="color:#94a3b8;font-size:18px">⏳</div>
                <div class="dash-stat-sub" id="dash-north-sub">北向外资</div>
            </div>
        </div>
        <div class="dash-stat-card animate-fade-up stagger-8" title="全市场主力资金(主力+超大单+大单净流入合计)">
            <div class="dash-stat-icon">💰</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value" id="dash-fund" style="color:#94a3b8;font-size:18px">⏳</div>
                <div class="dash-stat-sub" id="dash-fund-sub">全市场主力</div>
            </div>
        </div>
        <div class="dash-stat-card animate-fade-up stagger-9" title="市场状态分类 (北向/游资/机构/量化/防御)">
            <div class="dash-stat-icon">🎯</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value" id="dash-regime" style="color:#94a3b8;font-size:18px">⏳</div>
                <div class="dash-stat-sub" id="dash-regime-sub">市场状态</div>
            </div>
        </div>
        <div class="dash-stat-card animate-fade-up stagger-10" id="risk-card" title="市场风险评估(盘前+情绪+状态)加载中..." style="cursor:pointer" onclick="location.hash='#weights'">
            <div class="dash-stat-icon">🛡️</div>
            <div class="dash-stat-body">
                <div class="dash-stat-value" style="font-size:18px" id="risk-level">⏳</div>
                <div class="dash-stat-sub" id="risk-sub">风险评估</div>
            </div>
        </div>
    `;
}

function _dashSetCount(el, value) {
    if (!el) return;
    var target = parseInt(value) || 0;
    if (parseInt(el.dataset.target) !== target) {
        el.dataset.target = target;
        animateCount(el, target, 800);
    }
}

// 逐区块填充: 每次收到 update 都调用, 已有数据的卡片即时刷新
function _dashApplyState(s) {
    const $ = function(id) { return document.getElementById(id); };

    if (s.date) {
        var dEl = $('dash-date');
        if (dEl) dEl.textContent = String(s.date).slice(0,4) + '-' + String(s.date).slice(4,6) + '-' + String(s.date).slice(6);
    }

    if (s.sentiment) {
        var lvMap = {
            '高潮': {cls:'sentiment-hot', icon:'🔥'},
            '活跃': {cls:'sentiment-active', icon:'⚡'},
            '正常': {cls:'sentiment-normal', icon:'✅'},
            '低迷': {cls:'sentiment-low', icon:'⚠️'},
            '冰点': {cls:'sentiment-ice', icon:'❄️'},
        };
        var lv = lvMap[s.sentiment.level] || {cls:'', icon:'📊'};
        var val = $('dash-sentiment');
        if (val) {
            val.textContent = s.sentiment.level || '未知';
            val.style.color = lv.cls.includes('hot') ? '#ef4444' : lv.cls.includes('active') ? '#fbbf24' : lv.cls.includes('normal') ? '#34d399' : lv.cls.includes('low') ? '#94a3b8' : '#60a5fa';
        }
        var sub = $('dash-sentiment-sub');
        if (sub) sub.textContent = (s.sentiment.score != null ? s.sentiment.score + '/10 · ' : '') + '市场情绪';
        var icon = $('dash-sentiment-icon');
        if (icon) icon.textContent = lv.icon;
        var card = $('dash-sentiment-card');
        if (card) card.className = 'dash-stat-card animate-fade-up stagger-2 ' + lv.cls;
    }

    if (s.limit_up_count != null) _dashSetCount($('dash-limit'), s.limit_up_count);
    if (s.prev_limit_count != null) {
        var pEl = $('dash-prev-limit');
        if (pEl) pEl.textContent = '上交易日 ' + s.prev_limit_count + ' 只';
    }

    if (s.zhaban_count != null || s.dieting_count != null) {
        var zb = s.zhaban_count != null ? s.zhaban_count : 0;
        var dt = s.dieting_count != null ? s.dieting_count : 0;
        _dashSetCount($('dash-zbdt'), zb + dt);
        var zEl = $('dash-zbdt-label');
        if (zEl) zEl.textContent = '炸板 ' + zb + ' · 跌停 ' + dt;
    }
    if (s.avg_premium != null || s.promotion_rate != null) {
        var prEl = $('dash-premium');
        if (prEl) prEl.textContent = '溢价 ' + (s.avg_premium != null ? s.avg_premium + '%' : '—')
            + ' · 晋级 ' + (s.promotion_rate != null ? Math.round(s.promotion_rate * 100) + '%' : '—');
    }

    if (s.hot_sectors) {
        var secEl = $('dash-sectors');
        if (secEl) {
            var secs = (s.hot_sectors || []).slice(0, 5);
            secEl.innerHTML = secs.length
                ? secs.map(function(x) {
                    return '<a href="' + esc(x.url || '#') + '" target="_blank" class="sector-tag" title="点击查看同花顺板块详情">'
                        + esc(x.name) + ' <em>' + x.count + '</em></a>';
                  }).join('')
                : '<span style="color:var(--text-muted,#8c6553);font-size:12px">暂无板块</span>';
        }
    }

    if (s.premarket) {
        var pm = s.premarket;
        var pmColor = pm.direction === '偏多' ? '#ef4444' : (pm.direction === '偏空' ? '#22c55e' : '#94a3b8');
        var pmEl = $('dash-pm');
        if (pmEl) {
            pmEl.style.color = pmColor;
            pmEl.innerHTML = esc(pm.direction || '—') + ' <span style="font-size:12px;opacity:0.7">' + esc(pm.score != null ? pm.score + '分' : '—') + '</span>';
        }
        var pmSub = $('dash-pm-sub');
        if (pmSub) {
            pmSub.textContent = (pm.confidence || '') + '置信 · 盘前信号';
            pmSub.title = pm.summary || '';
        }
    }

    if (s.north_flow) {
        var nf = s.north_flow;
        var nfNet = nf.cumulative_net || 0;
        var nfColor = nf.signal === '偏多' ? '#ef4444' : (nf.signal === '偏空' ? '#22c55e' : '#94a3b8');
        var nEl = $('dash-north');
        if (nEl) {
            nEl.style.color = nfColor;
            nEl.innerHTML = esc(nf.direction || '—') + ' <span style="font-size:12px;opacity:0.7">' + (nfNet >= 0 ? '+' : '') + nfNet.toFixed(0) + '亿</span>';
        }
        var nSub = $('dash-north-sub');
        if (nSub) nSub.textContent = (nf.signal || '—') + ' · 北向外资';
    }

    if (s.market_fund_flow) {
        var mf = s.market_fund_flow;
        var mfNet = mf.total_net || 0;
        var mEl = $('dash-fund');
        if (mEl) {
            mEl.style.color = mfNet > 0 ? '#ef4444' : (mfNet < 0 ? '#22c55e' : '#94a3b8');
            mEl.innerHTML = esc(mf.direction || '—') + ' <span style="font-size:12px;opacity:0.7">' + (mfNet > 0 ? '+' : '') + mfNet.toFixed(0) + '亿</span>';
        }
        var mSub = $('dash-fund-sub');
        if (mSub) mSub.textContent = '全市场主力';
    }

    if (s.regime) {
        var rg = s.regime;
        var rgColor = rg.position_advice > 1 ? '#ef4444' : (rg.position_advice >= 0.8 ? '#f59e0b' : (rg.position_advice >= 0.5 ? '#94a3b8' : '#22c55e'));
        var rEl = $('dash-regime');
        if (rEl) {
            rEl.style.color = rgColor;
            rEl.textContent = rg.label || '—';
        }
        var rSub = $('dash-regime-sub');
        if (rSub) rSub.textContent = '仓位 ' + (rg.position_advice != null ? Math.round(rg.position_advice * 100) + '%' : '—') + ' · 市场状态';
    }
}

// 风险评估独立异步加载 (不阻塞其他卡片)
function _dashLoadRisk() {
    fetch('/api/risk/assessment').then(function(r) { return r.json(); }).then(function(rd) {
        if (!rd || !rd.ok) return;
        var riskEl = document.getElementById('risk-level');
        var subEl = document.getElementById('risk-sub');
        if (!riskEl) return;
        var colors = {'低':'#22c55e','中':'#fbbf24','高':'#f59e0b','极高':'#ef4444'};
        var icons = {'低':'✅','中':'📊','高':'⚡','极高':'🔴'};
        riskEl.textContent = (icons[rd.risk_level]||'') + ' ' + (rd.risk_level||'—');
        riskEl.style.color = colors[rd.risk_level]||'#94a3b8';
        if (subEl) subEl.textContent = (rd.risk_score||'?') + '/10 · 风险 ' + rd.risk_level;
        var card = document.getElementById('risk-card');
        if (card) card.title = rd.advice || '';
    }).catch(function(){});
}

async function loadDashboard(force) {
    const bar = document.getElementById('dashboard');
    if (!bar) return;
    // 先画骨架: 所有卡片立即出现, 快的区块到了逐个填充, 不再等最慢的
    bar.innerHTML = _dashSkeletonHTML();
    _dashLoadRisk();

    var state = {};
    var url = '/api/dashboard/stream' + (force ? '?refresh=1' : '') + '&_t=' + Date.now();
    try {
        const resp = await fetch(url, { cache: 'no-store' });
        if (!resp.ok || !resp.body) throw new Error('stream unavailable');
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        let finished = false;
        while (!finished) {
            const r = await reader.read();
            if (r.done) break;
            buf += dec.decode(r.value, {stream: true});
            const parts = buf.split('\n\n');
            buf = parts.pop() || '';
            for (const part of parts) {
                for (const line of part.split('\n')) {
                    if (!line.startsWith('data: ')) continue;
                    let msg;
                    try { msg = JSON.parse(line.slice(6)); } catch (_) { continue; }
                    if (msg.type === 'meta') {
                        state.date = msg.date;
                        _dashApplyState(state);
                    } else if (msg.type === 'update') {
                        Object.assign(state, msg.data || {});
                        _dashApplyState(state);
                    } else if (msg.type === 'complete') {
                        finished = true;
                    }
                }
            }
        }
    } catch (e) {
        console.warn('[dashboard] SSE 失败, 回退 JSON:', e.message);
        try {
            const resp = await fetch('/api/dashboard' + (force ? '?refresh=1' : ''), { cache: 'no-store' });
            const d = await resp.json();
            if (!d || !d.ok) throw new Error('no data');
            Object.assign(state, d);
            _dashApplyState(state);
        } catch (e2) {
            bar.innerHTML = '<div class="dash-loading" style="cursor:pointer" onclick="loadDashboard(true)">⚠️ 数据加载失败 · 点击重试</div>';
        }
    }
}
