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
            [s.north_flow_score || 5, 10, '北向资金'],
            [s.momentum_consistency != null ? s.momentum_consistency : 5, 10, '持续性'],
            [s.pullback_depth != null ? s.pullback_depth : 5, 10, '回撤位置'],
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
    html += '<div class="table-wrap" style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
    html += '<table style="width:100%;font-size:13px;border-collapse:collapse;min-width:260px">';
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
    html += '</table></div></div>';

    // 2. 相关性历史卡片（分页，每页5条）
    html += '<div class="card" style="flex:2;min-width:360px"><h3 style="margin:0 0 12px">因子相关性历史</h3>';
    var corrHist = data.corr_history || [];
    if (corrHist.length > 0) {
        var PAGE_SIZE = 5;
        var totalPages = Math.ceil(corrHist.length / PAGE_SIZE);
        var uid = 'corr_' + Date.now();
        // 表头
        html += '<div class="table-wrap" style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
        html += '<table style="width:100%;font-size:12px;border-collapse:collapse;min-width:480px"><thead>';
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
        html += '</tbody></table></div>';
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
    if (!data) {
        console.error('[回测] 返回 data 为 null/undefined');
        return '<div class="error-text">回测加载失败 - 无响应 (请检查网络或服务端日志)</div>';
    }
    if (!data.ok) {
        var errMsg = data.error || data.msg || '未知错误 (服务端未返回 error 字段)';
        console.error('[回测] 服务端 ok=false:', data);
        return '<div class="error-text">回测加载失败 - ' + esc(errMsg) + '</div>';
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
    var factors = wgt.factors || [];
    var history = wgt.history || [];
    var unbuyable = (cmp.unbuyable_count) || 0;
    var html = '';

    // ═══ 第1行: 胜率横幅 + 核心数据 — 暗色渐变背景 ═══
    var wr = s.win_rate || 0;
    var wrColor = wr >= 60 ? '#ef4444' : wr >= 45 ? '#f59e0b' : '#22c55e';
    var grade = wr >= 70 ? 'S' : wr >= 55 ? 'A' : wr >= 40 ? 'B' : wr >= 25 ? 'C' : 'D';
    var gColor = wr >= 70 ? '#ffd700' : wr >= 55 ? '#ef4444' : wr >= 40 ? '#f59e0b' : '#22c55e';
    var wr30 = s30.win_rate || 0;
    var wr30Color = wr30 >= 60 ? '#ef4444' : wr30 >= 45 ? '#f59e0b' : '#22c55e';
    var posColor = ti.position_weight >= 1 ? '#ef4444' : ti.position_weight >= 0.8 ? '#f59e0b' : '#22c55e';

    html += '<div style="margin:12px;padding:20px 16px;background:linear-gradient(135deg,var(--card-bg) 0%,' + wrColor + '11 100%);border:1px solid ' + wrColor + '33;border-radius:12px">';

    // 状态条
    html += '<div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;margin-bottom:14px;font-size:11px;color:var(--text-muted)">';
    html += '<span>📅 数据窗口 <b style="color:var(--text)">' + ti.days_available + '天</b></span>';
    html += '<span>⚖️ 仓位 <b style="color:' + posColor + '">' + (ti.position_label || '--') + ' ' + (ti.position_weight||0).toFixed(1) + 'x</b></span>';
    html += '<span>📐 <b>TOP' + (cfg.top_n || 3) + '</b> · 💰 ' + (cfg.capital || 30000) + '元/笔 · ⏱️ <b style="color:var(--accent)">T+' + (cfg.sell_n || 3) + '卖出</b></span>';
    html += '</div>';

    // 双胜率横幅
    html += '<div style="display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-bottom:12px">';
    function _wrCol(label, wrVal, wColor) {
        var g = wrVal >= 70 ? 'S' : wrVal >= 55 ? 'A' : wrVal >= 40 ? 'B' : wrVal >= 25 ? 'C' : 'D';
        var gc = wrVal >= 70 ? '#ffd700' : wrVal >= 55 ? '#ef4444' : wrVal >= 40 ? '#f59e0b' : '#22c55e';
        var bar = Math.min(100, Math.max(0, wrVal));
        return '<div style="flex:1;min-width:160px;text-align:center;padding:8px">'
            + '<div style="font-size:10px;color:var(--text-muted);letter-spacing:1px;margin-bottom:4px">' + label + '</div>'
            + '<div style="display:inline-block;padding:1px 10px;border-radius:10px;background:' + gc + ';color:#000;font-size:10px;font-weight:700;margin-bottom:6px">' + g + '</div>'
            + '<div style="font-size:44px;font-weight:800;line-height:1;color:' + wColor + ';letter-spacing:-1px">' + wrVal.toFixed(1) + '%</div>'
            + '<div style="max-width:140px;margin:6px auto;height:5px;background:var(--border);border-radius:3px;overflow:hidden"><div style="height:100%;width:' + bar + '%;background:' + wColor + ';border-radius:3px"></div></div>'
            + '</div>';
    }
    html += _wrCol('近30天', wr30, wr30Color);
    html += '<div style="width:1px;background:var(--border);align-self:stretch;margin:8px 0"></div>';
    html += _wrCol('全部历史', wr, wrColor);
    html += '</div>';

    // 底部统计条
    html += '<div style="display:flex;flex-wrap:wrap;justify-content:center;gap:16px;font-size:12px;color:var(--text-muted);border-top:1px solid var(--border);padding-top:10px">';
    html += '<span>累计 <b style="color:' + (s.cumulative_ret>=0?'#ef4444':'#22c55e') + '">' + (s.cumulative_ret>=0?'+':'') + (s.cumulative_ret||0).toFixed(2) + '%</b></span>';
    html += '<span>总盈亏 <b style="color:' + (s.total_pnl>=0?'#ef4444':'#22c55e') + '">' + (s.total_pnl>=0?'+':'') + (s.total_pnl||0).toFixed(0) + '元</b></span>';
    html += '<span>EV <b style="color:' + (s.ev>0?'#ef4444':'#22c55e') + '">' + (s.ev>=0?'+':'') + (s.ev||0).toFixed(2) + '%</b></span>';
    html += '<span>笔数 <b>' + (s.trade_count||0) + '</b> (' + (s.win_count||0) + 'W/' + (s.loss_count||0) + 'L)</span>';
    if (unbuyable) html += '<span>过滤 <b>' + unbuyable + '</b>笔一字板</span>';
    html += '</div></div>';

    // ═══ 第2行: 策略对比 + 因子权重 — 并排 ═══
    html += '<div style="display:flex;flex-wrap:wrap;gap:10px;margin:0 12px 10px">';

    // 策略对比 (左栏)
    function _cmpCard(label, sum, color, icon) {
        if (!sum) return '';
        var wc = sum.win_rate >= 60 ? '#ef4444' : sum.win_rate >= 45 ? '#f59e0b' : '#22c55e';
        return '<div style="flex:1;min-width:150px;padding:10px 12px;background:' + color + '10;border:1px solid ' + color + '33;border-radius:8px;text-align:center">'
            + '<div style="font-size:11px;font-weight:600;color:' + color + ';margin-bottom:4px">' + icon + ' ' + label + '</div>'
            + '<div style="font-size:20px;font-weight:700;color:' + wc + '">' + (sum.win_rate||0).toFixed(1) + '%</div>'
            + '<div style="font-size:10px;color:var(--text-muted)">EV ' + ((sum.ev||0)>=0?'+':'') + (sum.ev||0).toFixed(2) + '% · ' + (sum.trade_count||0) + '笔</div>'
            + '</div>';
    }
    html += '<div class="card" style="flex:0 0 100%;padding:14px">';
    html += '<div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px">📊 策略对比</div>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:6px">';
    html += _cmpCard('开盘买', (cmp.open_buy||{}).summary, '#3b82f6', '🌅');
    html += '</div></div>';

    // ═══ 第3行: 因子权重 — 带进度条的精致卡片 ═══
    if (factors.length > 0) {
        html += '<div class="card" style="flex:0 0 100%;padding:14px">';
        html += '<div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:10px">⚙️ 因子权重 (IC驱动自动调权) &nbsp; <span style="font-size:10px;font-weight:400"><span style="color:#ef4444">◀低于默认</span> | <span style="color:var(--text-muted)">▎默认基准</span> | <span style="color:#22c55e">高于默认▶</span></span></div>';
        html += '<div style="display:flex;flex-direction:column;gap:5px">';
        factors.forEach(function(f) {
            var cur = f.current || 0;
            var def = f.default || 1;
            var delta = f.delta || 0;
            var dScale = Math.max(def, 5);
            // 进度条: 中线=默认权重, 高于默认→右, 低于默认→左
            var deviation = cur - def;
            var barPct, barLeft, barRadius;
            if (deviation > 0) {
                // 高于默认: 从中线向右, 最大偏到 1.5×default
                barPct = Math.min(50, deviation / (dScale * 1.5 - dScale) * 50 || 0);
                barLeft = 50;
                barRadius = '0 6px 6px 0';
            } else if (deviation < 0) {
                // 低于默认: 从中线向左, 最大偏到 -1×max(def,5)
                var maxLeft = dScale + def;  // def向下到-dScale的距离
                barPct = Math.min(50, Math.abs(deviation) / maxLeft * 50);
                barLeft = 50 - barPct;
                barRadius = '6px 0 0 6px';
            } else {
                barPct = 0; barLeft = 50;
                barRadius = '0';
            }
            // 颜色: 负权=橙, 高于默认=绿, 低于默认=红
            var barColor = cur < 0 ? '#f59e0b' : (deviation > 0 ? '#22c55e' : '#ef4444');
            var dirIcon = deviation > 0.5 ? '▶' : deviation < -0.5 ? '◀' : '▎';
            html += '<div style="display:flex;align-items:center;gap:8px;font-size:11px">';
            html += '<span style="width:48px;text-align:right;color:var(--text-muted);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + f.name + '">' + f.name + '</span>';
            // 进度条容器
            html += '<div style="flex:1;min-width:60px;height:12px;background:var(--bg-primary);border-radius:6px;overflow:hidden;position:relative">';
            // 中线基准 (默认值)
            html += '<div style="position:absolute;left:50%;top:0;width:2px;height:100%;background:var(--text-muted);opacity:0.5;transform:translateX(-1px)"></div>';
            if (barPct > 0.5) {
                html += '<div style="position:absolute;left:' + barLeft + '%;top:0;height:100%;width:' + barPct + '%;background:' + barColor + ';border-radius:' + barRadius + ';opacity:0.75"></div>';
            }
            html += '</div>';
            // 数值
            html += '<span style="width:38px;text-align:right;font-weight:700;font-family:monospace;font-size:11px;color:' + barColor + '">' + (cur >= 0 ? '+' : '') + cur.toFixed(1) + '</span>';
            html += '<span style="width:55px;font-size:10px;color:' + barColor + '">' + dirIcon + ' ' + (delta >= 0 ? '+' : '') + delta.toFixed(1) + '</span>';
            html += '<span style="width:34px;font-size:9px;color:var(--text-muted);text-align:right">默认' + def.toFixed(0) + '</span>';
            html += '</div>';
        });
        html += '</div></div>';
    }

    // 调权历史 (折叠)
    if (history.length > 0) {
        html += '<div class="card" style="flex:0 0 100%;padding:14px">';
        html += '<details><summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text-muted)">📜 调权历史 (' + history.length + '条)</summary>';
        html += '<div style="font-size:10px;max-height:100px;overflow-y:auto;margin-top:6px;line-height:1.8">';
        history.slice(-12).reverse().forEach(function(h) {
            var dCol = h.delta > 0 ? '#ef4444' : '#22c55e';
            html += '<span style="margin-right:12px;white-space:nowrap">' + h.date + ' <b>' + h.factor + '</b>: ' + h.old + '→' + h.new + ' <b style="color:' + dCol + '">' + h.arrow + '</b> (IC' + (h.corr>=0?'+':'') + h.corr.toFixed(2) + ')</span><br>';
        });
        html += '</div></details></div>';
    }

    html += '</div>'; // end 2-col

    // ═══ 第4行: TOP5 / BOTTOM5 ═══
    if (top5.length || bot5.length) {
        html += '<div style="margin:0 12px;display:flex;flex-wrap:wrap;gap:8px">';
        function _tbl(title, trades, color, icon) {
            if (!trades.length) return '';
            var h = '<div class="card" style="flex:1;min-width:320px;padding:12px"><h4 style="margin:0 0 8px;font-size:13px">' + icon + ' ' + title + '</h4>';
            h += '<div class="table-wrap" style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
            h += '<table style="width:100%;font-size:11px;border-collapse:collapse;min-width:380px">';
            h += '<tr style="border-bottom:1px solid var(--border);color:var(--text-muted)"><th style="padding:4px;text-align:left">信号日</th><th style="text-align:left">股票</th><th style="text-align:right">评分</th><th style="text-align:right">买价(买日)</th><th style="text-align:right">卖价(卖日)</th><th style="text-align:right">收益</th></tr>';
            trades.slice(0, 5).forEach(function(t) {
                h += '<tr style="border-bottom:1px solid var(--border)">';
                h += '<td style="padding:4px;font-size:10px">' + t.signal_date + '</td>';
                h += '<td style="padding:4px;font-weight:600">' + esc(t.name) + '<span style="color:var(--text-muted);font-size:10px"> ' + t.code + '</span></td>';
                h += '<td style="padding:4px;text-align:right">' + (t.score||0).toFixed(0) + '</td>';
                // 显示"价(日期)"格式,避免同日 buy=sell 看着像反
                var sameDate = t.buy_date === t.sell_date;
                h += '<td style="padding:4px;text-align:right;font-size:10px">' + t.buy_price + (sameDate ? '' : '<span style="color:var(--text-muted)">(' + t.buy_date.slice(4) + ')</span>') + '</td>';
                h += '<td style="padding:4px;text-align:right;font-size:10px">' + t.sell_price + '<span style="color:var(--text-muted)">(' + t.sell_date.slice(4) + ')</span></td>';
                h += '<td style="padding:4px;text-align:right;font-weight:700;color:' + color + '">' + (t.net_ret_pct>0?'+':'') + t.net_ret_pct.toFixed(2) + '%</td>';
                h += '</tr>';
            });
            h += '</table></div></div>';
            return h;
        }
        html += _tbl('TOP5 最赚', top5, '#ef4444', '🏆');
        html += _tbl('BOTTOM5 最亏', bot5, '#22c55e', '💀');
        html += '</div>';
    }

    // ═══ 全部交易明细 (策略A: 开盘买) ═══
    var strategies = [
        { key: 'open_buy', label: 'A 开盘买', color: '#3b82f6', icon: '🌅' }
    ];
    var totalAll = 0;
    strategies.forEach(function(st) { totalAll += ((cmp[st.key] && cmp[st.key].trades) || []).length; });
    if (totalAll > 0) {
        html += '<div class="card" style="margin:0 12px;padding:12px">';
        html += '<div style="font-size:13px;font-weight:600;color:var(--text-muted);margin-bottom:10px">📋 全部交易明细 (' + totalAll + '笔)</div>';

        strategies.forEach(function(st) {
            var trades = ((cmp[st.key] && cmp[st.key].trades) || []).slice();
            if (trades.length === 0) return;
            // 策略级小计
            var winCount = 0, sumRet = 0, sumPnl = 0;
            trades.forEach(function(t) {
                if (t.net_ret_pct > 0) winCount++;
                sumRet += (t.net_ret_pct || 0);
                sumPnl += (t.pnl || 0);
            });
            var wrPct = (winCount / trades.length) * 100;
            var avgRet = sumRet / trades.length;
            var wrColor = wrPct >= 60 ? '#ef4444' : wrPct >= 45 ? '#f59e0b' : '#22c55e';
            var pnlColor = sumPnl >= 0 ? '#ef4444' : '#22c55e';

            // 按时间倒序
            trades.sort(function(a, b) { return (b.signal_date || '').localeCompare(a.signal_date || ''); });

            // 策略标题 (默认 A 展开, B/C 折叠)
            var openByDefault = (st.key === 'open_buy') ? ' open' : '';
            html += '<details' + openByDefault + ' style="margin-bottom:10px;border:1px solid var(--border-light);border-radius:6px;padding:0">';
            html += '<summary style="cursor:pointer;padding:8px 10px;background:' + st.color + '10;border-bottom:1px solid ' + st.color + '33;font-size:12px;font-weight:600;display:flex;flex-wrap:wrap;gap:8px;align-items:center;list-style:none">';
            html += '<span style="color:' + st.color + '">' + st.icon + ' ' + st.label + '</span>';
            html += '<span style="color:var(--text-muted);font-weight:400">' + trades.length + '笔</span>';
            html += '<span style="color:' + wrColor + '">胜率 ' + wrPct.toFixed(0) + '%</span>';
            html += '<span style="color:' + wrColor + '">均收益 ' + (avgRet>=0?'+':'') + avgRet.toFixed(2) + '%</span>';
            html += '<span style="color:' + pnlColor + '">总盈亏 ' + (sumPnl>=0?'+':'') + sumPnl.toFixed(0) + '元</span>';
            html += '</summary>';

            // 卡片行布局 (两大列: 左=识别信息, 右=战绩)
            // 左: 股票名 + 日期/代码 (副标)
            // 右: 收益% (大字) + 盈亏/评分 (小字副标)
            // 列间距由 flex space-between 自动撑开,不再有列间 padding 浪费
            html += '<div style="max-height:400px;overflow-y:auto;-webkit-overflow-scrolling:touch">';
            trades.forEach(function(t) {
                var retColor = t.net_ret_pct > 0 ? '#ef4444' : '#22c55e';
                var sameDate = t.buy_date === t.sell_date;
                var dateShort = (t.signal_date || '').slice(4) + '→' + (t.sell_date || '').slice(4);
                var tooltip = '买入 ' + (t.buy_price||0).toFixed(2) + (sameDate ? '' : ' (' + t.buy_date + ')')
                    + ' | 卖出 ' + (t.sell_price||0).toFixed(2) + ' (' + t.sell_date + ')';
                html += '<div title="' + tooltip + '" style="display:flex;align-items:center;padding:7px 10px;border-bottom:1px solid var(--border);gap:14px">';
                // ── 左列: 股票识别 (固定宽度) ──
                html += '<div style="flex:0 0 auto;min-width:88px;overflow:hidden">';
                html += '<div style="font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(t.name) + '</div>';
                html += '<div style="font-size:10px;color:var(--text-muted);margin-top:1px">' + t.code + '</div>';
                html += '</div>';
                // ── 中间列: 交易明细 (占满剩余: 价格走势 + 跳空 + 持仓天数) ──
                var buyPx = (t.buy_price||0).toFixed(2);
                var sellPx = (t.sell_price||0).toFixed(2);
                var gap = t.gap_open_pct;
                var gapHtml = '';
                if (gap != null && Math.abs(gap) >= 0.3) {
                    var gapColor = gap > 0 ? '#ef4444' : '#22c55e';
                    var gapArrow = gap > 0 ? '↑' : '↓';
                    gapHtml = ' <span style="font-size:10px;font-weight:600;color:' + gapColor + '">' + gapArrow + Math.abs(gap).toFixed(1) + '%</span>';
                }
                var holdDays = 1;
                if (t.buy_date && t.sell_date && t.buy_date !== t.sell_date) {
                    try {
                        var bd = t.buy_date.slice(0,4)+'-'+t.buy_date.slice(4,6)+'-'+t.buy_date.slice(6,8);
                        var sd = t.sell_date.slice(0,4)+'-'+t.sell_date.slice(4,6)+'-'+t.sell_date.slice(6,8);
                        holdDays = Math.max(1, Math.round((new Date(sd) - new Date(bd)) / 86400000));
                    } catch(e) {}
                }
                html += '<div style="flex:1;min-width:0;text-align:center;padding:0 6px">';
                html += '<div style="font-size:12px;font-weight:500"><span style="color:var(--text-muted)">' + buyPx + '</span> → <span>' + sellPx + '</span>' + gapHtml + '</div>';
                html += '<div style="font-size:10px;color:var(--text-muted);margin-top:1px">' + dateShort + ' · 持仓 ' + holdDays + ' 天</div>';
                html += '</div>';
                // ── 右列: 战绩 (固定宽度) ──
                html += '<div style="flex:0 0 auto;text-align:right;min-width:90px">';
                html += '<div style="font-weight:700;font-size:15px;color:' + retColor + '">' + (t.net_ret_pct>0?'+':'') + t.net_ret_pct.toFixed(2) + '%</div>';
                html += '<div style="font-size:10px;color:var(--text-muted);margin-top:1px">' + (t.pnl>0?'+':'') + (t.pnl||0).toFixed(0) + '元 · ' + (t.score||0).toFixed(0) + '分</div>';
                html += '</div>';
                html += '</div>';
            });
            html += '</div></details>';
        });
        html += '</div>';
    }

    return html;
}
window.renderBacktestTabFull = renderBacktestTabFull;

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

// ─── 权重可视化调整面板 (2026-07-14) ───
function renderWeightsPanel(data) {
    if (!data || !data.ok) return '<div class="error-text">⚠️ 权重加载失败</div>';

    var factors = data.factors || [];
    var activeTab = data.tab || 'limit-up';
    var html = '';

    // Tab 选择器
    var tabLabels = {'limit-up':'涨停','trend':'趋势','zhaban':'炸板','dtqiaoban':'翘板','reversal':'反转'};
    html += '<div style="margin:12px 16px;padding:12px;background:var(--card-bg);border-radius:8px">';
    html += '<div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">⚖️ 选择 Tab 调整因子权重 — 调整后点击「应用」保存</div>';
    html += '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">';
    for (var tab in tabLabels) {
        var active = tab === activeTab ? 'background:var(--accent);color:#fff' : 'background:var(--bg-secondary);color:var(--text)';
        html += '<button class="btn" style="font-size:11px;padding:5px 10px;' + active + '" onclick="loadWeightsTab(\'' + tab + '\')">' + tabLabels[tab] + '</button>';
    }
    html += '</div>';

    if (!factors.length) {
        html += '<div style="color:var(--text-muted);font-size:12px">该 tab 无可调因子权重</div></div>';
        return html;
    }

    // 因子滑块
    html += '<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">拖动滑块调整因子权重（当前 vs 默认）</div>';
    html += '<div id="weightSliders" style="display:flex;flex-direction:column;gap:8px">';
    factors.forEach(function(f) {
        var key = f.key || '';
        var name = f.name || key;
        var cur = f.current || 0;
        var def = f.default || 0;
        var delta = f.delta || 0;
        var direction = delta > 0 ? '↑' : (delta < 0 ? '↓' : '—');
        var color = delta > 0 ? '#22c55e' : (delta < 0 ? '#ef4444' : 'var(--text-muted)');

        // 确定最大范围 (min=0, max=default*2+5)
        var maxVal = Math.max(def * 2 + 5, cur * 1.5 + 5, 60);
        var sliderPct = Math.round(cur / maxVal * 100);

        html += '<div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:var(--bg-secondary,#1a1f2e);border-radius:6px">';
        html += '<div style="width:70px;font-size:12px;font-weight:600;color:var(--text)">' + name + '</div>';
        html += '<div style="flex:1;display:flex;align-items:center;gap:8px">';
        html += '<input type="range" min="0" max="' + maxVal + '" step="0.5" value="' + cur + '"'
             + ' oninput="updateWeightDisplay(this,\'' + key + '\')"'
             + ' style="flex:1;height:6px;accent-color:var(--accent,#4f8cff);cursor:pointer">';
        html += '</div>';
        html += '<div style="width:50px;text-align:right;font-size:13px;font-weight:700" id="wval_' + key + '">' + cur.toFixed(1) + '</div>';
        html += '<div style="width:50px;font-size:11px;color:' + color + '">' + direction + ' ' + (delta >= 0 ? '+' : '') + delta.toFixed(1) + '</div>';
        html += '<div style="width:40px;font-size:10px;color:var(--text-muted);text-align:right">默认' + def.toFixed(1) + '</div>';
        html += '</div>';
    });
    html += '</div>';

    // 按钮区
    html += '<div style="display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap">';
    html += '<button onclick="applyWeights(\'' + activeTab + '\')" style="padding:8px 20px;background:var(--accent,#4f8cff);color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600">💾 应用权重</button>';
    html += '<button onclick="optimizeWeights(\'' + activeTab + '\')" style="padding:8px 16px;background:#22c55e;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600" title="跑30天回测→ICIR调权+市场逻辑修正→再测验证">🧠 智能优化</button>';
    html += '<button onclick="resetWeights(\'' + activeTab + '\')" style="padding:8px 16px;background:var(--bg-secondary);color:var(--text);border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:12px">↻ 重置默认</button>';
    html += '<span id="weightStatus" style="font-size:11px;color:var(--text-muted);margin-left:8px">就绪</span>';
    html += '</div>';

    // 智能优化结果区
    html += '<div id="optimizeResults" style="display:none;margin-top:12px"></div>';

    // 调权说明
    html += '<div style="font-size:11px;color:var(--text-muted);margin-top:12px;padding:8px;background:var(--bg-secondary,#1a1f2e);border-radius:6px;line-height:1.6">'
        + '💡 <b>调权说明</b>：<br>'
        + '• 权重越大该因子对最终评分影响越大<br>'
        + '• 拖动滑块即时预览当前权重值，点击「应用权重」保存到服务器<br>'
        + '• 🧠 <b>智能优化</b>：自动跑 30 天回测 → 分析因子 IC → 结合市场情绪/状态 → 输出最优权重 → 再回测验证<br>'
        + '• 修改后切换回对应 tab 点击「运行」按钮可查看新权重下的排行榜<br>'
        + '• 点击「重置默认」恢复该 tab 出厂权重</div>';

    html += '</div>'; // /card wrapper
    return html;
}
window.renderWeightsPanel = renderWeightsPanel;

// ─── 权重滑块辅助函数 ───
function updateWeightDisplay(slider, key) {
    var val = parseFloat(slider.value);
    var display = document.getElementById('wval_' + key);
    if (display) display.textContent = val.toFixed(1);
}

async function loadWeightsTab(tab) {
    var output = document.getElementById('output');
    if (!output) return;
    output.innerHTML = '<div class="loading">⏳ 加载权重数据...</div>';
    try {
        var resp = await fetch('/api/weights/tab/' + tab, { cache: 'no-store' });
        var data = await resp.json();
        output.innerHTML = renderWeightsPanel(data);
    } catch(e) {
        output.innerHTML = '<span class="error-text">❌ 加载失败: ' + e.message + '</span>';
    }
}
window.loadWeightsTab = loadWeightsTab;

async function applyWeights(tab) {
    var statusEl = document.getElementById('weightStatus');
    if (statusEl) statusEl.textContent = '⏳ 保存中...';

    // 从滑块收集所有因子权重
    var factors = {};
    var sliders = document.querySelectorAll('#weightSliders input[type="range"]');
    sliders.forEach(function(s) {
        var key = s.getAttribute('oninput').match(/updateWeightDisplay\(this,'(\w+)'\)/);
        if (key && key[1]) {
            factors[key[1]] = parseFloat(s.value);
        }
    });

    if (Object.keys(factors).length === 0) {
        if (statusEl) statusEl.textContent = '⚠️ 无因子数据';
        return;
    }

    try {
        var resp = await fetch('/api/weights/tab/' + tab, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({factors: factors}),
            cache: 'no-store'
        });
        var data = await resp.json();
        if (data.ok) {
            if (statusEl) { statusEl.textContent = '✅ ' + (data.msg || '已保存'); statusEl.style.color = '#22c55e'; }
            // 重新加载显示最新值
            setTimeout(function() { loadWeightsTab(tab); }, 300);
        } else {
            if (statusEl) statusEl.textContent = '❌ ' + (data.error || '保存失败');
        }
    } catch(e) {
        if (statusEl) statusEl.textContent = '❌ ' + e.message;
    }
}
window.applyWeights = applyWeights;

