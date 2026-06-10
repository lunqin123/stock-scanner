/* ═══════════════════════════════════════
   选股扫描器 — 卡片渲染（性能优化版）
   ═══════════════════════════════════════ */

// ─── 工具函数 ───
// 格式化涨跌幅：正值拼+，负值自带-，防止 +-0.9% 这种错误
function fmtPct(v, decimals) {
    if (v == null || isNaN(v)) return '-';
    var n = Number(v);
    var s = n.toFixed(decimals != null ? decimals : 1);
    return (n >= 0 ? '+' : '') + s + '%';
}
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

        const bars = [
            [s.buyability_score || 5, 10, '可买性'],
            [s.stock_sentiment_score || 5, 10, '个股情绪'],
            [s.tech_score, 10, '量价结构'],
            [s.sector_mom, 12, '板块热度'],
            [s.money_score, 20, '资金驱动'],
            [s.principal_score || 5, 10, '本金适配'],
        ];

        let barsHTML = '';
        for (const [sc, mx, lb] of bars) {
            const v = (sc != null ? sc : 0);
            const pct = Math.min(100, (v / mx) * 100);
            barsHTML += `<div class="bar-row"><span class="bar-label">${lb || barLabel(mx)}</span><div class="bar-track"><div class="bar-fill ${barColor(pct)}" style="width:${pct}%"></div></div><span class="bar-val">${sc}</span></div>`;
        }

        const sentScore = s.sentiment_score != null ? s.sentiment_score : 5;
        const sentCls = sentScore >= 7 ? 'green' : sentScore <= 3 ? 'red' : '';
        const sentDisplay = sentScore + '/10';

        const tags = analyzeTags(s);

        // 合并所有标签：分析标签 + 危险标签
        var allTags = tags.slice();
        if (s.danger_flags && s.danger_flags.length)
            allTags = allTags.concat(s.danger_flags.map(function(f) { return [f, 'tag-red']; }));
        var tagsHTML = allTags.length ? '<div class="card-analysis">' + allTags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>' : '';

        // 竞价条件紧凑行
        var auctionHTML = s.auction_check ? '<div class="card-auction" style="padding:6px 0 2px;font-size:12px;color:var(--yellow);border-top:1px solid rgba(255,200,50,0.15);margin-top:4px">📋 ' + s.auction_check + '</div>' : '';

        parts.push(
            '<a href="https://stockpage.10jqka.com.cn/' + s.code + '/" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">#' + s.rank + '</span>',
            '<span class="card-name">' + esc(s.name) + '</span>',
            '<span class="card-code">' + s.code + '</span>',
            '<span class="copy-btn" onclick="copyCode(\'' + esc(s.code) + '\',\'' + esc(s.name) + '\');event.stopPropagation();return false" title="复制代码">📋</span>',
            scoreRingHTML(s.total_score),
            '</div>',
            // 双列信息（左:核心数据 右:评分条）
            '<div class="card-body" style="display:flex;flex-wrap:wrap;gap:8px 16px">',
            '<div class="card-info" style="flex:1;min-width:120px;font-size:12px">',
            '<div class="info-row"><span class="label">基础分</span><span class="value">' + s.base_score + '</span></div>',
            '<div class="info-row"><span class="label">大盘情绪</span><span class="value ' + sentCls + '">' + esc(sentDisplay) + '</span></div>',
            '<div class="info-row"><span class="label">净流入</span><span class="value ' + (s.net_money >= 0 ? 'green' : 'red') + '">' + msgn + esc(s.net_money_str) + '</span></div>',
            '<div class="info-row"><span class="label">换手率</span><span class="value">' + esc(s.turnover) + '%</span></div>',
            '<div class="info-row"><span class="label">封板时间</span><span class="value">' + st.slice(0, 2) + ':' + st.slice(2) + '</span></div>',
            '</div>',
            '<div class="card-bars" style="flex:1.5;min-width:150px">' + barsHTML + '</div>',
            '</div>',
            auctionHTML,
            tagsHTML,
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 板块标签助记 ───
function barLabel(max) {
    var labels = {10:'买得到',12:'晋级预期',20:'资金驱动'};
    return labels[max] || '';
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

        // 新闻项（不能嵌套 <a>，改用 onclick 跳转）
        var newsHtml = '';
        if (item.news && item.news.length) {
            newsHtml = '<div class="card-analysis" style="border-top:none;padding-top:4px;gap:3px;flex-direction:column">';
            for (var ni = 0; ni < item.news.length; ni++) {
                var n = item.news[ni];
                var nurl = n.url || 'https://www.10jqka.com.cn/#/search/' + encodeURIComponent(n.title);
                newsHtml += '<span onclick="event.stopPropagation();event.preventDefault();window.open(\'' + esc(nurl) + '\',\'_blank\')" style="cursor:pointer;font-size:12px;color:var(--text-secondary);padding:2px 0">';
                if (n.source) newsHtml += '<span style="color:var(--text-muted);font-size:10px">[' + esc(n.source) + ']</span> ';
                newsHtml += esc(n.title.length > 50 ? n.title.slice(0,50) + '...' : n.title) + '</span>';
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
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const eff = item.efficiency || 0;
        const sc = item.score || 0;
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

        // 涨停成分股（可展开，防嵌套<a>）
        var stockListHtml = '';
        var stocks = item.stocks || [];
        if (stocks.length) {
            var sid = 'ss-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,5);
            stockListHtml = '<div class="sector-stock-wrap" style="padding:10px 0 4px;display:flex;flex-wrap:wrap;align-items:center;gap:6px;line-height:1.8">';
            stockListHtml += '<span class="sector-stock-label">🧩 成分股</span>';
            for (var si = 0; si < stocks.length; si++) {
                var stk = stocks[si];
                var cls = (si >= 3) ? ' sector-stock-pill-hide' : '';
                var tag = '<span data-ss="' + sid + '-' + si + '" class="sector-stock-pill' + cls + '" onclick="event.stopPropagation();event.preventDefault();window.open(\'https://stockpage.10jqka.com.cn/' + esc(stk.code) + '/\',\'_blank\')">' + esc(stk.name) + '<em>' + esc(stk.code) + '</em></span>';
                stockListHtml += tag;
            }
            if (stocks.length > 3) {
                var nMore = stocks.length - 3;
                stockListHtml += '<span id="' + sid + '-btn" class="sector-expand-btn" onclick="document.getElementById(\'' + sid + '-fold\').style.display=\'\';this.style.display=\'none\';var h=this.parentElement.querySelectorAll(\'.sector-stock-pill-hide\');for(var i=0;i<h.length;i++)h[i].classList.remove(\'sector-stock-pill-hide\')">+展开' + nMore + '只</span>';
                stockListHtml += '<span id="' + sid + '-fold" class="sector-fold-btn" style="display:none" onclick="this.style.display=\'none\';document.getElementById(\'' + sid + '-btn\').style.display=\'\';var h=this.parentElement.querySelectorAll(\'.sector-stock-pill-hide\');for(var i=0;i<h.length;i++)h[i].classList.add(\'sector-stock-pill-hide\')">收起</span>';
            }
            stockListHtml += '</div>';
        }

        // 左列 info
        var infoHtml = '<div class="card-info" style="flex:1;min-width:120px;font-size:12px">';
        infoHtml += '<div class="info-row"><span class="label">联动分</span><span class="value" style="color:' + ec + '">' + sc + '</span></div>';
        infoHtml += '<div class="info-row"><span class="label">封板率</span><span class="value" style="color:' + ec + '">' + eff + '%</span></div>';
        infoHtml += '<div class="info-row"><span class="label">涨停数</span><span class="value" style="color:var(--green)">' + lc + '</span></div>';
        infoHtml += '<div class="info-row"><span class="label">炸板数</span><span class="value" style="color:var(--yellow)">' + zc + '</span></div>';
        infoHtml += '<div class="info-row"><span class="label">跌停数</span><span class="value" style="color:var(--red)">' + dc + '</span></div>';
        if (total > 0) infoHtml += '<div class="info-row"><span class="label">赚钱效应</span><span class="value" style="color:' + (eff>=70?RING_COLORS.high:RING_COLORS.poor) + '">' + (eff>=80?'强':eff>=60?'中':'弱') + '</span></div>';
        infoHtml += '</div>';

        // 右列 bars
        var bars = [
            [lcPct, 'high', '涨停', lc + '', 'var(--green)'],
            [zcPct, 'mid', '炸板', zc + '', 'var(--yellow)'],
            [dcPct, 'low', '跌停', dc + '', 'var(--red)'],
            [Math.min(100, eff), eff >= 70 ? 'high' : eff >= 50 ? 'mid' : 'low', '封板率', eff + '%', ec],
        ];
        var barsHtml = '<div class="card-bars" style="flex:1.5;min-width:150px">';
        for (var bi = 0; bi < bars.length; bi++) {
            var b = bars[bi];
            barsHtml += '<div class="bar-row"><span class="bar-label">' + b[2] + '</span><div class="bar-track"><div class="bar-fill ' + b[1] + '" style="width:' + b[0] + '%"></div></div><span class="bar-val" style="color:' + b[4] + '">' + b[3] + '</span></div>';
        }
        barsHtml += '</div>';

        // 板块竞价条件
        var auctionHtml = '';
        if (item.auction_check) {
            const secCol = sc >= 10 ? 'var(--green)' : sc >= 6 ? 'var(--yellow)' : 'var(--red)';
            auctionHtml = '<div class="card-auction" style="font-size:12px;color:' + secCol + '">📋 竞价: ' + esc(item.auction_check) + '</div>';
        }

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">#' + (i + 1) + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-code" style="font-size:12px;background:rgba(79,140,255,0.12);color:var(--accent);padding:2px 6px;border-radius:4px">' + lc + '只涨停</span>',
            scoreRingHTML(sc),
            '</div>',
            '<div class="card-body" style="display:flex;flex-wrap:wrap;gap:8px 16px">',
            infoHtml,
            barsHtml,
            '</div>',
            auctionHtml,
            '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>',
            stockListHtml,
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
        const cv = chg !== undefined ? (chg > 0 ? (chg >= 7 ? '#ef4444' : chg >= 3 ? '#f87171' : '#fca5a5') : chg < 0 ? (chg <= -5 ? '#16a34a' : '#22c55e') : '#94a3b8') : '';
        parts.push(
            `<a href="https://stockpage.10jqka.com.cn/${item.code}/" target="_blank" class="stock-card">`,
            '<div class="card-header">',
            `<span class="card-rank">#${i+1}</span>`,
            `<span class="card-name">${esc(item.name)}</span>`,
            `<span class="card-code">${item.code}</span>`,
            `<span class="copy-btn" onclick="copyCode('${item.code}','${esc(item.name)}');event.stopPropagation();return false" title="复制代码">📋</span>`,
            chg !== undefined ? `<span class="card-score" style="color:${cv}">${fmtPct(chg)}</span>` : '',
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
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const sc = item.score || 0;
        const col = sc >= 70 ? RING_COLORS.high : sc >= 50 ? RING_COLORS.mid : sc >= 35 ? RING_COLORS.low : RING_COLORS.poor;
        const st = item.seal_time || '';
        const sig = item.signals || [];
        const to = typeof item.turnover === 'number' ? item.turnover.toFixed(1) : (item.turnover || '-');

        const tags = [];
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

        // 左列 info
        let infoHtml = '<div class="card-info" style="flex:1;min-width:120px;font-size:12px">';
        if (item.price) infoHtml += '<div class="info-row"><span class="label">价格</span><span class="value">' + item.price.toFixed(2) + '</span></div>';
        if (item.consecutive >= 1) infoHtml += '<div class="info-row"><span class="label">连跌</span><span class="value">' + item.consecutive + '板</span></div>';
        if (item.turnover != null) infoHtml += '<div class="info-row"><span class="label">换手</span><span class="value">' + to + '%</span></div>';
        if (st) {
            const hh = parseInt(st.slice(0,2));
            const period = hh >= 14 ? '尾盘' : hh >= 13 ? '午后' : hh >= 10 ? '早盘' : '开盘';
            infoHtml += '<div class="info-row"><span class="label">封板时间</span><span class="value">' + period + ' ' + st.slice(0,2) + ':' + st.slice(2) + '</span></div>';
        }
        if (item.seal_fund) infoHtml += '<div class="info-row"><span class="label">封单</span><span class="value">' + (item.seal_fund/1e4).toFixed(0) + '万</span></div>';
        infoHtml += '</div>';

        // 右列 bars
        const amountW = sig.indexOf('巨量翘板')>=0 ? 100 : sig.indexOf('放量翘板')>=0 ? 75 : sig.indexOf('微量翘板')>=0 ? 45 : 15;
        const sealW = sig.indexOf('封单极小')>=0 ? 15 : sig.indexOf('封单偏小')>=0 ? 35 : sig.indexOf('封单适中')>=0 ? 55 : 90;
        const bars = [
            [Math.min(100, sc), sc >= 70 ? 'high' : sc >= 50 ? 'mid' : 'low', '翘板评分', sc + '', col],
            [amountW, 'mid', '放量', (item.seal_fund ? (item.seal_fund/1e8).toFixed(2)+'亿' : '-'), null],
            [sealW, 'low', '封单', (item.seal_fund ? (item.seal_fund/1e4).toFixed(0)+'万' : '-'), null],
            [Math.min(100, to * 4), to > 15 ? 'high' : to > 5 ? 'mid' : 'low', '换手', to + '%', null],
        ];
        let barsHtml = '<div class="card-bars" style="flex:1.5;min-width:150px">';
        for (const [pct, cls, lb, val, vc] of bars) {
            barsHtml += '<div class="bar-row"><span class="bar-label">' + lb + '</span><div class="bar-track"><div class="bar-fill ' + cls + '" style="width:' + pct + '%"></div></div><span class="bar-val"' + (vc ? ' style="color:' + vc + '"' : '') + '>' + val + '</span></div>';
        }
        barsHtml += '</div>';

        let adviceHtml = '';
        if (item.auction_check) {
            const adviceColor = sc >= 70 ? 'var(--green)' : sc >= 50 ? 'var(--yellow)' : 'var(--red)';
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:' + adviceColor + '">📋 竞价: ' + esc(item.auction_check) + '</div>';
        } else if (item.advice) {
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:var(--yellow)">📋 策略: ' + esc(item.advice) + '</div>';
        }

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">#' + (i + 1) + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-code">' + esc(item.code) + '</span>',
            '<span class="copy-btn" onclick="copyCode(\'' + esc(item.code) + '\',\'' + esc(item.name) + '\');event.stopPropagation();return false" title="复制代码">📋</span>',
            scoreRingHTML(sc),
            '</div>',
            '<div class="card-body" style="display:flex;flex-wrap:wrap;gap:8px 16px">',
            infoHtml,
            barsHtml,
            '</div>',
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
        { label: '上交易日涨停', value: data.prev_limit_count + ' 只', color: 'var(--green)' },
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
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const chg = item.change_pct || 0;
        const col = chg > 0 ? (chg >= 7 ? '#ef4444' : chg >= 3 ? '#f87171' : '#fca5a5')
                : chg < 0 ? (chg <= -5 ? '#16a34a' : chg <= -2 ? '#22c55e' : '#4ade80')
                : '#94a3b8';
        const sc = item.risk_score || 0;
        const sig = item.signals || [];
        const to = typeof item.turnover === 'number' ? item.turnover.toFixed(1) : (item.turnover || '-');

        // 标签（保留趋势特有逻辑）
        const tags = [];
        for (var si = 0; si < sig.length && tags.length < 5; si++) {
            var s = sig[si];
            if (s.indexOf('连板') >= 0) tags.push([s, 'tag-green']);
            else if (s.indexOf('活跃') >= 0 || s.indexOf('健康') >= 0) tags.push([s, 'tag-blue']);
            else tags.push([s, 'tag-gray']);
        }
        if (!tags.length) {
            if (chg >= 7) tags.push(['强势续涨','tag-green']);
            else if (chg >= 5) tags.push(['量价齐升','tag-blue']);
            else tags.push(['温和上涨','tag-yellow']);
        }
        if (item.industry) tags.push([esc(item.industry), 'tag-blue']);

        // 左列 info：保留趋势特有信息
        var infoHtml = '<div class="card-info" style="flex:1;min-width:120px;font-size:12px">';
        infoHtml += '<div class="info-row"><span class="label">涨幅</span><span class="value" style="color:' + col + '">' + fmtPct(chg) + '</span></div>';
        if (item.price) infoHtml += '<div class="info-row"><span class="label">价格</span><span class="value">' + item.price.toFixed(2) + '</span></div>';
        if (item.industry) infoHtml += '<div class="info-row"><span class="label">行业</span><span class="value">' + esc(item.industry) + '</span></div>';
        if (item.turnover != null) infoHtml += '<div class="info-row"><span class="label">换手</span><span class="value">' + to + '%</span></div>';
        if (item.volume || item.volume_unit) infoHtml += '<div class="info-row"><span class="label">成交</span><span class="value">' + (item.volume || '') + (item.volume_unit || '') + '</span></div>';
        if (item.consecutive) infoHtml += '<div class="info-row"><span class="label">连涨</span><span class="value">' + item.consecutive + '天</span></div>';
        infoHtml += '</div>';

        // 右列 bars：4 条
        const bars = [
            [Math.min(100, Math.abs(chg) * 10), chg >= 7 ? 'high' : chg >= 5 ? 'mid' : chg <= 0 ? 'neg' : 'low', '涨幅', fmtPct(chg), col],
            [Math.min(100, (item.turnover || 0) * 4), (item.turnover || 0) > 15 ? 'high' : (item.turnover || 0) > 5 ? 'mid' : 'low', '换手', to + '%', null],
            [Math.min(100, (item.consecutive || 0) * 30), 'high', '连涨', (item.consecutive || 0) + '天', null],
            [sc, sc >= 80 ? 'high' : sc >= 60 ? 'mid' : 'low', '风险分', sc + '', null],
        ];
        var barsHtml = '<div class="card-bars" style="flex:1.5;min-width:150px">';
        for (const [pct, cls, lb, val, vc] of bars) {
            barsHtml += '<div class="bar-row"><span class="bar-label">' + lb + '</span><div class="bar-track"><div class="bar-fill ' + cls + '" style="width:' + pct + '%"></div></div><span class="bar-val"' + (vc ? ' style="color:' + vc + '"' : '') + '>' + val + '</span></div>';
        }
        barsHtml += '</div>';

        // 策略提示
        var adviceHtml = '';
        if (item.auction_check) {
            var ac = sc >= 80 ? 'var(--green)' : sc >= 60 ? 'var(--yellow)' : 'var(--red)';
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:' + ac + '">📋 竞价: ' + esc(item.auction_check) + '</div>';
        } else if (item.advice) {
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:var(--yellow)">📋 策略: ' + esc(item.advice) + '</div>';
        }

        var tagsHtml = tags.length ? '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>' : '';

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">#' + (i + 1) + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-code">' + esc(item.code) + '</span>',
            '<span class="copy-btn" onclick="copyCode(\'' + esc(item.code) + '\',\'' + esc(item.name) + '\');event.stopPropagation();return false" title="复制代码">📋</span>',
            scoreRingHTML(sc),
            '</div>',
            '<div class="card-body" style="display:flex;flex-wrap:wrap;gap:8px 16px">',
            infoHtml,
            barsHtml,
            '</div>',
            adviceHtml,
            tagsHtml,
            '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>',
            '</a>'
        );
    }
    parts.push('</div>');
    return parts.join('');
}

// ─── 反转扫描卡片（上交易日涨停今日回调 → 明日反包潜力） ───
function renderReversalCards(items) {
    const parts = ['<div class="card-list">'];
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const chg = item.change_pct || 0;
        const col = chg > 0 ? '#ef4444' : chg < 0 ? '#22c55e' : '#94a3b8';
        const sig = item.signals || [];
        const sc = item.risk_score || 0;
        const to = typeof item.turnover === 'number' ? item.turnover.toFixed(1) : (item.turnover || 0);

        const tags = [];
        for (var si = 0; si < sig.length && tags.length < 5; si++) {
            var s = sig[si];
            if (s.indexOf('回调') >= 0 || s.indexOf('洗盘') >= 0) tags.push([s, 'tag-yellow']);
            else if (s.indexOf('放量') >= 0 || s.indexOf('承接') >= 0) tags.push([s, 'tag-blue']);
            else if (s.indexOf('板块') >= 0) tags.push([s, 'tag-green']);
            else tags.push([s, 'tag-gray']);
        }
        if (item.industry) tags.push([esc(item.industry), 'tag-blue']);

        // 左列 info
        let infoHtml = '<div class="card-info" style="flex:1;min-width:120px;font-size:12px">';
        infoHtml += '<div class="info-row"><span class="label">今日涨幅</span><span class="value" style="color:' + col + '">' + fmtPct(chg) + '</span></div>';
        if (item.price) infoHtml += '<div class="info-row"><span class="label">价格</span><span class="value">' + item.price.toFixed(2) + '</span></div>';
        if (item.industry) infoHtml += '<div class="info-row"><span class="label">行业</span><span class="value">' + esc(item.industry) + '</span></div>';
        if (item.turnover != null) infoHtml += '<div class="info-row"><span class="label">换手</span><span class="value">' + to + '%</span></div>';
        infoHtml += '<div class="info-row"><span class="label">反包潜力</span><span class="value" style="color:' + (sc >= 70 ? RING_COLORS.high : sc >= 50 ? RING_COLORS.mid : RING_COLORS.poor) + '">' + sc + '/100</span></div>';
        infoHtml += '</div>';

        // 右列 bars
        const bars = [
            [Math.min(100, Math.abs(chg) * 15), 'neg', '回调', fmtPct(chg), col],
            [Math.min(100, to * 4), to > 20 ? 'high' : to > 10 ? 'mid' : 'low', '换手', to + '%', null],
            [sc, sc >= 70 ? 'high' : sc >= 50 ? 'mid' : 'low', '反包分', sc + '', null],
        ];
        let barsHtml = '<div class="card-bars" style="flex:1.5;min-width:150px">';
        for (const [pct, cls, lb, val, vc] of bars) {
            barsHtml += '<div class="bar-row"><span class="bar-label">' + lb + '</span><div class="bar-track"><div class="bar-fill ' + cls + '" style="width:' + pct + '%"></div></div><span class="bar-val"' + (vc ? ' style="color:' + vc + '"' : '') + '>' + val + '</span></div>';
        }
        barsHtml += '</div>';

        let adviceHtml = '';
        if (item.auction_check) {
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:var(--yellow)">📋 ' + esc(item.auction_check) + '</div>';
        } else if (item.advice) {
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:var(--yellow)">📋 ' + esc(item.advice) + '</div>';
        }

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">#' + (i + 1) + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-code">' + esc(item.code) + '</span>',
            '<span class="copy-btn" onclick="copyCode(\'' + esc(item.code) + '\',\'' + esc(item.name) + '\');event.stopPropagation();return false" title="复制代码">📋</span>',
            scoreRingHTML(sc),
            '</div>',
            '<div class="card-body" style="display:flex;flex-wrap:wrap;gap:8px 16px">',
            infoHtml,
            barsHtml,
            '</div>',
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
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const sc = item.score || 0;
        const col = sc >= 70 ? RING_COLORS.high : sc >= 50 ? RING_COLORS.mid : sc >= 35 ? RING_COLORS.low : RING_COLORS.poor;
        const sig = item.signals || [];
        const st = item.seal_time || '';
        const to = typeof item.turnover === 'number' ? item.turnover.toFixed(1) : (item.turnover || '-');

        const tags = [];
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

        const nm = item.net_money || 0;
        let nmStr = nm >= 0 ? '+' : '';
        if (Math.abs(nm) >= 1e8) nmStr += (nm/1e8).toFixed(2) + '亿';
        else if (Math.abs(nm) >= 1e4) nmStr += (nm/1e4).toFixed(0) + '万';
        else nmStr += nm.toFixed(0);

        // 左列 info
        let infoHtml = '<div class="card-info" style="flex:1;min-width:120px;font-size:12px">';
        if (item.price) infoHtml += '<div class="info-row"><span class="label">价格</span><span class="value">' + item.price.toFixed(2) + '</span></div>';
        if (item.industry) infoHtml += '<div class="info-row"><span class="label">行业</span><span class="value">' + esc(item.industry) + '</span></div>';
        if (st) infoHtml += '<div class="info-row"><span class="label">封板时间</span><span class="value">' + st.slice(0,2) + ':' + st.slice(2) + '</span></div>';
        if (item.turnover != null) infoHtml += '<div class="info-row"><span class="label">换手</span><span class="value">' + to + '%</span></div>';
        infoHtml += '<div class="info-row"><span class="label">净流入</span><span class="value ' + (nm >= 0 ? 'green' : 'red') + '">' + nmStr + '</span></div>';
        infoHtml += '</div>';

        // 右列 bars
        const bars = [
            [Math.min(100, sc), sc >= 70 ? 'high' : sc >= 50 ? 'mid' : 'low', '反包评分', sc + '', col],
            [Math.min(100, Math.abs(nm) / 2e8 * 100), nm >= 0 ? 'high' : 'low', '资金净流', nmStr, null],
            [Math.min(100, (item.seal_fund || 0) / 1e8 * 50), 'mid', '封单', (item.seal_fund ? (item.seal_fund/1e4).toFixed(0) + '万' : '-'), null],
            [Math.min(100, to * 4), to > 15 ? 'high' : to > 5 ? 'mid' : 'low', '换手', to + '%', null],
        ];
        let barsHtml = '<div class="card-bars" style="flex:1.5;min-width:150px">';
        for (const [pct, cls, lb, val, vc] of bars) {
            barsHtml += '<div class="bar-row"><span class="bar-label">' + lb + '</span><div class="bar-track"><div class="bar-fill ' + cls + '" style="width:' + pct + '%"></div></div><span class="bar-val"' + (vc ? ' style="color:' + vc + '"' : '') + '>' + val + '</span></div>';
        }
        barsHtml += '</div>';

        let adviceHtml = '';
        if (item.auction_check) {
            const ac = sc >= 70 ? 'var(--green)' : sc >= 50 ? 'var(--yellow)' : 'var(--red)';
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:' + ac + '">📋 竞价: ' + esc(item.auction_check) + '</div>';
        } else if (item.advice) {
            adviceHtml = '<div class="card-auction" style="font-size:12px;color:var(--yellow)">📋 策略: ' + esc(item.advice) + '</div>';
        }

        const tagsHtml = '<div class="card-analysis">' + tags.map(function(t) { return '<span class="tag ' + t[1] + '">' + esc(t[0]) + '</span>'; }).join('') + '</div>';

        parts.push(
            '<a href="' + esc(item.url || '#') + '" target="_blank" class="stock-card">',
            '<div class="card-header">',
            '<span class="card-rank">#' + (i + 1) + '</span>',
            '<span class="card-name">' + esc(item.name) + '</span>',
            '<span class="card-code">' + esc(item.code) + '</span>',
            '<span class="copy-btn" onclick="copyCode(\'' + esc(item.code) + '\',\'' + esc(item.name) + '\');event.stopPropagation();return false" title="复制代码">📋</span>',
            scoreRingHTML(sc),
            '</div>',
            '<div class="card-body" style="display:flex;flex-wrap:wrap;gap:8px 16px">',
            infoHtml,
            barsHtml,
            '</div>',
            adviceHtml,
            tagsHtml,
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

// ─── 复制股票代码到剪贴板 ───
function copyCode(code, name) {
    const txt = code + ' ' + name;
    if (navigator.clipboard) {
        navigator.clipboard.writeText(txt).catch(function() {
            fallbackCopy(txt);
        });
    } else {
        fallbackCopy(txt);
    }
}
function fallbackCopy(txt) {
    var ta = document.createElement('textarea');
    ta.value = txt;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
}

// ─── 兼容 `escapeHtml` 别名 ───
window.escapeHtml = esc;

// ─── 回测追踪面板 ───
function corrPage(uid, delta, totalPages) {
    var rows = document.querySelectorAll('.' + uid + '-row');
    var label = document.getElementById(uid + '-label');
    if (!rows.length || !label) return;
    var cur = parseInt(label.textContent) || 1;
    var next = Math.max(1, Math.min(totalPages, cur + delta));
    if (next === cur) return;
    rows.forEach(function(r) { r.style.display = r.dataset.page == next ? '' : 'none'; });
    label.textContent = next + ' / ' + totalPages;
}
window.corrPage = corrPage;

function renderBacktestDashboard(data) {
    if (!data || !data.ok) {
        return '<div class="loading">暂无回测数据</div>';
    }

    // 因子中英文映射
    var FACTOR_CN = {
        'seal': '封板强度', 'tech': '量价结构', 'sector_mom': '板块热度',
        'sector_res': '板块共振', 'history': '历史股性', 'money': '资金驱动',
        'buyability': '开盘可行性', 'stock_sentiment': '个股情绪', 'principal_score': '本金适配'
    };
    function factorName(key) { return FACTOR_CN[key] || key; }

    var html = '<div class="dashboard-grid" style="display:flex;flex-wrap:wrap;gap:16px;padding:16px">';

    // 1. 权重对比卡片
    html += '<div class="card" style="flex:1;min-width:280px"><h3 style="margin:0 0 12px">权重对比 (当前 vs 默认)</h3>';
    html += '<table style="width:100%;font-size:13px;border-collapse:collapse">';
    html += '<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left;padding:4px">因子</th><th>当前</th><th>默认</th><th>偏移</th></tr>';
    var weights = data.weights || [];
    for (var i = 0; i < weights.length; i++) {
        var w = weights[i];
        var deltaSign = w.delta > 0 ? '+' : '';
        var deltaColor = w.delta > 0.05 ? '#22c55e' : w.delta < -0.05 ? '#ef4444' : 'var(--text-muted)';
        html += '<tr style="border-bottom:1px solid var(--border)">';
        html += '<td style="padding:4px;font-weight:600">' + esc(factorName(w.factor)) + '</td>';
        html += '<td style="text-align:center;padding:4px">' + w.current.toFixed(1) + '</td>';
        html += '<td style="text-align:center;padding:4px;color:var(--text-muted)">' + w.default.toFixed(1) + '</td>';
        html += '<td style="text-align:center;padding:4px;color:' + deltaColor + '">' + deltaSign + w.delta.toFixed(2) + '</td>';
        html += '</tr>';
    }
    html += '</table></div>';

    // 2. 相关性历史卡片（分页，每页5条）
    html += '<div class="card" style="flex:2;min-width:360px"><h3 style="margin:0 0 12px">因子相关性历史</h3>';
    var corrHist = data.corr_history || [];
    if (corrHist.length > 0) {
        var PAGE_SIZE = 5;
        var totalPages = Math.ceil(corrHist.length / PAGE_SIZE);
        var uid = 'corr_' + Date.now();
        // 表头
        html += '<table style="width:100%;font-size:12px;border-collapse:collapse"><thead>';
        html += '<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left;padding:3px">日期</th>';
        var factors = data.backtest_factors || [];
        for (var f = 0; f < factors.length; f++) {
            html += '<th style="padding:3px">' + esc(factorName(factors[f])) + '</th>';
        }
        html += '</tr></thead><tbody id="' + uid + '">';
        for (var d = corrHist.length - 1; d >= 0; d--) {
            var day = corrHist[d];
            var page = Math.floor((corrHist.length - 1 - d) / PAGE_SIZE) + 1;
            html += '<tr class="' + uid + '-row" data-page="' + page + '" style="border-bottom:1px solid var(--border);' + (page > 1 ? 'display:none' : '') + '">';
            html += '<td style="padding:3px;font-size:11px">' + esc(day.date || '') + '</td>';
            for (var f2 = 0; f2 < factors.length; f2++) {
                var val = day[factors[f2]];
                var vc = val > 0.1 ? '#22c55e' : val < -0.1 ? '#ef4444' : 'var(--text-muted)';
                html += '<td style="text-align:center;padding:3px;color:' + vc + '">' + (val != null ? val.toFixed(3) : '-') + '</td>';
            }
            html += '</tr>';
        }
        html += '</tbody></table>';
        if (totalPages > 1) {
            html += '<div style="display:flex;justify-content:center;align-items:center;gap:8px;padding:8px 0;font-size:12px">';
            html += '<button data-uid="' + uid + '" data-delta="-1" data-total="' + totalPages + '" class="corr-page-btn" style="padding:2px 8px;cursor:pointer;border:1px solid var(--border);background:var(--card-bg);color:var(--text);border-radius:4px">&lt; 上一页</button>';
            html += '<span id="' + uid + '-label" style="min-width:60px;text-align:center">1 / ' + totalPages + '</span>';
            html += '<button data-uid="' + uid + '" data-delta="1" data-total="' + totalPages + '" class="corr-page-btn" style="padding:2px 8px;cursor:pointer;border:1px solid var(--border);background:var(--card-bg);color:var(--text);border-radius:4px">下一页 &gt;</button>';
            html += '</div>';
        }
    } else {
        html += '<div class="loading">暂无相关性数据 — 需积累至少1天回测</div>';
    }
    html += '</div>';

    // 3. 状态卡片
    html += '<div class="card" style="flex:0 0 200px"><h3 style="margin:0 0 12px">系统状态</h3>';
    html += '<div style="font-size:13px;line-height:2">';
    html += '回测数据: <b>' + (data.days_with_data || 0) + '</b> 天<br>';
    html += '调权就绪: <b style="color:' + (data.ready ? '#22c55e' : '#f59e0b') + '">' + (data.ready ? 'YES' : '需>=2天') + '</b><br>';
    html += '可调权因子: <b>' + (data.backtest_factors || []).length + '</b> 个<br>';
    html += '学习率: <b>0.02</b>/天<br>';
    html += '钳制范围: <b>0.5x-1.5x</b> 默认权重<br>';
    html += '</div></div>';

    html += '</div>';
    return html;
}

// 注册全局渲染函数
window.renderBacktestDashboard = renderBacktestDashboard;

// ═══════════════════════════════════════════
//  P6: 单 tab 完整回测面板 (新)
// ═══════════════════════════════════════════

function renderBacktestTabFull(data) {
    if (!data || !data.ok) {
        return '<div class="loading">回测加载失败 - ' + (data && data.error || '未知错误') + '</div>';
    }
    var bt = data.backtest || {};
    var s = bt.summary || {};
    var s30 = bt.summary_30d || {};
    var cfg = bt.config || {};
    var top5 = bt.top5 || [];
    var bot5 = bt.bottom5 || [];
    var skipped = bt.skipped || [];
    var cmp = bt.comparison || {};
    var ti = data.tab_info || {};
    var wgt = data.weights || {};

    var html = '<div class="dashboard-grid" style="display:flex;flex-wrap:wrap;gap:12px;padding:12px;margin-top:8px">';

    // ── 0. Tab 状态条 ──
    html += '<div class="card" style="flex:0 0 100%;padding:8px 16px">';
    html += '<span style="font-size:12px;color:var(--text-muted)">数据窗口: ' + ti.days_available + '天 | ';
    html += '仓位建议: <b style="color:' + (ti.position_weight >= 1 ? '#ef4444' : '#f59e0b') + '">' + (ti.position_label || '--') + '</b> | ';
    html += 'TOP' + (cfg.top_n || 3) + ' | 本金' + (cfg.capital || 30000) + '元</span>';
    html += '</div>';

    // ── 1. 胜率横幅 (复用现有逻辑) ──
    var s30d = s30;
    var unbuyable = (cmp.unbuyable_count) || 0;

    function _wrBlock(label, sum, isPrimary) {
        var wr = sum.win_rate || 0;
        var wrColor = wr >= 60 ? '#ef4444' : wr >= 45 ? '#f59e0b' : '#22c55e';
        var grade = wr >= 70 ? 'S' : wr >= 55 ? 'A' : wr >= 40 ? 'B' : wr >= 25 ? 'C' : 'D';
        var gradeColor = wr >= 70 ? '#ffd700' : wr >= 55 ? '#ef4444' : wr >= 40 ? '#f59e0b' : '#22c55e';
        var barPct = Math.min(100, Math.max(0, wr));
        var size = isPrimary ? '42px' : '30px';
        var h = '<div style="flex:1;min-width:180px;padding:12px 8px;text-align:center">';
        h += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">' + label + '</div>';
        h += '<div style="display:inline-block;padding:1px 8px;border-radius:8px;background:' + gradeColor + ';color:#000;font-size:10px;font-weight:700;margin-bottom:4px">' + grade + '</div>';
        h += '<div style="font-size:' + size + ';font-weight:800;line-height:1;color:' + wrColor + '">' + wr.toFixed(1) + '%</div>';
        h += '<div style="font-size:10px;color:var(--text-muted);margin-top:2px">' + (sum.win_count||0) + 'W/' + (sum.loss_count||0) + 'L | ' + (sum.trade_count||0) + '笔</div>';
        h += '</div>';
        return h;
    }

    html += '<div class="card" style="flex:0 0 100%;padding:16px">';
    html += '<div style="display:flex;flex-wrap:wrap;justify-content:center">';
    html += _wrBlock('近30天胜率', s30d, true);
    html += '<div style="width:1px;background:var(--border);align-self:stretch;margin:4px 0"></div>';
    html += _wrBlock('全部历史胜率', s, false);
    html += '</div>';
    html += '<div style="font-size:11px;color:var(--text-muted);margin-top:8px;text-align:center;border-top:1px solid var(--border);padding-top:6px">';
    html += '累计收益 <b style="color:' + (s.cumulative_ret >= 0 ? '#ef4444' : '#22c55e') + '">' + (s.cumulative_ret >= 0 ? '+' : '') + (s.cumulative_ret||0).toFixed(2) + '%</b>';
    html += ' | 总盈亏 <b style="color:' + (s.total_pnl >= 0 ? '#ef4444' : '#22c55e') + '">' + (s.total_pnl >= 0 ? '+' : '') + (s.total_pnl||0).toFixed(0) + '元</b>';
    html += ' | EV <b>' + (s.ev >= 0 ? '+' : '') + (s.ev||0).toFixed(2) + '%</b>';
    if (unbuyable > 0) html += ' | 一字板过滤: ' + unbuyable + '笔';
    html += '</div></div>';

    // ── 2. 核心指标 ──
    function _metric(label, value, color, sub) {
        return '<div style="flex:1;min-width:120px;padding:10px;background:var(--card-bg);border-radius:8px;border:1px solid var(--border)">'
            + '<div style="font-size:10px;color:var(--text-muted);margin-bottom:3px">' + label + '</div>'
            + '<div style="font-size:20px;font-weight:700;color:' + (color||'var(--text)') + '">' + value + '</div>'
            + (sub ? '<div style="font-size:9px;color:var(--text-muted)">' + sub + '</div>' : '')
            + '</div>';
    }
    html += '<div class="card" style="flex:0 0 100%"><div style="display:flex;flex-wrap:wrap;gap:6px">';
    html += _metric('笔数', s.trade_count||0, '#3b82f6', '跳过' + skipped.length);
    html += _metric('平均收益', (s.avg_ret||0).toFixed(2)+'%', s.avg_ret >= 0 ? '#ef4444' : '#22c55e');
    html += _metric('盈亏比', (s.plr||0).toFixed(2), s.plr >= 1.5 ? '#ef4444' : 'var(--text)');
    html += _metric('最大回撤', (s.max_dd||0).toFixed(2)+'%', '#22c55e');
    html += _metric('期望值', (s.ev >= 0 ? '+' : '') + (s.ev||0).toFixed(2)+'%', s.ev > 0 ? '#ef4444' : '#22c55e');
    html += '</div></div>';

    // ── 3. 策略对比 ──
    function _cmpCard(label, sum, color) {
        if (!sum) return '';
        var wc = sum.win_rate >= 60 ? '#ef4444' : sum.win_rate >= 45 ? '#f59e0b' : '#22c55e';
        return '<div style="flex:1;min-width:200px;padding:10px;border:1px solid ' + color + '44;border-radius:8px;background:var(--card-bg)">'
            + '<div style="font-size:12px;font-weight:600;margin-bottom:6px;color:' + color + '">' + label + '</div>'
            + '<div style="font-size:11px;line-height:1.7">'
            + '<span>胜率 <b style="color:' + wc + '">' + (sum.win_rate||0).toFixed(1) + '%</b></span> '
            + '<span>EV <b>' + ((sum.ev||0)>=0?'+':'') + (sum.ev||0).toFixed(2) + '%</b></span> '
            + '<span>' + (sum.trade_count||0) + '笔</span>'
            + '</div></div>';
    }
    html += '<div class="card" style="flex:0 0 100%"><h4 style="margin:0 0 8px">策略对比</h4>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:6px">';
    html += _cmpCard('A 开盘买', (cmp.open_buy||{}).summary, '#3b82f6');
    html += _cmpCard('B 尾盘买', (cmp.close_buy||{}).summary, '#a855f7');
    html += _cmpCard('C 休盘+止损', (cmp.stop_loss||{}).summary, '#f59e0b');
    html += '</div></div>';

    // ── 4. 因子权重面板 ──
    var factors = wgt.factors || [];
    if (factors.length > 0) {
        html += '<div class="card" style="flex:0 0 100%"><h4 style="margin:0 0 8px">因子权重</h4>';
        html += '<div style="display:flex;flex-wrap:wrap;gap:4px">';
        factors.forEach(function(f) {
            var delta = f.delta || 0;
            var arrow = delta > 0.5 ? '↑' : delta < -0.5 ? '↓' : '→';
            var dColor = delta > 0 ? '#ef4444' : delta < 0 ? '#22c55e' : 'var(--text-muted)';
            html += '<div style="flex:1;min-width:100px;padding:8px;text-align:center;background:var(--bg-card);border-radius:6px;border:1px solid var(--border)">';
            html += '<div style="font-size:10px;color:var(--text-muted)">' + f.name + '</div>';
            html += '<div style="font-size:16px;font-weight:700">' + (f.current||0).toFixed(1) + '</div>';
            html += '<div style="font-size:10px;color:' + dColor + '">' + arrow + ' ' + (delta >= 0 ? '+' : '') + delta.toFixed(1) + ' (默认' + (f.default||0).toFixed(0) + ')</div>';
            html += '</div>';
        });
        html += '</div></div>';
    }

    // ── 5. 调权历史 ──
    var history = wgt.history || [];
    if (history.length > 0) {
        html += '<div class="card" style="flex:0 0 100%"><h4 style="margin:0 0 4px">调权历史 (近30天)</h4>';
        html += '<div style="font-size:10px;max-height:120px;overflow-y:auto">';
        history.slice(-15).forEach(function(h) {
            var dCol = h.delta > 0 ? '#ef4444' : '#22c55e';
            html += '<span style="margin-right:8px;white-space:nowrap">' + h.date + ' ' + h.factor + ': ' + h.old + '→' + h.new + ' <b style="color:' + dCol + '">' + h.arrow + (h.delta>=0?'+':'') + h.delta.toFixed(1) + '</b> (IC=' + (h.corr>=0?'+':'') + h.corr.toFixed(3) + ')</span><br>';
        });
        html += '</div></div>';
    }

    // ── 6. TOP5/BOTTOM5 ──
    function _tradeTable(title, trades, color) {
        if (!trades.length) return '';
        var h = '<div class="card" style="flex:1;min-width:300px"><h4 style="margin:0 0 6px">' + title + '</h4>';
        h += '<table style="width:100%;font-size:11px;border-collapse:collapse">';
        h += '<tr style="border-bottom:1px solid var(--border)"><th>日期</th><th>票</th><th>买→卖</th><th>收益</th></tr>';
        trades.slice(0, 5).forEach(function(t) {
            h += '<tr style="border-bottom:1px solid var(--border)">';
            h += '<td style="font-size:9px">' + t.signal_date + '</td>';
            h += '<td style="font-weight:600">' + esc(t.name) + '</td>';
            h += '<td>' + t.buy_price + '→' + t.sell_price + '</td>';
            h += '<td style="color:' + color + ';font-weight:600">' + (t.net_ret_pct>0?'+':'') + t.net_ret_pct.toFixed(2) + '%</td>';
            h += '</tr>';
        });
        h += '</table></div>';
        return h;
    }
    if (top5.length || bot5.length) {
        html += '<div style="flex:0 0 100%;display:flex;flex-wrap:wrap;gap:8px">';
        html += _tradeTable('🏆 TOP5', top5, '#ef4444');
        html += _tradeTable('💀 BOTTOM5', bot5, '#22c55e');
        html += '</div>';
    }

    html += '</div>'; // dashboard-grid end
    return html;
}
window.renderBacktestTabFull = renderBacktestTabFull;


// ═══════════════════════════════════════════
//  T+1 真实回测面板 (A 股 T+1 规则) — 旧版, 保留兼容
// ═══════════════════════════════════════════

function renderT1BacktestPanel(data) {
    if (!data || !data.ok) {
        return '<div class="loading">T+1 回测加载失败 - ' + (data && (data.error || (data.data && data.data.error)) || '未知错误') + '</div>';
    }
    // 兼容两种格式: {data:{summary,...}} (旧 /api/backtest/t1) 与扁平 {summary,...} (新 /api/bt/{tab})
    var d = data.data || data;
    var s = d.summary || {};
    var s30 = d.summary_30d || {};
    var cfg = d.config || {};
    var top5 = d.top5 || [];
    var bot5 = d.bottom5 || [];
    var skipped = d.skipped || [];

    var html = '<div class="dashboard-grid" style="display:flex;flex-wrap:wrap;gap:16px;padding:16px;margin-top:8px">';

    // 0. 标题 + 配置
    html += '<div class="card" style="flex:0 0 100%"><h3 style="margin:0 0 8px"> T+1 真实回测 (A 股 T+1 规则)</h3>';
    html += '<div style="font-size:12px;color:var(--text-muted)">';
    html += '策略: ' + esc(cfg.strategy || 'N/A') + ' | 评分: ' + esc(cfg.scoring || 'N/A') + '<br>';
    html += '区间: ' + esc(cfg.start || '?') + ' ~ ' + esc(cfg.end || '?') + ' | 每天 TOP ' + (cfg.top_n || 3) + ' | 本金 ' + (cfg.capital || 30000) + '元/笔 | 成本 ' + (cfg.commission_pct || 0.05) + '%+' + (cfg.slippage_pct || 0.1) + '%';
    html += '</div></div>';

    // ===== 胜率横幅: 近30天 vs 全部历史 =====
    var s30 = d.summary_30d || {};
    var unbuyable = (d.comparison && d.comparison.unbuyable_count) || 0;

    function _wrBlock(label, sum, isPrimary) {
        var wr = sum.win_rate || 0;
        var wrColor = wr >= 60 ? '#ef4444' : wr >= 45 ? '#f59e0b' : '#22c55e';
        var grade = wr >= 70 ? 'S' : wr >= 55 ? 'A' : wr >= 40 ? 'B' : wr >= 25 ? 'C' : 'D';
        var gradeColor = wr >= 70 ? '#ffd700' : wr >= 55 ? '#ef4444' : wr >= 40 ? '#f59e0b' : '#22c55e';
        var barPct = Math.min(100, Math.max(0, wr));
        var size = isPrimary ? '48px' : '36px';
        var opacity = isPrimary ? '1' : '0.85';
        var h = '<div style="flex:1;min-width:220px;padding:16px 12px;opacity:' + opacity + '">';
        h += '<div style="font-size:12px;color:var(--text-muted);margin-bottom:6px;letter-spacing:1px">' + label + '</div>';
        h += '<div style="display:inline-block;padding:1px 10px;border-radius:10px;background:' + gradeColor + ';color:#000;font-size:11px;font-weight:700;margin-bottom:6px">' + grade + '</div>';
        h += '<div style="font-size:' + size + ';font-weight:800;line-height:1;color:' + wrColor + ';letter-spacing:-1px;margin:4px 0">' + wr.toFixed(1) + '%</div>';
        h += '<div style="max-width:160px;margin:6px auto;height:6px;background:var(--border,#333);border-radius:3px;overflow:hidden">';
        h += '<div style="height:100%;width:' + barPct + '%;background:linear-gradient(90deg,' + wrColor + ',' + wrColor + '88);border-radius:3px"></div></div>';
        h += '<div style="font-size:11px;color:var(--text-muted)">';
        h += '<span style="color:' + wrColor + ';font-weight:600">' + (sum.win_count || 0) + '胜</span>';
        h += ' / ' + (sum.loss_count || 0) + '负';
        h += ' | ' + (sum.trade_count || 0) + '笔';
        h += '</div></div>';
        return h;
    }

    html += '<div class="card" style="flex:0 0 100%;padding:20px 16px;background:linear-gradient(135deg,var(--card-bg,#1a1f2e) 0%,#22c55e08 100%);border:1px solid var(--border);text-align:center">';
    html += '<div style="display:flex;flex-wrap:wrap;justify-content:center;gap:8px">';
    html += _wrBlock('近30天胜率', s30, true);
    html += '<div style="width:1px;background:var(--border);align-self:stretch;margin:8px 0"></div>';
    html += _wrBlock('全部历史胜率', s, false);
    html += '</div>';
    html += '<div style="font-size:11px;color:var(--text-muted);margin-top:8px">📌 笔数 = <b>实际可成交</b>笔数（已过滤一字板 / 停牌 / 涨停开盘无法买入等无效信号），与上方调权卡的 TOP-N 推荐总数口径不同</div>';
    // 底部统计
    html += '<div style="font-size:12px;color:var(--text-muted);margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">';
    html += '累计收益 <span style="color:' + (s.cumulative_ret >= 0 ? '#ef4444' : '#22c55e') + ';font-weight:600">' + (s.cumulative_ret >= 0 ? '+' : '') + (s.cumulative_ret || 0).toFixed(2) + '%</span>';
    html += ' | 总盈亏 <span style="color:' + (s.total_pnl >= 0 ? '#ef4444' : '#22c55e') + ';font-weight:600">' + (s.total_pnl >= 0 ? '+' : '') + (s.total_pnl || 0).toFixed(0) + ' 元</span>';
    if (unbuyable > 0) { html += ' | 涨停开盘无法买入 ' + unbuyable + ' 笔'; }
    html += '</div></div>';

    // 1. 核心指标卡片
    function metric(label, value, color, sub) {
        return '<div style="flex:1;min-width:140px;padding:12px;background:var(--card-bg,#1e2233);border-radius:8px;border:1px solid var(--border)">'
            + '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">' + label + '</div>'
            + '<div style="font-size:22px;font-weight:700;color:' + (color || 'var(--text)') + '">' + value + '</div>'
            + (sub ? '<div style="font-size:10px;color:var(--text-muted);margin-top:2px">' + sub + '</div>' : '')
            + '</div>';
    }
    html += '<div class="card" style="flex:0 0 100%"><h3 style="margin:0 0 12px">核心指标</h3>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:10px">';
    var evColor = s.ev > 0 ? '#ef4444' : '#22c55e';
    html += metric('笔数', s.trade_count || 0, '#3b82f6', '跳过: ' + skipped.length);
    html += metric('平均收益', (s.avg_ret || 0).toFixed(2) + '%', s.avg_ret >= 0 ? '#ef4444' : '#22c55e');
    html += metric('总盈亏', (s.total_pnl || 0).toFixed(0) + ' 元', s.total_pnl >= 0 ? '#ef4444' : '#22c55e', '本金' + (cfg.capital || 30000) + '×' + s.trade_count + '笔');
    html += metric('盈亏比', (s.plr || 0).toFixed(2), s.plr >= 1.5 ? '#ef4444' : 'var(--text)');
    html += metric('最大回撤', (s.max_dd || 0).toFixed(2) + '%', '#22c55e');
    html += metric('期望值 EV', (s.ev || 0).toFixed(2) + '%', evColor, evColor === '#ef4444' ? '长期盈利' : '长期亏损');
    html += '</div></div>';

    // 2. TOP 5 + BOTTOM 5
    function tradeTable(title, trades, color) {
        var h = '<div class="card" style="flex:1;min-width:380px"><h3 style="margin:0 0 8px">' + title + '</h3>';
        h += '<table style="width:100%;font-size:12px;border-collapse:collapse">';
        h += '<tr style="border-bottom:1px solid var(--border)"><th style="padding:3px;text-align:left">日期(信→买→卖)</th><th>票</th><th>评分</th><th>买→卖</th><th>收益</th><th>盈亏</th></tr>';
        for (var i = 0; i < trades.length; i++) {
            var t = trades[i];
            h += '<tr style="border-bottom:1px solid var(--border)">';
            h += '<td style="padding:3px;font-size:10px">' + t.signal_date + '→' + t.buy_date + '→' + t.sell_date + '</td>';
            h += '<td style="padding:3px;font-weight:600">' + esc(t.name) + '<span style="color:var(--text-muted);font-size:10px"> (' + t.code + ')</span></td>';
            h += '<td style="padding:3px;text-align:center">' + t.score + '</td>';
            h += '<td style="padding:3px;font-size:11px">' + t.buy_price + '→' + t.sell_price + '</td>';
            h += '<td style="padding:3px;text-align:center;color:' + color + ';font-weight:600">' + (t.net_ret_pct > 0 ? '+' : '') + t.net_ret_pct.toFixed(2) + '%</td>';
            h += '<td style="padding:3px;text-align:right;color:' + color + ';font-weight:600">' + (t.pnl > 0 ? '+' : '') + t.pnl.toFixed(0) + '元</td>';
            h += '</tr>';
        }
        h += '</table></div>';
        return h;
    }
    if (top5.length > 0) {
        html += tradeTable('🏆 TOP 5 (最赚)', top5, '#ef4444');
    }
    if (bot5.length > 0) {
        html += tradeTable('💀 BOTTOM 5 (最亏)', bot5, '#22c55e');
    }

    // 3.5 双策略对比
    var cmp = d.comparison;
    if (cmp && (cmp.open_buy || cmp.close_buy)) {
        var ob = cmp.open_buy && cmp.open_buy.summary;
        var cb = cmp.close_buy && cmp.close_buy.summary;
        html += '<div class="card" style="flex:0 0 100%"><h3 style="margin:0 0 8px">策略对比: 开盘买 vs 尾盘买</h3>';
        html += '<div style="display:flex;flex-wrap:wrap;gap:10px">';
        function _cmpCard(label, sum, color) {
            if (!sum) return '';
            var wc = sum.win_rate >= 60 ? '#ef4444' : sum.win_rate >= 45 ? '#f59e0b' : '#22c55e';
            return '<div style="flex:1;min-width:250px;padding:14px;border:1px solid ' + color + '44;border-radius:8px;background:var(--card-bg)">'
                + '<div style="font-size:13px;font-weight:600;margin-bottom:8px;color:' + color + '">' + label + '</div>'
                + '<div style="display:flex;justify-content:space-between;font-size:12px;line-height:1.8">'
                + '<span>胜率 <b style="color:' + wc + '">' + (sum.win_rate || 0).toFixed(1) + '%</b> (' + (sum.win_count||0) + 'W/' + (sum.loss_count||0) + 'L)</span>'
                + '<span>笔数 <b>' + (sum.trade_count||0) + '</b></span>'
                + '</div>'
                + '<div style="display:flex;justify-content:space-between;font-size:12px;line-height:1.8">'
                + '<span>累计 <b style="color:' + ((sum.cumulative_ret||0) >=0?'#ef4444':'#22c55e') + '">' + ((sum.cumulative_ret||0) >=0?'+':'') + (sum.cumulative_ret||0).toFixed(2) + '%</b></span>'
                + '<span>平均 <b>' + ((sum.avg_ret||0) >=0?'+':'') + (sum.avg_ret||0).toFixed(2) + '%</b></span>'
                + '</div>'
                + '<div style="display:flex;justify-content:space-between;font-size:12px;line-height:1.8">'
                + '<span>盈亏比 <b>' + (sum.plr||0).toFixed(2) + '</b></span>'
                + '<span>EV <b style="color:' + ((sum.ev||0) >0?'#ef4444':'#22c55e') + '">' + ((sum.ev||0) >=0?'+':'') + (sum.ev||0).toFixed(2) + '%</b></span>'
                + '</div>'
                + (cmp.unbuyable_count ? '<div style="font-size:11px;color:var(--text-muted);margin-top:6px;border-top:1px solid var(--border);padding-top:6px">策略A开盘买因一字板无法买入: ' + cmp.unbuyable_count + ' 笔<br><span style="font-size:10px">(策略B尾盘买/C休盘买不受影响)</span></div>' : '')
                + '</div>';
        }
        html += _cmpCard('开盘买 (D+1 开盘 -> D+2 开盘)', ob, '#3b82f6');
        html += _cmpCard('尾盘买 (D+1 收盘 -> D+2 开盘)', cb, '#a855f7');
        var st = cmp.stop_loss && cmp.stop_loss.summary;
        html += _cmpCard('休盘买+止损 (当日收盘 -> 次日止损/收盘)', st, '#f59e0b');
        html += '</div></div>';
    }

    // 4. 操作建议
    html += '<div class="card" style="flex:0 0 100%"><h3 style="margin:0 0 8px">⚙️ 关键参数 + 实操建议</h3>';
    html += '<div style="font-size:12px;line-height:1.8;color:var(--text)">';
    html += '<b>回测假设</b>: 信号日 (涨停) → D+1 开盘买入 → D+2 开盘卖出 (A 股 T+1 真实规则, 2 天持仓)<br>';
    html += '<b>评分</b>: backtest_score_prev (回测专用, 6 因子) — plan_a 实盘评分可能更准<br>';
    html += '<b>胜率含义</b>: 接近 50% 表示赢 1 亏 1, 心理压力大; 期望值 EV > 0 表示长期数学上盈利<br>';
    if (s.ev > 0) {
        html += '<b style="color:#ef4444">✅ 当前期望值 ' + (s.ev || 0).toFixed(2) + '% > 0, 长期能赚 — 但胜率 ' + (s.win_rate || 0).toFixed(1) + '% 偏低, 需要仓位控制</b><br>';
    } else {
        html += '<b style="color:#22c55e">⚠️ 当前期望值 ' + (s.ev || 0).toFixed(2) + '% < 0, 长期亏损, 不建议直接用</b><br>';
    }
    html += '<b>实盘建议</b>: 单只仓位 ≤ 1/3 总仓位; 3 连亏日主动减仓 50%; 至少 30 天数据再下结论<br>';
    html += '<b>回撤 -' + Math.abs(s.max_dd || 0).toFixed(0) + '%</b> 是 3w 本金最大浮亏 = <b>' + (30000 * Math.abs(s.max_dd || 0) / 100).toFixed(0) + ' 元</b>';
    html += '</div></div>';

    html += '</div>';  // dashboard-grid end
    return html;
}
window.renderT1BacktestPanel = renderT1BacktestPanel;

// ─── 全 Tab 策略权重面板 ───
function renderTabWeights(weights) {
    if (!weights || !weights.length) return '';
    var html = '<div class="card" style="margin:16px"><h3 style="margin:0 0 6px">📊 策略权重分配 (基于滚动胜率+EV)</h3>';
    html += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:10px;line-height:1.6">'
    + '💡 <b>调权算法专用指标</b>：各 tab 近30 天 <b>推荐过</b> 的 TOP-N 笔数 +模拟收益，用于动态调整仓位权重。<br>'
    + '⚠️ <b>与下方"近30 天回测"笔数口径不同</b>：本卡统计的是"该 tab 每天跑出的 TOP-N 推荐总数"，包含一字板 / 无效信号；下方回测是"实际可成交笔数"。<br>'
    + '👉 例：涨停 TOP3 ×30 天 ≈90 笔，但下方回测可能只算入60 笔左右（被一字板 / 无成交过滤掉）。'
    + '</div>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:8px">';
    weights.forEach(function(w) {
        var bg = w.color + '18';
        html += '<div style="flex:1;min-width:140px;padding:10px 12px;background:' + bg + ';border:1px solid ' + w.color + '44;border-radius:8px;text-align:center">';
        html += '<div style="font-size:13px;font-weight:600;color:' + w.color + '">' + w.name_cn + '</div>';
        html += '<div style="font-size:20px;font-weight:700;color:' + w.color + ';margin:4px 0">' + w.win_rate.toFixed(1) + '%</div>';
        html += '<div style="font-size:11px;color:var(--text-muted)">EV ' + (w.ev >= 0 ? '+' : '') + w.ev.toFixed(2) + '% | ' + w.trades + '笔(' + w.days + '天)</div>';
        html += '<div style="font-size:11px;font-weight:600;color:' + w.color + ';margin-top:4px">' + w.label + ' · ' + w.allocation_pct + '%仓位</div>';
        html += '</div>';
    });
    html += '</div></div>';
    return html;
}
window.renderTabWeights = renderTabWeights;

// ─── 推荐追踪面板 — 各 tab 胜率 ───
function renderTrackerPanel(data) {
    if (!data || !data.ok || !data.tabs || data.tabs.length === 0) {
        return '<div class="card" style="padding:16px;margin:16px;text-align:center;color:var(--text-muted)">暂无追踪数据 — 各 tab 推荐后次日自动产生</div>';
    }
    var html = '<div class="card" style="margin:16px"><h3 style="margin:0 0 12px">各 Tab 推荐胜率追踪</h3>';
    html += '<div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">每个 tab 推荐过的票，统计次日涨跌幅。数据累积越多越可靠。</div>';
    html += '<div style="display:flex;flex-direction:column;gap:8px">';
    for (var i = 0; i < data.tabs.length; i++) {
        var t = data.tabs[i];
        var wr = t.win_rate || 0;
        var barW = Math.min(100, Math.max(0, wr));
        var color = wr >= 60 ? '#22c55e' : wr >= 45 ? '#f59e0b' : '#ef4444';
        html += '<div style="display:flex;align-items:center;gap:12px;padding:10px 14px;border:1px solid var(--border);border-radius:8px;background:var(--card-bg,#1a1f2e)">';
        html += '<div style="width:80px;font-weight:600;font-size:13px">' + (t.label || t.tab) + '</div>';
        html += '<div style="flex:1;height:24px;background:var(--border,#333);border-radius:12px;overflow:hidden;position:relative">';
        html += '<div style="height:100%;width:' + barW + '%;background:linear-gradient(90deg,' + color + ',' + color + '88);border-radius:12px;transition:width 0.5s"></div>';
        html += '<div style="position:absolute;top:0;left:0;right:0;height:24px;line-height:24px;text-align:center;font-size:12px;font-weight:700;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,0.5)">' + wr.toFixed(1) + '%</div>';
        html += '</div>';
        html += '<div style="font-size:12px;color:var(--text-muted);min-width:100px;text-align:right">' + t.wins + '/' + t.count + ' 胜 | ' + (t.win_open_rate || 0).toFixed(0) + '%开盘涨</div>';
        html += '</div>';
    }
    html += '</div></div>';
    return html;
}
window.renderTrackerPanel = renderTrackerPanel;
