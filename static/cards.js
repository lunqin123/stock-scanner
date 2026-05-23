function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function barColorClass(pct) {
    if (pct >= 70) return 'high';
    if (pct >= 40) return 'mid';
    return 'low';
}

function scoreRingColor(score) {
    if (score >= 80) return '#34d399';
    if (score >= 70) return '#fbbf24';
    if (score >= 55) return '#fb923c';
    return '#f87171';
}

function renderStockCards(stocks, data) {
    let html = '<div class="card-list">';
    stocks.forEach((stk, i) => {
        const analysis = analyzeStock(stk);
        const ringCol = scoreRingColor(stk.total_score);
        const ringPct = Math.min(100, Math.max(0, stk.total_score));
        const circ = 100; // circumference for r=15.9155 in a 36x36 viewBox
        const dash = (ringPct / 100) * circ;
        const moneySign = stk.net_money >= 0 ? '+' : '';
        const st = stk.seal_time || '0000';
        const stagger = Math.min(i + 1, 10);

        html += `<a href="${escapeHtml(stk.url)}" target="_blank" class="stock-card stagger-${stagger}">`;
        html += `<div class="card-header">`;
        html += `<span class="card-rank">#${stk.rank}</span>`;
        html += `<span class="card-name">${escapeHtml(stk.name)}</span>`;
        html += `<span class="card-code">${escapeHtml(stk.code)}</span>`;
        const watched = isWatched(stk.code);
        html += `<span class="watchlist-btn ${watched ? 'watched' : ''}" data-code="${escapeHtml(stk.code)}" data-name="${escapeHtml(stk.name)}" onclick="toggleWatchBtn(this);return false">${watched ? '✓' : '+'}</span>`;
        html += `<div class="score-ring-wrapper"><svg class="score-ring" viewBox="0 0 36 36">`;
        html += `<path class="score-ring-bg" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"/>`;
        html += `<path class="score-ring-fill" stroke="${ringCol}" stroke-dasharray="${dash} ${circ}" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"/>`;
        html += `<text class="score-ring-text" x="18" y="18">${stk.total_score}</text></svg></div>`;
        html += `</div>`;
        html += '<div class="card-body">';
        html += '<div class="card-bars">';
        const bars = [
            {label:'涨停强度', score:stk.seal_score, max:32},
            {label:'资金面', score:stk.money_score, max:20},
            {label:'板块热度', score:stk.sector_score, max:22},
            {label:'量价关系', score:stk.tech_score, max:10},
            {label:'历史股性', score:stk.history_score, max:5},
            {label:'舆情评分', score:stk.community_score, max:7},
        ];
        for (const b of bars) {
            const pct = Math.min(100, (b.score / b.max) * 100);
            const cls = barColorClass(pct);
            html += `<div class="bar-row"><span class="bar-label">${b.label}</span>`;
            html += `<div class="bar-track"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div>`;
            html += `<span class="bar-val">${b.score}</span></div>`;
        }
        html += '</div>';
        const netPct = stk.base_score > 0 ? ((stk.total_score - stk.base_score) / stk.base_score * 100).toFixed(1) : '0';
        html += '<div class="card-info">';
        html += `<div class="info-row"><span class="label">基础分</span><span class="value">${stk.base_score}</span></div>`;
        html += `<div class="info-row"><span class="label">情绪加成</span><span class="value">+${netPct}%</span></div>`;
        html += `<div class="info-row"><span class="label">净流入</span><span class="value ${stk.net_money >= 0 ? 'green' : 'red'}">${moneySign}${escapeHtml(stk.net_money_str)}</span></div>`;
        html += `<div class="info-row"><span class="label">换手率</span><span class="value">${escapeHtml(stk.turnover)}%</span></div>`;
        html += `<div class="info-row"><span class="label">封板时间</span><span class="value">${st.slice(0,2)}:${st.slice(2)}</span></div>`;
        html += '</div></div>';
        html += '<div class="card-analysis">';
        for (const tag of analysis.tags) {
            html += `<span class="tag ${tag.cls}">${escapeHtml(tag.text)}</span>`;
        }
        html += '</div>';
        if (analysis.notes.length) {
            html += '<div class="card-analysis" style="border-top:none;padding-top:0;margin-top:4px">';
            for (const note of analysis.notes) {
                html += `<span style="font-size:12px;color:var(--text-muted)">${escapeHtml(note)}</span>`;
            }
            html += '</div>';
        }
        html += '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>';
        html += '</a>';
    });
    html += '</div>';
    return html;
}