async function resetWeights(tab) {
    if (!confirm('重置 ' + tab + ' 的因子权重到默认值？')) return;
    var statusEl = document.getElementById('weightStatus');
    if (statusEl) statusEl.textContent = '⏳ 重置中...';
    try {
        // 获取默认权重
        var resp = await fetch('/api/weights/tab/' + tab, { cache: 'no-store' });
        var data = await resp.json();
        if (!data.ok || !data.factors) {
            if (statusEl) statusEl.textContent = '❌ 获取默认权重失败';
            return;
        }
        // 用默认值构造 factors
        var factors = {};
        data.factors.forEach(function(f) {
            factors[f.key] = f.default;
        });
        // 保存默认值
        var saveResp = await fetch('/api/weights/tab/' + tab, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({factors: factors}),
            cache: 'no-store'
        });
        var saveData = await saveResp.json();
        if (saveData.ok) {
            if (statusEl) { statusEl.textContent = '✅ 已重置为默认值'; statusEl.style.color = '#22c55e'; }
            setTimeout(function() { loadWeightsTab(tab); }, 300);
        } else {
            if (statusEl) statusEl.textContent = '❌ ' + (saveData.error || '重置失败');
        }
    } catch(e) {
        if (statusEl) statusEl.textContent = '❌ ' + e.message;
    }
}
window.resetWeights = resetWeights;

