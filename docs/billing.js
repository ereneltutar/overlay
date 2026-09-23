/* ============ helpers ============ */
function escapeHtml(str){
  return String(str == null ? '' : str).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function fmtUsd(n){
  n = n || 0;
  return '$' + n.toLocaleString('en-US', {
    minimumFractionDigits: Math.abs(n) < 1 ? 4 : 2,
    maximumFractionDigits: Math.abs(n) < 1 ? 4 : 2,
  });
}
function fmtInt(n){ return (n || 0).toLocaleString('en-US'); }
function fmtPct(n){ return (n * 100).toFixed(1) + '%'; }
function fmtDateTime(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  if(isNaN(d)) return '—';
  return d.toLocaleString('en-US', { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hourCycle:'h23' });
}
function verdictClass(v){
  switch(v){
    case 'SAFE': return 'safe';
    case 'VETO': return 'veto';
    case 'ERROR': return 'error';
    default: return 'uncertain';
  }
}
function stateHtml(msg, sub){
  return `<div class="state"><div class="state-msg">${escapeHtml(msg)}</div>${sub ? `<div class="state-sub">${escapeHtml(sub)}</div>` : ''}</div>`;
}

/* ============ jsonl parsing ============ */
function parseJsonl(text){
  const rows = [];
  for(const line of text.split('\n')){
    const t = line.trim();
    if(!t) continue;
    try { rows.push(JSON.parse(t)); } catch(e) { /* skip malformed line */ }
  }
  return rows;
}

/* ============ daily spend bar chart ============ */
function renderDailyChart(daily, todayDate){
  const wrap = document.getElementById('dailyBody');
  if(!daily.length){
    wrap.innerHTML = stateHtml('No fact-checks logged yet', 'This fills in once the daily scan places its first calibration/mispricing bet candidate.');
    return;
  }

  const recent = daily.slice(-30); // last 30 days with any activity
  wrap.innerHTML = buildChartAndTable(recent, todayDate, 680, 220, 52);
}

function buildChartAndTable(recent, todayDate, W, H, padL){
  const padR = 10, padT = 14, padB = 34;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxCost = Math.max(...recent.map(d => d.cost_usd), 0.0001);
  const n = recent.length;
  const bw = Math.min(34, plotW / n * 0.6);
  const slot = plotW / n;
  const x = i => padL + i * slot + (slot - bw) / 2;
  const y = v => padT + (1 - v / maxCost) * plotH;

  let bars = '';
  let labels = '';
  recent.forEach((d, i) => {
    const h = Math.max(1, plotH - (y(d.cost_usd) - padT));
    const cls = d.date === todayDate ? 'bar-rect today' : 'bar-rect';
    bars += `<rect class="${cls}" x="${x(i).toFixed(1)}" y="${y(d.cost_usd).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2"><title>${escapeHtml(d.date)} · ${fmtInt(d.calls)} call(s) · ${fmtUsd(d.cost_usd)}</title></rect>`;
    if(n <= 14 || i % Math.ceil(n / 10) === 0 || i === n - 1){
      labels += `<text class="axis-label" x="${(x(i) + bw/2).toFixed(1)}" y="${H - 6}" text-anchor="middle">${escapeHtml(d.date.slice(5))}</text>`;
    }
  });
  for(const t of [0, 0.5, 1]){
    labels += `<line class="grid-line" x1="${padL}" y1="${y(maxCost*t).toFixed(1)}" x2="${padL+plotW}" y2="${y(maxCost*t).toFixed(1)}"></line>`;
    labels += `<text class="axis-label" x="${padL-6}" y="${(y(maxCost*t)+3).toFixed(1)}" text-anchor="end">${fmtUsd(maxCost*t)}</text>`;
  }

  const tableRows = recent.slice().reverse().map(d => `
    <tr>
      <td class="date">${escapeHtml(d.date)}${d.date === todayDate ? ' <span class="today-mark">(today)</span>' : ''}</td>
      <td class="num">${fmtInt(d.calls)}</td>
      <td class="num">${fmtInt(d.vetoes)}</td>
      <td class="num">${fmtInt(d.errors)}</td>
      <td class="num">${fmtInt(d.web_searches)}</td>
      <td class="num">${fmtInt(d.input_tokens + d.output_tokens)}</td>
      <td class="num">${fmtUsd(d.cost_usd)}</td>
    </tr>`).join('');

  return `
    <div class="chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily fact-check cost chart">${labels}${bars}</svg>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Date</th><th class="num">Calls</th><th class="num">Vetoes</th><th class="num">Errors</th>
          <th class="num">Searches</th><th class="num">Tokens</th><th class="num">Cost</th>
        </tr></thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>`;
}

/* ============ recent fact-checks ledger ============ */
function renderLedger(entries){
  const wrap = document.getElementById('ledgerBody');
  const meta = document.getElementById('ledgerMeta');
  if(!entries.length){
    meta.textContent = '0 checks';
    wrap.innerHTML = stateHtml('No fact-checks logged yet');
    return;
  }

  const sorted = entries.slice().sort((a, b) => (b.checked_at || '').localeCompare(a.checked_at || ''));
  const shown = sorted.slice(0, 100);
  meta.textContent = `${fmtInt(entries.length)} total · showing most recent ${fmtInt(shown.length)}`;

  const rows = shown.map(e => `
    <tr>
      <td class="date">${fmtDateTime(e.checked_at)}</td>
      <td class="question">${escapeHtml(e.market_question || '—')}</td>
      <td><span class="side-badge">${escapeHtml(e.recommended_side || '—')}</span></td>
      <td><span class="verdict ${verdictClass(e.verdict)}">${escapeHtml(e.verdict || '?')}</span></td>
      <td class="reason">${escapeHtml(e.reason || '—')}</td>
      <td class="num">${fmtUsd(e.cost_usd)}</td>
    </tr>`).join('');

  wrap.innerHTML = `
    <div class="table-wrap">
      <table class="ledger-table">
        <thead><tr>
          <th>Checked</th><th>Market</th><th>Side</th><th>Verdict</th><th>Reason</th><th class="num">Cost</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

/* ============ header, hero, stat cards ============ */
function renderStatCard(prefix, stats){
  document.getElementById(prefix + 'Cost').textContent = fmtUsd(stats.cost_usd);
  document.getElementById(prefix + 'Sub').innerHTML =
    `<b>${fmtInt(stats.calls)}</b> calls · <b>${fmtInt(stats.vetoes)}</b> vetoed`;
  document.getElementById(prefix + 'Sub2').textContent =
    `${fmtInt(stats.errors)} errored · ${fmtInt(stats.web_searches)} searches`;
}

function renderHeader(data){
  const all = data.all_time || {};
  document.getElementById('tkGenerated').textContent = fmtDateTime(data.generated_at);
  document.getElementById('tkCalls').textContent = fmtInt(all.calls);
  document.getElementById('tkVetoes').textContent = fmtInt(all.vetoes);
  document.getElementById('tkErrors').textContent = fmtInt(all.errors);

  document.getElementById('allTimeAmount').textContent = fmtUsd(all.cost_usd);
  document.getElementById('todayAmount').textContent = fmtUsd((data.today || {}).cost_usd);
  document.getElementById('monthAmount').textContent = fmtUsd((data.month_to_date || {}).cost_usd);
  document.getElementById('avgPerCall').textContent = all.calls ? fmtUsd(all.cost_usd / all.calls) : '—';
  document.getElementById('vetoRate').textContent = all.calls ? fmtPct(all.vetoes / all.calls) : '—';
}

/* ============ main load ============ */
async function loadBudget(){
  try{
    const res = await fetch('./fact_check_budget.json', { cache: 'no-store' });
    if(!res.ok) throw new Error('not found');
    const data = await res.json();

    renderHeader(data);
    renderStatCard('today', data.today);
    renderStatCard('month', data.month_to_date);
    renderStatCard('allTime', data.all_time);

    const pr = data.pricing_reference || {};
    const pricingText = `$${pr.input_token_cost_per_million_usd}/M in · $${pr.output_token_cost_per_million_usd}/M out · $${pr.web_search_cost_per_search_usd}/search`;
    document.getElementById('pricingMeta').textContent = 'generated ' + fmtDateTime(data.generated_at);
    document.getElementById('pricingLine').textContent = pricingText;

    renderDailyChart(data.daily || [], data.today ? data.today.date : null);
  }catch(err){
    ['todayCost','monthCost','allTimeCost'].forEach(id => document.getElementById(id).textContent = '—');
    document.getElementById('dailyBody').innerHTML = stateHtml('Billing data not available yet', "fact_check_budget.json hasn't been generated yet. It's created the first time the daily scan runs the fact-check gate.");
    document.getElementById('pricingMeta').textContent = '—';
  }
}

async function loadLedger(){
  try{
    const res = await fetch('./fact_check_log.jsonl', { cache: 'no-store' });
    if(!res.ok) throw new Error('not found');
    const text = await res.text();
    renderLedger(parseJsonl(text));
  }catch(err){
    document.getElementById('ledgerMeta').textContent = '0 checks';
    document.getElementById('ledgerBody').innerHTML = stateHtml('Ledger not available yet', "fact_check_log.jsonl hasn't been generated yet.");
  }
}

document.getElementById('themeToggle').addEventListener('click', () => {
  const root = document.documentElement;
  const current = root.getAttribute('data-theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const effectiveCurrent = current || (prefersDark ? 'dark' : 'light');
  const next = effectiveCurrent === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  try { localStorage.setItem('overlay-theme', next); } catch(e) {}
});

loadBudget();
loadLedger();