function renderSectorCards(items) {
    let html = '<div class="card-list">';
    for (const item of items) {
        const effColor = item.efficiency >= 70 ? '#34d399' : item.efficiency >= 50 ? '#fbbf24' : '#f87171';
        html += `<div class="stock-card" style="cursor:default">`;
        html += `<div class="card-header">`;
        html += `<span class="card-rank">#${item.limit_count}只涨停</span>`;
        html += `<span class="card-name">${escapeHtml(item.name)}</span>`;
        html += `<span class="card-score" style="color:${effColor}">${item.score}</span>`;
        html += `</div>`;
        html += '<div class="card-body"><div class="card-bars">';
        const bars = [
            {label:'涨停数', score:item.limit_count, max:Math.max(1, item.limit_count*2)},
            {label:'炸板数', score:item.zhaban_count, max:Math.max(1, item.limit_count+item.zhaban_count)},
        ];
        for (const b of bars) {
            const pct = Math.min(100, (b.score / b.max) * 100);
            html += `<div class="bar-row"><span class="bar-label">${b.label}</span>`;
            html += `<div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>`;
            html += `<span class="bar-val">${b.score}</span></div>`;
        }
        html += '</div>';
        html += '<div class="card-info">';
        html += `<div class="info-row"><span class="label">涨停</span><span class="value green">${item.limit_count}</span></div>`;
        html += `<div class="info-row"><span class="label">炸板</span><span class="value yellow">${item.zhaban_count}</span></div>`;
        html += `<div class="info-row"><span class="label">跌停</span><span class="value red">${item.dieting_count}</span></div>`;
        html += `<div class="info-row"><span class="label">封板率</span><span class="value" style="color:${effColor}">${item.efficiency}%</span></div>`;
        html += '</div></div>';
        html += '</div>';
    }
    html += '</div>';
    return html;
}

function renderSimpleCards(items, pageKey) {
    const titles = {
        'scan-trend': '趋势动量',
        'scan-zhaban': '炸板分析',
        'scan-dtqiaoban': '跌停翘板',
    };
    const hasChange = pageKey === 'scan-trend';

    let html = '<div class="card-list">';
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        html += `<a href="${escapeHtml(item.url)}" target="_blank" class="stock-card">`;
        html += `<div class="card-header">`;
        html += `<span class="card-rank">#${i+1}</span>`;
        html += `<span class="card-name">${escapeHtml(item.name)}</span>`;
        html += `<span class="card-code">${escapeHtml(item.code)}</span>`;
        if (item.change_pct !== undefined) {
            const chgColor = item.change_pct >= 5 ? '#34d399' : item.change_pct >= 3 ? '#fbbf24' : '#f87171';
            html += `<span class="card-score" style="color:${chgColor}">+${item.change_pct}%</span>`;
        }
        html += `</div>`;
        if (item.seal_time) {
            const st = item.seal_time;
            html += `<div style="font-size:12px;color:var(--text-muted);padding:8px 0">封板时间: ${st.slice(0,2)}:${st.slice(2)}</div>`;
        }
        html += '<div class="card-hint"><span>点击查看同花顺详情 →</span></div>';
        html += '</a>';
    }
    html += '</div>';
    return html;
}

function analyzeStock(stk) {
    const tags = [];
    const notes = [];

    // 封板时间分析
    const st = parseInt(stk.seal_time);
    if (st <= 0930) tags.push({text:'开盘秒板', cls:'tag-green'});
    else if (st <= 1000) tags.push({text:'早盘板', cls:'tag-blue'});
    else if (st <= 1100) tags.push({text:'上午板', cls:'tag-blue'});
    else if (st <= 1400) tags.push({text:'午后板', cls:'tag-yellow'});
    else tags.push({text:'尾盘偷袭', cls:'tag-red'});

    // 资金分析
    if (stk.net_money > 1e8) {
        tags.push({text:'主力强', cls:'tag-green'});
    } else if (stk.net_money > 5e7) {
        tags.push({text:'资金正', cls:'tag-blue'});
    } else if (stk.net_money < 0) {
        tags.push({text:'资金流出', cls:'tag-red'});
        notes.push('⚠️ 主力净流出，注意次日抛压');
    }

    // 换手率分析
    const tr = parseFloat(stk.turnover);
    if (tr > 25) {
        tags.push({text:'爆量', cls:'tag-yellow'});
        notes.push('⚠️ 换手过高(>' + tr + '%)，次日接力难度大');
    } else if (tr > 15) {
        tags.push({text:'高换手', cls:'tag-yellow'});
    } else if (tr < 5) {
        tags.push({text:'缩量', cls:'tag-green'});
        notes.push('✅ 缩量涨停(' + tr + '%)，筹码锁定良好');
    }

    // 评分分析
    if (stk.sector_score >= 10) tags.push({text:'板块龙', cls:'tag-green'});
    if (stk.money_score >= 12) tags.push({text:'资金龙', cls:'tag-green'});
    if (stk.seal_score >= 22) tags.push({text:'封板强', cls:'tag-green'});

    // 综合评语
    if (stk.total_score >= 80) notes.push('🔥 综合评分优秀，多因子共振');
    else if (stk.total_score >= 70) notes.push('✅ 评分良好，关注次日竞价');
    else notes.push('📌 评分一般，需结合板块和资金面判断');

    return { tags, notes };
}

