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

async function loadDashboard(force) {
    const bar = document.getElementById('dashboard');
    try {
        const url = '/api/dashboard' + (force ? '?refresh=1' : '');
        const resp = await fetch(url);
        const d = await resp.json();
        if (!d.ok) throw new Error('no data');

        const s = d.sentiment || {};
        const lvMap = {
            '高潮': {cls:'sentiment-hot', icon:'🔥'},
            '活跃': {cls:'sentiment-active', icon:'⚡'},
            '正常': {cls:'sentiment-normal', icon:'✅'},
            '低迷': {cls:'sentiment-low', icon:'⚠️'},
            '冰点': {cls:'sentiment-ice', icon:'❄️'},
        };
        const lv = lvMap[s.level] || {cls:'', icon:'📊'};
        const sectors = (d.hot_sectors || []).slice(0, 5);
        const sntCls = lv.cls;

        const weekdays = ['日','一','二','三','四','五','六'];
        const wd = weekdays[new Date().getDay()];

        // v2.0 新增数据
        const pm = d.premarket || {};
        const nf = d.north_flow || {};
        const mf = d.market_fund_flow || {};
        const rg = d.regime || {};

        // 盘前方向颜色
        const pmColor = pm.direction === '偏多' ? '#ef4444' : (pm.direction === '偏空' ? '#22c55e' : '#94a3b8');
        // 北向方向
        const nfNet = nf.cumulative_net || 0;
        const nfColor = nf.signal === '偏多' ? '#ef4444' : (nf.signal === '偏空' ? '#22c55e' : '#94a3b8');
        const nfSign = nfNet >= 0 ? '+' : '';
        // 市场状态颜色
        const rgColor = rg.position_advice > 1 ? '#ef4444' : (rg.position_advice >= 0.8 ? '#f59e0b' : (rg.position_advice >= 0.5 ? '#94a3b8' : '#22c55e'));

        bar.innerHTML = `
            <div class="dash-stat-card animate-fade-up stagger-1" onclick="location.hash=\'#scan-limit\'" title="点击查看涨停扫描">
                <div class="dash-stat-icon">📅</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value accent">${d.date.slice(0,4)}-${d.date.slice(4,6)}-${d.date.slice(6)}</div>
                    <div class="dash-stat-sub">星期${wd}</div>
                </div>
            </div>
            <div class="dash-stat-card animate-fade-up stagger-2 ${sntCls}" onclick="location.hash=\'#sentiment\'" title="点击查看情绪详情">
                <div class="dash-stat-icon">${lv.icon}</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value" style="color:${lv.cls.includes('hot')?'#ef4444':lv.cls.includes('active')?'#fbbf24':lv.cls.includes('normal')?'#34d399':lv.cls.includes('low')?'#94a3b8':'#60a5fa'}">${s.level}</div>
                    <div class="dash-stat-sub">${s.score}/10 · 市场情绪</div>
                </div>
            </div>
            <div class="dash-stat-card animate-fade-up stagger-3" onclick="location.hash=\'#scan-limit\'" title="点击查看涨停列表">
                <div class="dash-stat-icon">📈</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value green"><span class="dash-count" data-target="${d.limit_up_count||0}">0</span></div>
                    <div class="dash-stat-label">涨停</div>
                    <div class="dash-stat-sub">上交易日 ${d.prev_limit_count||'?'} 只</div>
                </div>
            </div>
            <div class="dash-stat-card animate-fade-up stagger-4" onclick="location.hash=\'#scan-dtqiaoban\'" title="点击查看跌停翘板">
                <div class="dash-stat-icon">💥</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value"><span class="dash-count" data-target="${(d.zhaban_count||0)+(d.dieting_count||0)}">0</span></div>
                    <div class="dash-stat-label">炸板 ${d.zhaban_count||0} · 跌停 ${d.dieting_count||0}</div>
                    <div class="dash-stat-sub">溢价 ${d.avg_premium != null ? d.avg_premium + '%' : '?'} · 晋级 ${d.promotion_rate != null ? Math.round(d.promotion_rate*100) + '%' : '?'}</div>
                </div>
            </div>
            <div class="dash-stat-card sectors-card animate-fade-up stagger-5" onclick="location.hash=\'#scan-sector\'" title="今日板块概览">
                <div class="dash-stat-icon" style="font-size:18px">🔥</div>
                <div class="dash-stat-body" style="flex:1">
                    <div class="dash-sectors-wrap">
                        ${sectors.map(s => `<a href="${esc(s.url||'#')}" target="_blank" class="sector-tag" title="点击查看同花顺板块详情">${s.name} <em>${s.count}</em></a>`).join('')}
                    </div>
                </div>
            </div>
            <!-- v2.0 第二行: 盘前信号 + 北向资金 + 市场状态 -->
            <div class="dash-stat-card animate-fade-up stagger-6" title="盘前多空信号 (A50+美股+汇率+流动性)">
                <div class="dash-stat-icon">🌅</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value" style="color:${pmColor};font-size:18px">${pm.direction||'—'} <span style="font-size:12px;opacity:0.7">${pm.score||'—'}分</span></div>
                    <div class="dash-stat-sub" style="font-size:10px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(pm.summary||'')}">${pm.confidence||''}置信 · 盘前信号</div>
                </div>
            </div>
            <div class="dash-stat-card animate-fade-up stagger-7" title="北向资金(外资通过沪/深股通)">
                <div class="dash-stat-icon">🌊</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value" style="color:${nfColor};font-size:18px">${nf.direction||'—'} <span style="font-size:12px;opacity:0.7">${nfSign}${nfNet.toFixed(0)}亿</span></div>
                    <div class="dash-stat-sub">${nf.signal||'—'} · 北向外资</div>
                </div>
            </div>
            <div class="dash-stat-card animate-fade-up stagger-8" title="全市场主力资金(主力+超大单+大单净流入合计)">
                <div class="dash-stat-icon">💰</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value" style="color:${mf.total_net > 0 ? '#ef4444' : mf.total_net < 0 ? '#22c55e' : '#94a3b8'};font-size:18px">${mf.direction||'—'} <span style="font-size:12px;opacity:0.7">${mf.total_net > 0 ? '+' : ''}${(mf.total_net||0).toFixed(0)}亿</span></div>
                    <div class="dash-stat-sub">全市场主力</div>
                </div>
            </div>
            <div class="dash-stat-card animate-fade-up stagger-9" title="市场状态分类 (北向/游资/机构/量化/防御)">
                <div class="dash-stat-icon">🎯</div>
                <div class="dash-stat-body">
                    <div class="dash-stat-value" style="color:${rgColor};font-size:18px">${rg.label||'—'}</div>
                    <div class="dash-stat-sub">仓位 ${rg.position_advice ? Math.round(rg.position_advice*100)+'%' : '—'} · 市场状态</div>
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

        // 异步加载风险等级
        fetch('/api/risk/assessment').then(r=>r.json()).then(rd=>{
            if (!rd.ok) return;
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

        // 触发数字滚动
        bar.querySelectorAll('.dash-count').forEach(el => {
            animateCount(el, parseInt(el.dataset.target), 800);
        });

    } catch (e) {
        bar.innerHTML = '<div class="dash-loading" style="cursor:pointer" onclick="loadDashboard(true)">⚠️ 数据加载失败 · 点击重试</div>';
    }
}