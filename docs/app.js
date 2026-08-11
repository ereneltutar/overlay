/* ============ illustrative example rows (clearly marked, not real market data) ============ */
const DEMO_ENTRIES = [
  {
    type: 'arbitrage',
    event_title: '[EXAMPLE] Who wins the next F1 race?',
    slug: '#', url: '#',
    days_left: 6, num_outcomes: 4, ask_cost: 0.947, total_fee: 0.008, total_cost: 0.955, edge_pct: 4.7,
    min_outcome_liquidity: 310,
    legs: [
      { outcome: 'Verstappen', ask: 0.520, liquidity: 900 },
      { outcome: 'Norris', ask: 0.230, liquidity: 610 },
      { outcome: 'Leclerc', ask: 0.130, liquidity: 420 },
      { outcome: 'Other', ask: 0.067, liquidity: 310 }
    ]
  },
  {
    type: 'calibration',
    event_title: '[EXAMPLE] A long-tail Yes/No market question',
    slug: '#', url: '#',
    days_left: 11, recommended_side: 'NO', current_price: 0.91, implied_cost: 0.09,
    bucket_range: [0.90, 0.95], bucket_sample_size: 41, bucket_historical_rate: 0.83, edge_pct: 8.2
  },
  {
    type: 'mispricing',
    event_title: '[EXAMPLE] A medium-term political outcome market',
    slug: '#', url: '#',
    days_left: 18, recommended_side: 'YES', implied_probability: 0.34, fair_probability: 0.51,
    bucket_range: [0.30, 0.35], bucket_sample_size: 46, edge_pct: 17.0, volume_24h: 8400,
    low_sample_warning: false, score: 62.4
  }
];