// --- 智能权重优化 (2026-07-14) ---
async function optimizeWeights(tab) {
    var statusEl = document.getElementById('weightStatus');
    var resultEl = document.getElementById('optimizeResults');
    if (statusEl) { statusEl.textContent = '⏳ 跑回测中(约30s)...'; statusEl.style.color = 'var(--text-muted)'; }
    if (resultEl) {
        resultEl.style.display = 'block';
        resultEl.innerHTML = '<div style="padding:12px;text-align:center;color:var(--text-muted)">⏳ 正在跑 30 天回测 + IC 分析 + 市场逻辑修正...</div>';
    }

    try {
        var resp = await fetch('/api/weights/optimize/' + tab, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            cache: 'no-store'
        });
        var data = await resp.json();
        if (!data.ok) {
            if (statusEl) { statusEl.textContent = '❌ ' + (data.error || '优化失败'); statusEl.style.color = '#ef4444'; }
            if (resultEl) resultEl.innerHTML = '<div style="padding:12px;color:#ef4444">❌ ' + (data.error || '优化失败') + '</div>';
            return;
        }
        if (resultEl) resultEl.innerHTML = renderOptimizeResults(data, tab);
        if (statusEl) {
            statusEl.textContent = data.improved ? '✅ 优化完成，收益改善' : '⚠️ 优化完成，改善不显著';
            statusEl.style.color = data.improved ? '#22c55e' : '#f59e0b';
        }
        // 自动重新加载权重滑块显示新值
        setTimeout(function() { loadWeightsTab(tab); }, 500);
    } catch(e) {
        if (statusEl) { statusEl.textContent = '❌ ' + e.message; statusEl.style.color = '#ef4444'; }
        if (resultEl) resultEl.innerHTML = '<div style="padding:12px;color:#ef4444">❌ 请求失败: ' + e.message + '</div>';
    }
}
window.optimizeWeights = optimizeWeights;