function renderStyledText(txt) {
    if (!txt || txt === '(空)') return '<span class="loading">暂无数据</span>';
    const lines = txt.split('\n');
    let html = '<div class="text-report">';
    for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) { html += '<div class="tr-empty"></div>'; continue; }
        // 分隔线
        if (/^[═=]{5,}$/.test(trimmed) || /^[─-]{5,}$/.test(trimmed)) {
            html += '<div class="tr-divider"></div>';
            continue;
        }
        // 带图标的标题行（🚨 📊 💰 等）
        if (/^[【#]/.test(trimmed) || /^[📊📈📉💥🚨💰🔥✅⚠️❌🎯📋🛡️🧩💬🌡️⏱️🏆]/u.test(trimmed)) {
            html += `<div class="tr-section-title">${escapeHtml(trimmed)}</div>`;
            continue;
        }
        // 冒号开头的标签行
        if (/^  [^\s]/.test(line) && trimmed.includes(':')) {
            const parts = trimmed.split(/：(.*)/s);
            if (parts.length >= 2) {
                html += `<div class="tr-label-row"><span class="tr-label">${escapeHtml(parts[0])}</span><span class="tr-value">${escapeHtml(parts[1])}</span></div>`;
                continue;
            }
        }
        // 带 | 分隔的数据行
        if (trimmed.includes('|')) {
            const cells = trimmed.split('|').map(s => s.trim());
            html += '<div class="tr-data-row">';
            for (const c of cells) html += `<span class="tr-cell">${escapeHtml(c)}</span>`;
            html += '</div>';
            continue;
        }
        // 序号行（1. 2. 等）
        if (/^\d+[\.\s、]/.test(trimmed)) {
            html += `<div class="tr-item-row">${escapeHtml(trimmed)}</div>`;
            continue;
        }
        // 普通行
        html += `<div class="tr-line">${escapeHtml(trimmed)}</div>`;
    }
    html += '</div>';
    return html;
}

function renderTableOutput(txt) {
    // 检测是否是表格格式（含 │ 分隔符和 ── 边框）
    const lines = txt.split('\n').filter(l => l.trim());
    if (lines.length < 3) return null;
    if (!lines[0].includes('─')) return null;

    // 找表头行（含 │ 且不含连续 ─）
    let headerIdx = -1, dataStart = -1;
    for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        if (l.includes('│') && !l.includes('┼') && headerIdx === -1) {
            headerIdx = i;
        }
        if (l.includes('┼')) {
            dataStart = i + 1;
        }
    }
    if (headerIdx === -1 || dataStart === -1) return null;

    // 解析表头
    const headers = lines[headerIdx].split('│').map(h => h.trim()).filter(h => h);

    // 解析数据行
    const rows = [];
    for (let i = dataStart; i < lines.length; i++) {
        const l = lines[i];
        if (l.includes('─')) break;  // 到底部边框
        if (!l.includes('│')) continue;
        const cells = l.split('│').map(c => c.trim()).filter(c => c);
        if (cells.length === headers.length) {
            rows.push(cells);
        }
    }
    if (!rows.length) return null;

    // 判断哪些列是数值列（检查第一行数据，排除序号和股票代码）
    const numCols = new Set();
    const centerCols = new Set();
    if (rows.length > 0) {
        for (let i = 0; i < headers.length; i++) {
            const val = (rows[0][i] || '').trim();
            const h = headers[i].trim();
            // 序号列居中
            if (h === '#' || h === '序号') {
                centerCols.add(i);
                continue;
            }
            // 股票代码列居中
            if (h.includes('代码')) {
                centerCols.add(i);
                continue;
            }
            // 数值列右对齐（纯数字、百分比、带亿/万单位）
            if (/^[+-]?(0|[1-9]\d*)(\.\d+)?(%|亿|万)?$/.test(val) && val.length < 12) {
                numCols.add(i);
            }
        }
    }

    // 构建 HTML 表格
    let html = '<div class="table-wrap"><table class="data-table">';
    html += '<thead><tr>';
    for (let i = 0; i < headers.length; i++) {
        const h = headers[i];
        let cls = '';
        if (numCols.has(i)) cls = 'num';
        else if (centerCols.has(i)) cls = 'center';
        html += cls ? `<th class="${cls}">${escapeHtml(h)}</th>` : `<th>${escapeHtml(h)}</th>`;
    }
    html += '</tr></thead><tbody>';
    for (const row of rows) {
        html += '<tr>';
        for (let i = 0; i < row.length; i++) {
            const val = row[i];
            let cls = '';
            if (numCols.has(i)) cls = 'num';
            else if (centerCols.has(i)) cls = 'center';
            html += cls ? `<td class="${cls}">${escapeHtml(val)}</td>` : `<td>${escapeHtml(val)}</td>`;
        }
        html += '</tr>';
    }
    html += '</tbody></table></div>';
    return html;
}