/* ============ helpers ============ */
function fmtDate(iso){
  if(!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString('en-US', { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' });
}
function fmtInt(n){ return (n || 0).toLocaleString('en-US'); }

// Market titles/questions/outcomes/slugs come from the public Polymarket
// API (anyone can create a market), so they're untrusted and must be
// escaped before landing in any innerHTML-rendered markup or attribute.
function escapeHtml(str){
  return String(str == null ? '' : str).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/* ============ calibration curve chart ============ */
function renderCalibrationChart(bins){
  const svg = document.getElementById('calChart');
  const W = 680, H = 300;
  const padL = 44, padR = 16, padT = 14, padB = 34;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const x = p => padL + p * plotW;
  const y = r => padT + (1 - r) * plotH;

  let svgParts = [];
  [0, .25, .5, .75, 1].forEach(t => {
    svgParts.push(`<line class="grid-line" x1="${x(t)}" y1="${padT}" x2="${x(t)}" y2="${padT+plotH}"></line>`);
    svgParts.push(`<line class="grid-line" x1="${padL}" y1="${y(t)}" x2="${padL+plotW}" y2="${y(t)}"></line>`);
    svgParts.push(`<text class="axis-label" x="${x(t)}" y="${padT+plotH+16}" text-anchor="middle">%${Math.round(t*100)}</text>`);
    svgParts.push(`<text class="axis-label" x="${padL-8}" y="${y(t)+3}" text-anchor="end">%${Math.round(t*100)}</text>`);
  });
  svgParts.push(`<text class="axis-label" x="${padL+plotW/2}" y="${H-4}" text-anchor="middle">probability the market prices →</text>`);
  svgParts.push(`<path class="diag-line" d="M${x(0)},${y(0)} L${x(1)},${y(1)}"></path>`);

  bins.forEach(b => {
    const midX = x(b.midpoint);
    if(b.resolved_yes_rate === null){
      const ry = padT + plotH;
      const rr = Math.max(2.5, Math.min(7, Math.sqrt(b.sample_size) * 1.15));
      svgParts.push(`<circle class="bucket-dot-pending" cx="${midX}" cy="${ry}" r="${rr}"><title>${Math.round(b.range[0]*100)}–${Math.round(b.range[1]*100)}% bucket · n=${b.sample_size} (needs 30) · still collecting data</title></circle>`);
    } else {
      const cy = y(b.resolved_yes_rate);
      const ciLowY = y(b.ci_95_low), ciHighY = y(b.ci_95_high);
      svgParts.push(`<line class="ci-bar" x1="${midX}" y1="${ciLowY}" x2="${midX}" y2="${ciHighY}"></line>`);
      const rr = Math.max(4, Math.min(11, Math.sqrt(b.sample_size) * 0.62));
      const cls = b.significant ? 'bucket-dot-sig' : 'bucket-dot-pending';
      svgParts.push(`<circle class="${cls}" cx="${midX}" cy="${cy}" r="${rr}"><title>${Math.round(b.range[0]*100)}–${Math.round(b.range[1]*100)}% bucket · n=${b.sample_size} · realized ${(b.resolved_yes_rate*100).toFixed(1)}% · 95% CI [${(b.ci_95_low*100).toFixed(1)}–${(b.ci_95_high*100).toFixed(1)}] · ${b.significant ? 'significant gap' : 'not significant'}</title></circle>`);
    }
  });

  svg.innerHTML = svgParts.join('');

  const strongest = bins.find(b => b.resolved_yes_rate !== null);
  const caption = document.getElementById('calCaption');
  if(strongest){
    const sigCount = bins.filter(b => b.significant).length;
    if(sigCount > 0){
      caption.innerHTML = `<b>${sigCount}</b> buckets show a statistically significant gap. CAL signals come from that gap.`;
    } else {
      caption.innerHTML = `Strongest bucket with enough samples so far: <b>${Math.round(strongest.range[0]*100)}–${Math.round(strongest.range[1]*100)}%</b> (n=${strongest.sample_size}). Realized rate <b>${(strongest.resolved_yes_rate*100).toFixed(2)}%</b>, close to the market's <b>${(strongest.midpoint*100).toFixed(1)}%</b> estimate. No statistically significant gap yet.`;
    }
  } else {
    caption.textContent = 'No bucket has reached 30 samples yet.';
  }
}

function showCalibrationUnavailable(){
  document.getElementById('calMetaDate').textContent = 'not run yet';
  document.getElementById('calBody').innerHTML = `<div class="panel-empty">The weekly calibration scan hasn't run yet. A curve appears here once the "Weekly Calibration Scan" workflow finishes for the first time.</div>`;
}

/* ============ archive sparkline ============ */
function renderSparkline(series){
  const svg = document.getElementById('sparkChart');
  if(series.length < 2){ svg.innerHTML = ''; return; }
  const W = 460, H = 64, padX = 4, padY = 8;
  const plotW = W - padX*2, plotH = H - padY*2;
  const maxVal = Math.max(...series.map(d => d.total));
  const n = series.length;
  const x = i => padX + (i/(n-1)) * plotW;
  const y = v => padY + (1 - v/maxVal) * plotH;

  const linePts = series.map((d,i) => `${x(i).toFixed(1)},${y(d.total).toFixed(1)}`).join(' L');
  const areaPts = `M${x(0)},${H-padY} L` + linePts + ` L${x(n-1)},${H-padY} Z`;
  const last = series[n-1];
  const tooltips = series.map((d,i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(d.total).toFixed(1)}" r="7" fill="transparent"><title>${d.date} · ${d.total} records</title></circle>`).join('');

  svg.innerHTML = `
    <path class="spark-area" d="${areaPts}"></path>
    <path class="spark-line" d="M${linePts}"></path>
    ${tooltips}
    <circle class="spark-end" cx="${x(n-1).toFixed(1)}" cy="${y(last.total).toFixed(1)}" r="3.5"></circle>
  `;
}

/* ============ signals-found-per-day sparkline ============ */
function renderSignalSparkline(series){
  const svg = document.getElementById('signalSparkChart');
  if(series.length < 2){ svg.innerHTML = ''; return; }
  const W = 460, H = 64, padX = 4, padY = 8;
  const plotW = W - padX*2, plotH = H - padY*2;
  const maxVal = Math.max(1, ...series.map(d => d.total));
  const n = series.length;
  const x = i => padX + (i/(n-1)) * plotW;
  const y = v => padY + (1 - v/maxVal) * plotH;

  const linePts = series.map((d,i) => `${x(i).toFixed(1)},${y(d.total).toFixed(1)}`).join(' L');
  const areaPts = `M${x(0)},${H-padY} L` + linePts + ` L${x(n-1)},${H-padY} Z`;
  const last = series[n-1];
  const tooltips = series.map((d,i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(d.total).toFixed(1)}" r="7" fill="transparent"><title>${d.date} · ${d.total} signals</title></circle>`).join('');

  svg.innerHTML = `
    <path class="spark-area" d="${areaPts}"></path>
    <path class="spark-line" d="M${linePts}"></path>
    ${tooltips}
    <circle class="spark-end" cx="${x(n-1).toFixed(1)}" cy="${y(last.total).toFixed(1)}" r="3.5"></circle>
  `;
}

/* ============ ledger entries ============ */
const TAG_LABELS = { arbitrage: 'ARB', calibration: 'CAL', mispricing: 'MIS' };

function daysLabel(d, roundIt){
  if(d === null || d === undefined) return 'date unknown';
  const v = roundIt ? Math.round(d) : d;
  return v <= 0 ? 'closes today' : `${v} days left`;
}

function buildArbBar(legs, totalCost){
  const raw = legs.map(leg => Math.max(leg.ask * 100, 0.3));
  const edgeRaw = Math.max((1 - totalCost) * 100, 0);
  const sum = raw.reduce((a,b) => a+b, 0) + edgeRaw;
  const scale = sum > 0 ? 100/sum : 1;
  const segs = legs.map((leg,i) => {
    const pct = raw[i]*scale;
    const tone = i % 2 === 0 ? 'a' : 'b';
    return `<span class="seg seg-${tone}" style="width:${pct.toFixed(2)}%"></span>`;
  }).join('');
  const edgePct = edgeRaw*scale;
  return `<div class="bar">${segs}<span class="seg" style="width:${edgePct.toFixed(2)}%;background:var(--arb)"></span></div>`;
}

function buildCalBar(item){
  const trueRate = item.recommended_side === 'YES' ? item.bucket_historical_rate : (1 - item.bucket_historical_rate);
  const costPct = Math.max(item.implied_cost*100, 0.3);
  const edgePct = Math.max((trueRate - item.implied_cost)*100, 0);
  const riskPct = Math.max(100 - costPct - edgePct, 0);
  return `<div class="bar">
    <span class="seg seg-a" style="width:${costPct.toFixed(2)}%"></span>
    <span class="seg" style="width:${edgePct.toFixed(2)}%;background:var(--cal)"></span>
    <span class="seg seg-b" style="width:${riskPct.toFixed(2)}%"></span>
  </div>`;
}

function buildMisDumbbell(sig){
  const W = 400, H = 34, padX = 6;
  const x = p => padX + p*(W-padX*2);
  const impX = x(sig.implied_probability), fairX = x(sig.fair_probability);
  const lo = Math.min(impX, fairX), hi = Math.max(impX, fairX);
  return `<div class="dumbbell"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line class="dumbbell-track" x1="${padX}" y1="17" x2="${W-padX}" y2="17"></line>
    <line class="dumbbell-gap" x1="${lo}" y1="17" x2="${hi}" y2="17"></line>
    <circle class="dumbbell-implied" cx="${impX}" cy="17" r="4.5"><title>implied %${(sig.implied_probability*100).toFixed(1)}</title></circle>
    <circle class="dumbbell-fair" cx="${fairX}" cy="17" r="4.5"><title>fair %${(sig.fair_probability*100).toFixed(1)}</title></circle>
    <text class="dumbbell-label" x="${impX}" y="9" text-anchor="middle">implied %${(sig.implied_probability*100).toFixed(0)}</text>
    <text class="dumbbell-label" x="${fairX}" y="32" text-anchor="middle">fair %${(sig.fair_probability*100).toFixed(0)}</text>
  </svg></div>`;
}

function renderArbEntry(opp, rank, demo){
  return `
    <a class="entry" data-tier="arbitrage" href="${escapeHtml(opp.url || '#')}" target="_blank" rel="noopener">
      <div class="entry-head">
        <span class="tag${demo?' demo-tag':''}">${TAG_LABELS.arbitrage}</span>
        <p class="entry-title">${escapeHtml(opp.event_title)}</p>
        <span class="entry-edge">+${opp.edge_pct.toFixed(1)}%</span>
      </div>
      <div class="entry-meta">${daysLabel(opp.days_left)} · ${opp.num_outcomes} outcomes · min liquidity $${fmtInt(Math.round(opp.min_outcome_liquidity))}</div>
      ${buildArbBar(opp.legs, opp.total_cost)}
      <div class="legend">${opp.legs.map((leg,i) => `<span class="legend-chip"><i class="legend-dot" style="background:var(--ink-faint);opacity:${i%2===0?.35:.55}"></i>${escapeHtml(leg.outcome)} <b>$${leg.ask.toFixed(3)}</b></span>`).join('')}</div>
      <div class="entry-foot">
        <span>cost <span class="cost">$${opp.total_cost.toFixed(3)}</span>${opp.total_fee > 0 ? ` (incl. $${opp.total_fee.toFixed(3)} fees)` : ''} / $1.00 payout</span>
        <span class="open">↗ open on polymarket</span>
      </div>
    </a>`;
}

function renderCalEntry(sig, rank, demo){
  const [lo, hi] = sig.bucket_range;
  const trueRatePct = (sig.recommended_side === 'YES' ? sig.bucket_historical_rate : 1 - sig.bucket_historical_rate) * 100;
  return `
    <a class="entry" data-tier="calibration" href="${escapeHtml(sig.url || '#')}" target="_blank" rel="noopener">
      <div class="entry-head">
        <span class="tag${demo?' demo-tag':''}">${TAG_LABELS.calibration}</span>
        <p class="entry-title">${escapeHtml(sig.market_question || sig.event_title)}</p>
        <span class="entry-edge">+${sig.edge_pct.toFixed(1)}%</span>
      </div>
      <div class="entry-meta">recommends ${sig.recommended_side} · ${daysLabel(sig.days_left)} · ${Math.round(lo*100)}–${Math.round(hi*100)}% bucket · historical rate ${(sig.bucket_historical_rate*100).toFixed(1)}% (n=${sig.bucket_sample_size})</div>
      ${buildCalBar(sig)}
      <div class="entry-foot">
        <span>cost <span class="cost">$${sig.implied_cost.toFixed(3)}</span>${sig.fee > 0 ? ` (incl. $${sig.fee.toFixed(3)} fees)` : ''} / historical rate ${trueRatePct.toFixed(1)}%</span>
        <span class="open">↗ open on polymarket</span>
      </div>
    </a>`;
}

function renderMisEntry(sig, rank, demo){
  const [lo, hi] = sig.bucket_range;
  const warn = sig.low_sample_warning ? ' · ⚠ low sample size' : '';
  return `
    <a class="entry" data-tier="mispricing" href="${escapeHtml(sig.url || '#')}" target="_blank" rel="noopener">
      <div class="entry-head">
        <span class="tag${demo?' demo-tag':''}">${TAG_LABELS.mispricing}</span>
        <p class="entry-title">${escapeHtml(sig.market_question || sig.event_title)}</p>
        <span class="entry-edge">${sig.edge_pct.toFixed(1)}pt</span>
      </div>
      <div class="entry-meta">recommends ${sig.recommended_side} · ${daysLabel(sig.days_left, true)} · ${Math.round(lo*100)}–${Math.round(hi*100)}% bucket · n=${sig.bucket_sample_size}${warn}</div>
      ${buildMisDumbbell(sig)}
      <div class="entry-foot">
        <span>24h volume <span class="cost">$${fmtInt(Math.round(sig.volume_24h))}</span> · score ${sig.score}</span>
        <span class="open">↗ open on polymarket</span>
      </div>
    </a>`;
}

function renderEntry(item, i, demo){
  if(item.type === 'calibration') return renderCalEntry(item, i, demo);
  if(item.type === 'mispricing') return renderMisEntry(item, i, demo);
  return renderArbEntry(item, i, demo);
}

/* ============ state ============ */
let currentFilter = 'all';
let showDemo = false;
let merged = [];
let scannedEvents = 0;
const LEDGER_PAGE_SIZE = 25;
let visibleCount = LEDGER_PAGE_SIZE;

function applyFilterAndRender(){
  const grid = document.getElementById('grid');
  const source = showDemo ? DEMO_ENTRIES : merged;
  const list = currentFilter === 'all' ? source : source.filter(x => x.type === currentFilter);

  if(list.length === 0){
    if(showDemo){
      grid.innerHTML = `<div class="state"><div class="state-msg">No examples in this filter</div></div>`;
    } else {
      grid.innerHTML = `<div class="state"><div class="state-msg">No entries of this type today</div><div class="state-sub">The system is running. It scanned ${fmtInt(scannedEvents)} markets. Markets look balanced on this measure today.</div></div>`;
    }
    return;
  }

  const visible = list.slice(0, visibleCount);
  const remaining = list.length - visible.length;
  let html = visible.map((item,i) => renderEntry(item, i, showDemo)).join('');
  if(remaining > 0){
    html += `<div class="show-more-row"><button class="show-more-btn" id="showMoreBtn" type="button">show ${Math.min(remaining, LEDGER_PAGE_SIZE)} more (${remaining} left)</button></div>`;
  }
  grid.innerHTML = html;

  const showMoreBtn = document.getElementById('showMoreBtn');
  if(showMoreBtn){
    showMoreBtn.addEventListener('click', () => {
      visibleCount += LEDGER_PAGE_SIZE;
      applyFilterAndRender();
    });
  }
}

function updateCounts(){
  const source = showDemo ? DEMO_ENTRIES : merged;
  const counts = { arbitrage: 0, calibration: 0, mispricing: 0 };
  source.forEach(x => counts[x.type]++);
  document.getElementById('cnt-all').textContent = source.length;
  document.getElementById('cnt-arbitrage').textContent = counts.arbitrage;
  document.getElementById('cnt-calibration').textContent = counts.calibration;
  document.getElementById('cnt-mispricing').textContent = counts.mispricing;
}

/* ============ price_log.jsonl parsing (for the archive sparkline) ============ */
function parsePriceLog(text){
  const lines = text.split('\n');
  const counts = {};
  let total = 0;
  for(const line of lines){
    const trimmed = line.trim();
    if(!trimmed) continue;
    try{
      const entry = JSON.parse(trimmed);
      const day = (entry.logged_at || '').slice(0, 10);
      if(!day) continue;
      counts[day] = (counts[day] || 0) + 1;
      total++;
    }catch(e){ continue; }
  }
  const days = Object.keys(counts).sort();
  let cum = 0;
  const series = days.map(d => { cum += counts[d]; return { date: d, total: cum }; });
  return { total, series };
}

/* ============ main load ============ */
async function loadResults(){
  const grid = document.getElementById('grid');
  try{
    const res = await fetch('./results.json', { cache: 'no-store' });
    if(!res.ok) throw new Error('not found');
    const data = await res.json();

    scannedEvents = data.scanned_events || 0;
    const arbitrage = (data.opportunities || []).map(o => ({ ...o, type: 'arbitrage' }));
    const calibration = (data.calibration_signals || []).map(s => ({ ...s, type: 'calibration' }));
    const mispricing = (data.mispricing_signals || []).map(s => ({ ...s, type: 'mispricing' }));
    merged = [...arbitrage, ...calibration, ...mispricing].sort((a,b) => b.edge_pct - a.edge_pct);

    document.getElementById('scanTime').textContent = fmtDate(data.generated_at);
    document.getElementById('oppCount').textContent = merged.length;
    document.getElementById('scannedCount').textContent = fmtInt(scannedEvents);
    document.getElementById('tkScanned').textContent = fmtInt(scannedEvents);

    updateCounts();
    applyFilterAndRender();
  }catch(err){
    grid.innerHTML = `<div class="state"><div class="state-msg">No scan yet</div><div class="state-sub">results.json not found. Run the "Daily Polymarket Scan" workflow manually from the GitHub Actions tab, or wait for the first automatic scan to finish.</div></div>`;
  }
}

async function loadCalibration(){
  try{
    const res = await fetch('./calibration.json', { cache: 'no-store' });
    if(!res.ok) throw new Error('not found');
    const data = await res.json();
    document.getElementById('tkResolved').textContent = fmtInt(data.markets_resolved);
    document.getElementById('calMetaDate').textContent = 'last run: ' + fmtDate(data.generated_at);
    renderCalibrationChart(data.bins || []);
  }catch(err){
    showCalibrationUnavailable();
  }
}

async function loadPriceLog(){
  try{
    const res = await fetch('./price_log.jsonl', { cache: 'no-store' });
    if(!res.ok) throw new Error('not found');
    const text = await res.text();
    const { total, series } = parsePriceLog(text);
    document.getElementById('tkArchive').textContent = fmtInt(total);
    document.getElementById('sparkTotal').textContent = fmtInt(total);
    renderSparkline(series);
  }catch(err){
    document.getElementById('tkArchive').textContent = '0';
    document.getElementById('sparkTotal').textContent = '0';
  }
}

async function loadScanHealth(){
  try{
    const res = await fetch('./scan_health.json', { cache: 'no-store' });
    if(!res.ok) throw new Error('not found');
    const data = await res.json();
    const history = data.history || [];
    const series = history.map(h => ({
      date: h.date,
      total: (h.opportunities || 0) + (h.calibration_signals || 0) + (h.mispricing_signals || 0),
    }));
    const avgEl = document.getElementById('signalSparkAvg');
    if(series.length > 0){
      const window = series.slice(-7);
      const avg = window.reduce((s,d) => s + d.total, 0) / window.length;
      avgEl.textContent = avg.toFixed(1);
    } else {
      avgEl.textContent = '0';
    }
    renderSignalSparkline(series);
  }catch(err){
    document.getElementById('signalSparkAvg').textContent = '0';
  }
}

document.getElementById('filters').addEventListener('click', (e) => {
  const btn = e.target.closest('.chip-btn');
  if(!btn) return;
  document.querySelectorAll('.chip-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  currentFilter = btn.dataset.tier;
  visibleCount = LEDGER_PAGE_SIZE;
  applyFilterAndRender();
});

document.getElementById('demoToggle').addEventListener('click', () => {
  showDemo = !showDemo;
  document.getElementById('demoToggle').textContent = showDemo ? '← back to live data' : 'preview with example data →';
  visibleCount = LEDGER_PAGE_SIZE;
  updateCounts();
  applyFilterAndRender();
});

document.getElementById('themeToggle').addEventListener('click', () => {
  const root = document.documentElement;
  const current = root.getAttribute('data-theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const effectiveCurrent = current || (prefersDark ? 'dark' : 'light');
  const next = effectiveCurrent === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  try { localStorage.setItem('overlay-theme', next); } catch(e) {}
});

loadResults();
loadCalibration();
loadPriceLog();
loadScanHealth();