function renderOptimizeResults(data, tab) {
    var improved = data.improved;
    var summary_old = data.summary_old || {};
    var summary_new = data.summary_new || {};
    var reasons = data.reasons || [];
    var factorIcs = data.factor_ics || {};
    var marketRegime = data.market_regime || 'N/A';
    var sentiment = data.sentiment || {};

    var accent = improved ? '#22c55e' : '#f59e0b';
    var icon = improved ? '✅' : '⚠️';
    var title = improved ? '优化成功 - 回测收益改善' : '优化完成 - 改善不显著';

    var html = '<div style="border:2px solid ' + accent + ';border-radius:8px;padding:14px;background:' + accent + '11">';

    // 标题
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">';
    html += '<span style="font-size:18px">' + icon + '</span>';
    html += '<span style="font-weight:700;font-size:14px;color:' + accent + '">' + title + '</span>';
    html += '</div>';

    // 回测对比表
    html += '<table style="width:100%;font-size:12px;border-collapse:collapse;margin-bottom:10px">';
    html += '<tr style="background:var(--bg-secondary,#1a1f2e)"><th style="padding:6px 8px;text-align:left">指标</th><th style="padding:6px 8px;text-align:right">优化前</th><th style="padding:6px 8px;text-align:right">优化后</th><th style="padding:6px 8px;text-align:right">变化</th></tr>';

    var metrics = [
        {key:'trade_count', label:'交易笔数', fmt:function(v){return (v||0).toFixed(0)}},
        {key:'win_rate', label:'胜率', fmt:function(v){return (v||0).toFixed(1)+'%'}},
        {key:'ev', label:'期望值(EV)', fmt:function(v){return (v||0)>0?'+':''+(v||0).toFixed(2)+'%'}},
        {key:'total_pnl', label:'总盈亏', fmt:function(v){return (v||0)>0?'+':''+(v||0).toFixed(0)}},
        {key:'cumulative_ret', label:'累计收益率', fmt:function(v){return (v||0)>0?'+':''+(v||0).toFixed(2)+'%'}},
        {key:'avg_ret', label:'平均收益', fmt:function(v){return (v||0)>0?'+':''+(v||0).toFixed(2)+'%'}},
        {key:'max_dd', label:'最大回撤', fmt:function(v){return (v||0).toFixed(2)+'%'}},
    ];

    metrics.forEach(function(m) {
        var oldV = m.fmt(summary_old[m.key]);
        var newV = m.fmt(summary_new[m.key]);
        var change = '';
        if (m.key !== 'trade_count' && m.key !== 'max_dd') {
            var diff = (summary_new[m.key]||0) - (summary_old[m.key]||0);
            change = (diff > 0 ? '+' : '') + m.fmt(diff);
        }
        var color = change.startsWith('+') ? '#22c55e' : change.startsWith('-') ? '#ef4444' : 'var(--text-muted)';
        html += '<tr><td style="padding:4px 8px">' + m.label + '</td>';
        html += '<td style="padding:4px 8px;text-align:right;color:var(--text-muted)">' + oldV + '</td>';
        html += '<td style="padding:4px 8px;text-align:right;font-weight:600;color:' + accent + '">' + newV + '</td>';
        html += '<td style="padding:4px 8px;text-align:right;color:' + color + ';font-weight:600">' + change + '</td></tr>';
    });
    html += '</table>';

    // 因子 IC 展示
    var icKeys = Object.keys(factorIcs);
    if (icKeys.length > 0) {
        html += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:6px"><b>因子 IC (与收益相关性)</b></div>';
        html += '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">';
        icKeys.forEach(function(k) {
            var v = factorIcs[k];
            var color = v > 0 ? '#22c55e' : '#ef4444';
            html += '<span style="font-size:11px;padding:3px 8px;border-radius:4px;background:' + color + '22;color:' + color + '">' + k + ' ' + (v > 0 ? '+' : '') + v.toFixed(4) + '</span>';
        });
        html += '</div>';
    }

    // 市场状态
    html += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">';
    html += '📊 市场情绪: ' + (sentiment.level || 'N/A') + ' (' + (sentiment.score || '?') + '/10)';
    html += ' | 市场状态: ' + marketRegime;
    html += '</div>';

    // 调权细节
    if (data.opt_detail) {
        html += '<details style="font-size:11px;color:var(--text-muted);margin-bottom:8px">';
        html += '<summary style="cursor:pointer;font-weight:600">📋 调权明细</summary>';
        html += '<pre style="margin:6px 0 0;padding:8px;background:var(--bg-secondary,#1a1f2e);border-radius:4px;font-size:10px;white-space:pre-wrap">' + escapeHtml(data.opt_detail) + '</pre>';
        html += '</details>';
    }

    // 分析说明
    if (reasons.length > 0) {
        html += '<div style="font-size:11px;padding:8px;background:var(--bg-secondary,#1a1f2e);border-radius:6px;line-height:1.6">';
        html += '<b>' + (improved ? '📈 改善分析' : '📉 未改善原因') + '</b><br>';
        reasons.forEach(function(r) { html += r + '<br>'; });
        html += '</div>';
    }

    // 底部提示
    html += '<div style="font-size:10px;color:var(--text-muted);margin-top:8px">';
    html += '💡 新权重已自动保存。切换回对应 tab 点击「运行」查看实际排行榜。</div>';

    html += '</div>';
    return html;
}
window.renderOptimizeResults = renderOptimizeResults;

