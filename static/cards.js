/* ═══════════════════════════════════════
   选股扫描器 — 卡片渲染（性能优化版）
   ═══════════════════════════════════════ */

// ─── 转义（仅对用户数据） ───
function esc(s) {
    if (!s) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ─── 颜色辅助 ───
const RING_COLORS = {
    high: '#34d399',
    mid:  '#fbbf24',
    low:  '#fb923c',
    poor: '#f87171',
};
function ringColor(s) { return s >= 80 ? RING_COLORS.high : s >= 70 ? RING_COLORS.mid : s >= 55 ? RING_COLORS.low : RING_COLORS.poor; }
function barColor(pct) { return pct >= 70 ? 'high' : pct >= 40 ? 'mid' : 'low'; }

// ─── 评分圆环（CSS conic-gradient 替代 SVG，DOM 轻 10 倍） ───
function scoreRingHTML(score) {
    const pct = Math.min(100, Math.max(0, score));
    const col = ringColor(score);
    return `<div class="sr-wrap"><div class="sr-ring" style="background:conic-gradient(${col} ${pct}%, transparent ${pct}%)"><div class="sr-inner">${score}</div></div></div>`;
}

// ─── 股票卡片渲染 ───
function renderStockCards(stocks, data) {
    const parts = ['<div class="card-list">'];
    for (let i = 0; i < stocks.length; i++) {
        const s = stocks[i];
        const col = ringColor(s.total_score);
        const msgn = s.net_money >= 0 ? '+' : '';
        const st = s.seal_time || '0000';
        const watched = isWatched(s.code);

        const bars = [
            [s.seal_score, 32],
            [s.money_score, 20],
            [s.sector_score, 22],
            [s.tech_score, 10],
            [s.history_score, 5],
            [s.community_score, 7],
        ];

        let barsHTML = '';
        for (const [sc, mx] of bars) {
            const pct = Math.min(100, (sc / mx) * 100);
            barsHTML += `<div class="bar-row"><span class="bar-label">${barLabel(mx)}</span><div class="bar-track"><div class="bar-fill ${barColor(pct)}" style="width:${pct}%"></div></div><span class="bar-val">${sc}</span></div>`;
        }

        const sentSign = s.base_score > 0 ? (s.total_score > s.base_score ? '+' : '-') : '';

        const tags = analyzeTags(s);

        parts.push(
            `<a href="https://stockpage.10jqka.com.cn/${s.code}/" target="_blank" class="stock-card">`,
            `<div class="card-header">`,
            `<span class="card-rank">#${s.rank}</span>`,
            `<span class="card-name">${esc(s.name)}</span>`,
            `<span class="card-code">${s.code}</span>`,
            `<span class="watchlist-btn ${watched?'watched':''}" data-code="${s.code}" data-name="${esc(s.name)}" onclick="toggleWatchBtn(this);return false">${watched?'✓':'+'}</span>`,
            scoreRingHTML(s.total_score),
            `</div>`,
            `<div class="card-body"><div class="card-bars">${barsHTML}</div>`,
            `<div class="card-info">`,
            `<div class="info-row"><span class="label">基础分</span><span class="value">${s.base_score}</span></div>`,
            `<div class="info-row"><span class="label">情绪加成</span><span class="value ${sentSign === '+' ? 'green' : sentSign === '-' ? 'red' : ''}">${sentSign || '='}</span></div>`,
            `<div class="info-row"><span class="label">净流入</span><span class="value ${s.net_money>=0?'green':'red'}">${msgn}${esc(s.net_money_str)}</span></div>`,
            `<div class="info-row"><span class="label">换手率</span><span class="value">${esc(s.turnover)}%</span></div>`,
            `<div class="info-row"><span class="label">封板时间</span><span class="value">${st.slice(0,2)}:${st.slice(2)}</span></div>`,
            `</div></div>`,
            `<div class="card-analysis">${tags.map(t => `<span class="tag ${t[1]}">${esc(t[0])}</span>`).join('')}</div>`,
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 板块标签助记 ───
function barLabel(max) {
    return max === 32 ? '涨停强度' : max === 20 ? '资金面' : max === 22 ? '板块热度' : max === 10 ? '量价关系' : max === 5 ? '历史股性' : '舆情评分';
}

// ─── 标签分析 ───
function analyzeTags(s) {
    const t = [];
    const st = parseInt(s.seal_time || '0');
    if (st <= 930) t.push(['开盘秒板','tag-green']);
    else if (st <= 1000) t.push(['早盘板','tag-blue']);
    else if (st <= 1100) t.push(['上午板','tag-blue']);
    else if (st <= 1400) t.push(['午后板','tag-yellow']);
    else t.push(['尾盘偷袭','tag-red']);

    if (s.net_money > 1e8) t.push(['主力强','tag-green']);
    else if (s.net_money > 5e7) t.push(['资金正','tag-blue']);
    else if (s.net_money < 0) t.push(['资金流出','tag-red']);

    const tr = parseFloat(s.turnover);
    if (tr > 25) t.push(['爆量','tag-yellow']);
    else if (tr > 15) t.push(['高换手','tag-yellow']);
    else if (tr < 5 && tr > 0) t.push(['缩量','tag-green']);

    if (s.sector_score >= 10) t.push(['板块龙','tag-green']);
    if (s.money_score >= 12) t.push(['资金龙','tag-green']);
    if (s.seal_score >= 22) t.push(['封板强','tag-green']);
    return t;
}

// ─── 舆情卡片（含评分解析 + 行情背景） ───
function renderCommunityCards(items) {
    const parts = ['<div class="card-list">'];
    for (const item of items) {
        const cs = item.comment_score || 0;
        var col = cs >= 80 ? RING_COLORS.high : cs >= 70 ? RING_COLORS.mid : cs >= 55 ? RING_COLORS.low : RING_COLORS.poor;
        var ctx = item.context || {};
        var st = ctx.seal_time || '';

        var tags = [];
        if (cs >= 80) tags.push(['评分优秀','tag-green']);
        else if (cs >= 70) tags.push(['评分良好','tag-blue']);
        else if (cs >= 60) tags.push(['评分一般','tag-yellow']);
        else tags.push(['评分偏低','tag-red']);
        if (item.guba_rank && item.guba_rank <= 3) tags.push(['热榜TOP3','tag-green']);
        if (item.institution && item.institution > 0.5) tags.push(['机构关注','tag-blue']);
        if (ctx.consecutive >= 2) tags.push([ctx.consecutive + '连板','tag-green']);
        if (ctx.industry) tags.push([esc(ctx.industry),'tag-blue']);

        // 新闻项
        var newsHtml = '';
        if (item.news && item.news.length) {
            newsHtml = '<div class="card-analysis" style="border-top:none;padding-top:4px;gap:3px;flex-direction:column">';
            for (var ni = 0; ni < item.news.length; ni++) {
                var n = item.news[ni];
                var nurl = n.url || 'https://www.10jqka.com.cn/#/search/' + encodeURIComponent(n.title);
                newsHtml += '<a href="' + esc(nurl) + '" target="_blank" style="font-size:12px;color:var(--text-secondary);text-decoration:none;padding:2px 0" onclick="event.stopPropagation()">';
                if (n.source) newsHtml += '<span style="color:var(--text-muted);font-size:10px">[' + esc(n.source) + ']</span> ';
                newsHtml += esc(n.title.length > 50 ? n.title.slice(0,50) + '...' : n.title) + '</a>';
            }
            newsHtml += '</div>';
        }

        // 评分条
        var barsHtml = '';
        barsHtml += '<div class="bar-row"><span class="bar-label">综合评分</span><div class="bar-track"><div class="bar-fill ' + (cs >= 70 ? 'high' : cs >= 50 ? 'mid' : 'low') + '" style="width:' + Math.min(100, cs || 0) + '%"></div></div><span class="bar-val" style="color:' + col + '">' + (cs ? cs.toFixed(2) : 'N/A') + '</span></div>';
        if (item.attention) {
            barsHtml += '<div class="bar-row"><span class="bar-label">关注度</span><div class="bar-track"><div class="bar-fill mid" style="width:' + Math.min(100, item.attention) + '%"></div></div><span class="bar-val">' + item.attention + '</span></div>';
        }
        if (item.institution) {
            barsHtml += '<div class="bar-row"><span class="bar-label">机构参与</span><div class="bar-track"><div class="bar-fill high" style="width:' + Math.min(100, item.institution * 100) + '%"></div></div><span class="bar-val">' + (item.institution * 100).toFixed(0) + '%' + '</span></div>';
        }

        // 行情背景
        var ctxHtml = '';
        if (ctx.industry || ctx.consecutive || st) {
            ctxHtml += '<div style="font-size:12px;color:var(--text-muted);padding:4px 0 0">';
            if (ctx.industry) ctxHtml += '所属板块: ' + esc(ctx.industry) + ' ';
            if (ctx.consecutive >= 1) ctxHtml += ctx.consecutive + '连板 ';
            if (ctx.turnover) ctxHtml += '换手' + ctx.turnover.toFixed(1) + '% ';
            if (st) ctxHtml += '封板' + st.slice(0,2) + ':' + st.slice(2);
            ctxHtml += '</div>';
        }

        // 评分解析
        var noteHtml = '';
        if (item.score_note) {
            noteHtml = '<div style="font-size:12px;color:var(--text-secondary);padding:4px 0">' + esc(item.score_note) + '</div>';
        }

        // 关注原因
        var reasonHtml = '';
        if (item.reasons && item.reasons.length) {
            reasonHtml = '<div style="font-size:12px;color:var(--text-muted);padding:4px 0">关注原因: ';
            reasonHtml += item.reasons.map(function(r) { return '<span class="sector-tag" style="font-size:11px;padding:1px 6px;margin:0 2px">' + esc(r) + '</span>'; }).join('');
            reasonHtml += '</div>';
        }

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">' + item.code + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            cs ? '<span class="card-score" style="color:' + col + '">' + cs.toFixed(2) + '</span>' : '',
            '</div>',
            '<div class="card-body"><div class="card-bars">' + barsHtml + '</div></div>',
            ctxHtml,
            noteHtml,
            reasonHtml,
            '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>',
            newsHtml,
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}


// ─── 板块卡片（增强版：含成分股列表 + 评分条 + 跳转链接） ───
function renderSectorCards(items) {
    const parts = ['<div class="card-list">'];
    for (const item of items) {
        const eff = item.efficiency || 0;
        const ec = eff >= 70 ? RING_COLORS.high : eff >= 50 ? RING_COLORS.mid : RING_COLORS.poor;
        const total = (item.limit_count||0) + (item.zhaban_count||0) + (item.dieting_count||0);
        const lc = item.limit_count || 0;
        const zc = item.zhaban_count || 0;
        const dc = item.dieting_count || 0;
        const lcPct = total > 0 ? Math.min(100, lc / total * 100) : 0;
        const zcPct = total > 0 ? Math.min(100, zc / total * 100) : 0;
        const dcPct = total > 0 ? Math.min(100, dc / total * 100) : 0;

        const tags = [];
        if (eff >= 80) tags.push(['合力强','tag-green']);
        else if (eff >= 60) tags.push(['联动好','tag-blue']);
        else tags.push(['分歧大','tag-yellow']);
        if (lc >= 4) tags.push(['热点核心','tag-green']);
        else if (lc >= 3) tags.push(['板块活跃','tag-blue']);
        if (dc >= 2) tags.push(['有跌停','tag-red']);

        const stockStr = (item.stocks || []).map(s =>
            `<a href="https://stockpage.10jqka.com.cn/${s.code}/" target="_blank" class="sector-stock-link" onclick="event.stopPropagation()">${esc(s.name)} <small>${s.code}</small></a>`
        ).join(' ');
        const more = (item.limit_count || 0) > (item.stocks || []).length;

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank" style="font-size:13px;background:rgba(79,140,255,0.12);color:var(--accent)">' + lc + '只涨停</span>',
            '<span class="card-name" style="font-size:16px">' + esc(item.name) + '</span>',
            '<span class="card-score" style="font-size:16px;font-weight:700;color:' + ec + '">' + item.score + '</span>',
            '</div>',
            '<div class="card-body"><div class="card-bars">',
            '<div class="bar-row"><span class="bar-label">涨停</span><div class="bar-track"><div class="bar-fill high" style="width:' + lcPct + '%"></div></div><span class="bar-val" style="color:var(--green)">' + lc + '</span></div>',
            '<div class="bar-row"><span class="bar-label">炸板</span><div class="bar-track"><div class="bar-fill mid" style="width:' + zcPct + '%"></div></div><span class="bar-val" style="color:var(--yellow)">' + zc + '</span></div>',
            '<div class="bar-row"><span class="bar-label">跌停</span><div class="bar-track"><div class="bar-fill low" style="width:' + dcPct + '%"></div></div><span class="bar-val" style="color:var(--red)">' + dc + '</span></div>',
            '</div>',
            '<div class="card-info">',
            '<div class="info-row"><span class="label">联动分</span><span class="value" style="color:' + ec + '">' + item.score + '</span></div>',
            '<div class="info-row"><span class="label">封板率</span><span class="value" style="color:' + ec + '">' + eff + '%</span></div>',
            total > 0 ? '<div class="info-row"><span class="label">赚钱效应</span><span class="value" style="color:' + (eff>=70?RING_COLORS.high:RING_COLORS.poor) + '">' + (eff>=80?'强':eff>=60?'中':'弱') + '</span></div>' : '',
            '</div></div>',
            '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>',
            stockStr ? '<div class="card-analysis" style="border-top:none;padding-top:4px;margin-top:2px;gap:4px;align-items:center"><span class="tag tag-blue" style="font-size:11px">涨停成分</span> ' + stockStr + (more ? ' <span style="font-size:11px;color:var(--text-muted)">…</span>' : '') + '</div>' : '',
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 简化卡片（趋势/炸板/翘板） ───
function renderMiniStockCards(items, pageKey) {
    const parts = ['<div class="card-list">'];
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const chg = item.change_pct;
        const cv = chg !== undefined ? (chg >= 5 ? RING_COLORS.high : chg >= 3 ? RING_COLORS.mid : RING_COLORS.poor) : '';
        const watched = isWatched(item.code);
        parts.push(
            `<a href="https://stockpage.10jqka.com.cn/${item.code}/" target="_blank" class="stock-card">`,
            '<div class="card-header">',
            `<span class="card-rank">#${i+1}</span>`,
            `<span class="card-name">${esc(item.name)}</span>`,
            `<span class="card-code">${item.code}</span>`,
            `<span class="watchlist-btn ${watched?'watched':''}" data-code="${item.code}" data-name="${esc(item.name)}" onclick="toggleWatchBtn(this);return false">${watched?'✓':'+'}</span>`,
            chg !== undefined ? `<span class="card-score" style="color:${cv}">+${chg}%</span>` : '',
            '</div>',
            item.seal_time ? `<div style="font-size:12px;color:var(--text-muted);padding:8px 0">封板时间: ${item.seal_time.slice(0,2)}:${item.seal_time.slice(2)}</div>` : '',
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}


// ─── 跌停翘板卡片（含评分 + 信号分析 + 策略） ───
function renderDtqiaobanCards(items) {
    const parts = ['<div class="card-list">'];
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var sc = item.score || 0;
        var col = sc >= 70 ? RING_COLORS.high : sc >= 50 ? RING_COLORS.mid : sc >= 35 ? RING_COLORS.low : RING_COLORS.poor;
        var st = item.seal_time || '';
        var sig = item.signals || [];

        // 信号标签
        var tags = [];
        if (sc >= 70) tags.push(['翘板概率高','tag-green']);
        else if (sc >= 50) tags.push(['有翘板可能','tag-blue']);
        else if (sc >= 35) tags.push(['翘板难度大','tag-yellow']);
        else tags.push(['不建议参与','tag-red']);
        for (var si = 0; si < sig.length && tags.length < 5; si++) {
            var s = sig[si];
            if (s.indexOf('巨量') >= 0) tags.push([s,'tag-green']);
            else if (s.indexOf('极小') >= 0 || s.indexOf('偏小') >= 0) tags.push([s,'tag-blue']);
            else if (s.indexOf('高换手') >= 0) tags.push([s,'tag-green']);
            else if (s.indexOf('超跌') >= 0) tags.push([s,'tag-yellow']);
        }

        // 行情行
        var infoHtml = '<div style="font-size:12px;color:var(--text-muted);padding:4px 0">';
        if (item.price) infoHtml += '💰 ' + item.price.toFixed(2) + '  ';
        if (item.consecutive >= 1) infoHtml += '📉 连跌' + item.consecutive + '板  ';
        if (item.turnover) infoHtml += '🔄 换手' + item.turnover + '%  ';
        if (st) infoHtml += '⏰ ' + (parseInt(st.slice(0,2)) >= 14 ? '尾盘' : parseInt(st.slice(0,2)) >= 13 ? '午后' : parseInt(st.slice(0,2)) >= 10 ? '早盘' : '开盘') + st.slice(0,2) + ':' + st.slice(2);
        infoHtml += '</div>';

        // 策略
        var adviceHtml = '';
        if (item.advice) {
            var adviceColor = sc >= 70 ? 'var(--green)' : sc >= 50 ? 'var(--yellow)' : 'var(--red)';
            adviceHtml = '<div style="font-size:12px;color:' + adviceColor + ';padding:4px 0">策略: ' + esc(item.advice) + '</div>';
        }

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">' + item.code + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-score" style="color:' + col + '">' + sc + '</span>',
            '</div>',
            '<div class="card-body"><div class="card-bars">',
            '<div class="bar-row"><span class="bar-label">翘板评分</span><div class="bar-track"><div class="bar-fill ' + (sc >= 70 ? 'high' : sc >= 50 ? 'mid' : 'low') + '" style="width:' + Math.min(100, sc) + '%"></div></div><span class="bar-val" style="color:' + col + '">' + sc + '</span></div>',
            '<div class="bar-row"><span class="bar-label">放量</span><div class="bar-track"><div class="bar-fill mid" style="width:' + (sig.indexOf('巨量翘板')>=0 ? 100 : sig.indexOf('放量翘板')>=0 ? 75 : sig.indexOf('微量翘板')>=0 ? 45 : 15) + '%"></div></div><span class="bar-val">' + (item.seal_fund ? (item.seal_fund/1e8).toFixed(2)+'亿' : '-') + '</span></div>',
            '<div class="bar-row"><span class="bar-label">封单</span><div class="bar-track"><div class="bar-fill low" style="width:' + (sig.indexOf('封单极小')>=0 ? 15 : sig.indexOf('封单偏小')>=0 ? 35 : sig.indexOf('封单适中')>=0 ? 55 : 90) + '%"></div></div><span class="bar-val">' + (item.seal_fund ? (item.seal_fund/1e4).toFixed(0)+'万' : '-') + '</span></div>',
            '</div></div>',
            infoHtml,
            adviceHtml,
            '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>',
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}





// ─── 市场情绪卡片（含评分表盘 + 明细数据） ───
function renderSentimentCards(data) {
    var sc = data.score || 0;
    var lv = data.level || '未知';
    var icon = data.icon || '📊';
    var col = sc >= 8 ? '#ef4444' : sc >= 6 ? '#fbbf24' : sc >= 4 ? '#34d399' : sc >= 2 ? '#94a3b8' : '#60a5fa';

    var pct = sc * 10;
    var ringCol = col;

    // 等级标签
    var lvLabel = lv;
    if (lv.indexOf('炸板') >= 0) lvLabel = lv;

    // 明细数据
    var items = [
        { label: '昨日涨停', value: data.prev_limit_count + ' 只', color: 'var(--green)' },
        { label: '今日涨停', value: (data.today_limit_up || 0) + ' 只', color: 'var(--green)' },
        { label: '今日跌停', value: (data.today_limit_down || 0) + ' 只', color: 'var(--red)' },
        { label: '全市场涨', value: (data.all_up || 0) + ' 家', color: data.all_up > data.all_down ? 'var(--green)' : 'var(--red)' },
        { label: '全市场跌', value: (data.all_down || 0) + ' 家', color: data.all_up > data.all_down ? 'var(--red)' : 'var(--green)' },
        { label: '涨跌比', value: (data.all_up && data.all_down ? (data.all_up / data.all_down).toFixed(2) : '-'), color: data.all_up > data.all_down ? 'var(--green)' : 'var(--red)' },
    ];

    var detailRows = '';
    for (var di = 0; di < items.length; di++) {
        var it = items[di];
        detailRows += '<div class="bar-row"><span class="bar-label" style="width:70px">' + it.label + '</span><div class="bar-track"><div class="bar-fill" style="width:' + (di < 3 ? Math.min(100, data[['prev_limit_count','today_limit_up','today_limit_down'][di]] * 3) : 50) + '%;background:' + it.color + ';opacity:0.3"></div></div><span class="bar-val" style="color:' + it.color + '">' + it.value + '</span></div>';
    }

    return '<div class="card-list">' +
        '<div class="stock-card" style="cursor:default">' +
        '<div class="card-header">' +
        '<span style="font-size:24px;margin-right:8px">' + icon + '</span>' +
        '<span class="card-name" style="font-size:18px">市场情绪</span>' +
        '<span class="card-score" style="font-size:24px;color:' + ringCol + '">' + lvLabel + '</span>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:16px;padding:8px 0">' +
        '<div class="sr-wrap" style="width:80px;height:80px"><div class="sr-ring" style="width:80px;height:80px;background:conic-gradient(' + ringCol + ' ' + pct + '%, transparent ' + pct + '%)"><div class="sr-inner" style="width:64px;height:64px;font-size:22px;background:var(--bg-secondary)">' + sc + '</div></div></div>' +
        '<div style="flex:1"><div class="card-bars">' + detailRows + '</div></div>' +
        '</div>' +
        '<div class="card-hint"><span>评分制: 0-10分 · 市场情绪综合指标</span></div>' +
        '</div></div>';
}

function renderIndicatorsCards(items) {
    const parts = ['<div class="card-list">'];
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var sr = item.seal_ratio || 0;
        var col = sr >= 1.5 ? RING_COLORS.high : sr >= 0.5 ? RING_COLORS.mid : RING_COLORS.poor;
        var sig = item.signals || [];

        var tags = [];
        if (sr >= 1.5) tags.push(['强封','tag-green']);
        else if (sr >= 0.5) tags.push(['封板一般','tag-blue']);
        else tags.push(['弱封','tag-yellow']);
        if (item.lhb_level == 'warn') tags.push(['龙虎榜预警','tag-red']);
        if (item.leadership) tags.push([esc(item.leadership), 'tag-blue']);

        var hasLhb = item.lhb_net && Math.abs(item.lhb_net) > 0;
        var hasDetail = item.lhb_detail || item.position;

        var infoHtml = '';
        if (hasLhb || hasDetail) {
            infoHtml = '<div style="font-size:12px;color:var(--text-muted);padding:4px 0">';
            if (hasLhb) {
                var netColor = item.lhb_net >= 0 ? 'var(--green)' : 'var(--red)';
                infoHtml += '💰 主力净<span style="color:' + netColor + '">' + (item.lhb_net >= 0 ? '+' : '') + esc(item.lhb_net_str) + '</span>  ';
            }
            if (item.position) infoHtml += '📊 ' + esc(item.position) + '  ';
            if (item.lhb_detail) infoHtml += '📋 ' + esc(item.lhb_detail);
            infoHtml += '</div>';
        }

        var srPct = Math.min(100, sr * 60);
        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">' + item.code + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-score" style="color:' + col + '">' + (sr ? sr.toFixed(2) : '-') + '</span>',
            '</div>',
            '<div class="card-body"><div class="card-bars">',
            '<div class="bar-row"><span class="bar-label">封成比</span><div class="bar-track"><div class="bar-fill ' + (sr >= 1.5 ? 'high' : sr >= 0.5 ? 'mid' : 'low') + '" style="width:' + srPct + '%"></div></div><span class="bar-val" style="color:' + col + '">' + (sr ? sr.toFixed(2) : '-') + '</span></div>',
            (hasLhb ? '<div class="bar-row"><span class="bar-label">资金</span><div class="bar-track"><div class="bar-fill ' + (item.lhb_net >= 0 ? 'high' : 'low') + '" style="width:' + Math.min(100, Math.abs(item.lhb_net || 0) / 2e8 * 100) + '%"></div></div><span class="bar-val ' + (item.lhb_net >= 0 ? 'green' : 'red') + '">' + esc(item.lhb_net_str || '-') + '</span></div>' : ''),
            '</div></div>',
            infoHtml,
            '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>',
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 趋势动量卡片（含趋势分析 + 量价 + 策略） ───
function renderTrendCards(items) {
    const parts = ['<div class="card-list">'];
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var chg = item.change_pct || 0;
        var col = chg >= 7 ? RING_COLORS.high : chg >= 5 ? RING_COLORS.mid : RING_COLORS.poor;
        var sig = item.signals || [];

        var tags = [];
        if (chg >= 7) tags.push(['强势续涨','tag-green']);
        else if (chg >= 5) tags.push(['量价齐升','tag-blue']);
        else tags.push(['温和上涨','tag-yellow']);
        for (var si = 0; si < sig.length && tags.length < 5; si++) {
            var s = sig[si];
            if (s.indexOf('连板') >= 0) tags.push([s, 'tag-green']);
            else if (s.indexOf('活跃') >= 0 || s.indexOf('健康') >= 0) tags.push([s, 'tag-blue']);
            else tags.push([s, 'tag-gray']);
        }
        if (item.industry) tags.push([esc(item.industry), 'tag-blue']);

        var infoHtml = '<div style="font-size:12px;color:var(--text-muted);padding:4px 0">';
        if (item.price) infoHtml += '💰 ' + item.price.toFixed(2) + '  ';
        if (item.industry) infoHtml += '📌 ' + esc(item.industry) + '  ';
        if (item.turnover) infoHtml += '🔄 换手' + item.turnover + '%  ';
        infoHtml += '📊 ' + (item.volume || '') + item.volume_unit;
        infoHtml += '</div>';

        var adviceHtml = '';
        if (item.advice) {
            var ac = chg >= 7 ? 'var(--green)' : chg >= 5 ? 'var(--yellow)' : 'var(--red)';
            adviceHtml = '<div style="font-size:12px;color:' + ac + ';padding:4px 0">策略: ' + esc(item.advice) + '</div>';
        }

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">' + item.code + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-score" style="color:' + col + '">+' + chg + '%</span>',
            '</div>',
            '<div class="card-body"><div class="card-bars">',
            '<div class="bar-row"><span class="bar-label">涨幅</span><div class="bar-track"><div class="bar-fill ' + (chg >= 7 ? 'high' : chg >= 5 ? 'mid' : 'low') + '" style="width:' + Math.min(100, chg * 10) + '%"></div></div><span class="bar-val" style="color:' + col + '">+' + chg + '%</span></div>',
            '<div class="bar-row"><span class="bar-label">换手</span><div class="bar-track"><div class="bar-fill mid" style="width:' + Math.min(100, (item.turnover || 0) * 4) + '%"></div></div><span class="bar-val">' + (item.turnover || '-') + '%</span></div>',
            '<div class="bar-row"><span class="bar-label">连板</span><div class="bar-track"><div class="bar-fill high" style="width:' + Math.min(100, (item.consecutive || 0) * 30) + '%"></div></div><span class="bar-val">' + (item.consecutive || 0) + '板</span></div>',
            '</div></div>',
            infoHtml,
            adviceHtml,
            '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>',
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 炸板分析卡片（含评分 + 信号分析 + 策略） ───
function renderZhabanCards(items) {
    const parts = ['<div class="card-list">'];
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var sc = item.score || 0;
        var col = sc >= 70 ? RING_COLORS.high : sc >= 50 ? RING_COLORS.mid : sc >= 35 ? RING_COLORS.low : RING_COLORS.poor;
        var sig = item.signals || [];
        var st = item.seal_time || '';

        var tags = [];
        if (sc >= 70) tags.push(['反包潜力高','tag-green']);
        else if (sc >= 50) tags.push(['有反包可能','tag-blue']);
        else if (sc >= 35) tags.push(['反包难度大','tag-yellow']);
        else tags.push(['不建议参与','tag-red']);
        for (var si = 0; si < sig.length && tags.length < 5; si++) {
            var s = sig[si];
            if (s.indexOf('资金承接强') >= 0 || s.indexOf('早盘封板') >= 0) tags.push([s, 'tag-green']);
            else if (s.indexOf('有资金承接') >= 0 || s.indexOf('换手适中') >= 0) tags.push([s, 'tag-blue']);
            else if (s.indexOf('高换手') >= 0) tags.push([s, 'tag-yellow']);
            else if (s.indexOf('资金流出') >= 0) tags.push([s, 'tag-red']);
            else tags.push([s, 'tag-gray']);
        }

        var infoHtml = '<div style="font-size:12px;color:var(--text-muted);padding:4px 0">';
        if (item.price) infoHtml += '💰 ' + item.price.toFixed(2) + '  ';
        if (item.industry) infoHtml += '📌 ' + esc(item.industry) + '  ';
        if (st) infoHtml += '⏰ 封板' + st.slice(0,2) + ':' + st.slice(2) + '  ';
        if (item.turnover) infoHtml += '🔄 换手' + item.turnover + '%';
        infoHtml += '</div>';

        var adviceHtml = '';
        if (item.advice) {
            var ac = sc >= 70 ? 'var(--green)' : sc >= 50 ? 'var(--yellow)' : 'var(--red)';
            adviceHtml = '<div style="font-size:12px;color:' + ac + ';padding:4px 0">策略: ' + esc(item.advice) + '</div>';
        }

        // 净流入金额显示
        var nm = item.net_money || 0;
        var nmStr = nm >= 0 ? '+' : '';
        if (Math.abs(nm) >= 1e8) nmStr += (nm/1e8).toFixed(2) + '亿';
        else if (Math.abs(nm) >= 1e4) nmStr += (nm/1e4).toFixed(0) + '万';
        else nmStr += nm.toFixed(0);

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">' + item.code + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-score" style="color:' + col + '">' + sc + '</span>',
            '</div>',
            '<div class="card-body"><div class="card-bars">',
            '<div class="bar-row"><span class="bar-label">反包评分</span><div class="bar-track"><div class="bar-fill ' + (sc >= 70 ? 'high' : sc >= 50 ? 'mid' : 'low') + '" style="width:' + Math.min(100, sc) + '%"></div></div><span class="bar-val" style="color:' + col + '">' + sc + '</span></div>',
            '<div class="bar-row"><span class="bar-label">资金</span><div class="bar-track"><div class="bar-fill ' + (nm >= 0 ? 'high' : 'low') + '" style="width:' + Math.min(100, Math.abs(nm) / 2e8 * 100) + '%"></div></div><span class="bar-val ' + (nm >= 0 ? 'green' : 'red') + '">' + nmStr + '</span></div>',
            '<div class="bar-row"><span class="bar-label">封板</span><div class="bar-track"><div class="bar-fill mid" style="width:' + Math.min(100, (item.seal_fund || 0) / 1e8 * 50) + '%"></div></div><span class="bar-val">' + (item.seal_fund ? (item.seal_fund/1e4).toFixed(0) + '万' : '-') + '</span></div>',
            '</div></div>',
            infoHtml,
            adviceHtml,
            '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>',
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 文本报告渲染 ───
function renderStyledText(txt) {
    if (!txt || txt === '(空)') return '<span class="loading">暂无数据</span>';
    const lines = txt.split('\n');
    const parts = ['<div class="text-report">'];
    for (const line of lines) {
        const t = line.trim();
        if (!t) { parts.push('<div class="tr-empty"></div>'); continue; }
        if (/^[═=]{5,}$/.test(t) || /^[─-]{5,}$/.test(t)) { parts.push('<div class="tr-divider"></div>'); continue; }
        if (/^[【#]/.test(t) || /^[📊📈📉💥🚨💰🔥✅⚠️❌🎯📋🛡️🧩💬🌡️⏱️🏆]/u.test(t)) {
            parts.push(`<div class="tr-section-title">${esc(t)}</div>`); continue;
        }
        if (/^  [^\s]/.test(line) && t.includes('：')) {
            const idx = t.indexOf('：');
            parts.push(`<div class="tr-label-row"><span class="tr-label">${esc(t.slice(0,idx))}</span><span class="tr-value">${esc(t.slice(idx+1))}</span></div>`);
            continue;
        }
        if (t.includes('|')) {
            parts.push(`<div class="tr-data-row">${t.split('|').map(c => `<span class="tr-cell">${esc(c.trim())}</span>`).join('')}</div>`);
            continue;
        }
        if (/^\d+[\.\s、]/.test(t)) {
            parts.push(`<div class="tr-item-row">${esc(t)}</div>`); continue;
        }
        parts.push(`<div class="tr-line">${esc(t)}</div>`);
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 表格渲染 ───
function renderTableOutput(txt) {
    const lines = txt.split('\n').filter(l => l.trim());
    if (lines.length < 3 || !lines[0].includes('─')) return null;
    let hi = -1, ds = -1;
    for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        if (l.includes('│') && !l.includes('┼') && hi === -1) hi = i;
        if (l.includes('┼')) ds = i + 1;
    }
    if (hi === -1 || ds === -1) return null;
    const hdrs = lines[hi].split('│').map(h => h.trim()).filter(h => h);
    const rows = [];
    for (let i = ds; i < lines.length; i++) {
        const l = lines[i];
        if (l.includes('─')) break;
        if (!l.includes('│')) continue;
        const cells = l.split('│').map(c => c.trim()).filter(c => c);
        if (cells.length === hdrs.length) rows.push(cells);
    }
    if (!rows.length) return null;
    const nc = new Set(), cc = new Set();
    if (rows.length > 0) {
        for (let i = 0; i < hdrs.length; i++) {
            const v = (rows[0][i] || '').trim();
            const h = hdrs[i].trim();
            if (h === '#' || h === '序号' || h.includes('代码')) { cc.add(i); continue; }
            if (/^[+-]?(0|[1-9]\d*)(\.\d+)?(%|亿|万)?$/.test(v) && v.length < 12) nc.add(i);
        }
    }
    let h = '<div class="table-wrap"><table class="data-table"><thead><tr>';
    for (let i = 0; i < hdrs.length; i++) {
        const cls = nc.has(i) ? 'num' : cc.has(i) ? 'center' : '';
        h += cls ? `<th class="${cls}">${esc(hdrs[i])}</th>` : `<th>${esc(hdrs[i])}</th>`;
    }
    h += '</tr></thead><tbody>';
    for (const row of rows) {
        h += '<tr>';
        for (let i = 0; i < row.length; i++) {
            const cls = nc.has(i) ? 'num' : cc.has(i) ? 'center' : '';
            h += cls ? `<td class="${cls}">${esc(row[i])}</td>` : `<td>${esc(row[i])}</td>`;
        }
        h += '</tr>';
    }
    return h + '</tbody></table></div>';
}

// ─── 简化卡片（通用回调） ───
function renderSimpleCards(items, pageKey) {
    return renderMiniStockCards(items, pageKey);
}

// ─── 兼容 `escapeHtml` 别名（被 watchlist.js 引用） ───
window.escapeHtml = esc;
