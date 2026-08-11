function fmtDate(iso){
  if(!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString('en-US', { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' });
}
function fmtShortDate(iso){
  if(!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString('en-US', { day:'2-digit', month:'short', year:'numeric' });
}
function fmtUsd(n){
  const sign = n < 0 ? '-' : '';
  return sign + '$' + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits:2, maximumFractionDigits:2 });
}
function fmtSignedUsd(n){
  return (n > 0 ? '+' : '') + fmtUsd(n);
}
function fmtInt(n){ return (n || 0).toLocaleString('en-US'); }

// Market titles/questions/slugs come from the public Polymarket API
// (anyone can create a market), so they're untrusted and must be
// escaped before landing in any innerHTML-rendered markup or attribute.
function escapeHtml(str){
  return String(str == null ? '' : str).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function wilsonInterval(k, n, z){
  z = z || 1.96;
  if(n === 0) return [0, 0];
  const pHat = k / n;
  const denom = 1 + (z*z) / n;
  const center = (pHat + (z*z) / (2*n)) / denom;
  const margin = (z / denom) * Math.sqrt((pHat * (1 - pHat) / n) + (z*z) / (4*n*n));
  return [Math.max(0, center - margin), Math.min(1, center + margin)];
}

function computeMaxDrawdown(history){
  if(!history || history.length < 2) return 0;
  let peak = history[0].bankroll;
  let maxDrawdownPct = 0;
  for(const h of history){
    if(h.bankroll > peak) peak = h.bankroll;
    if(peak > 0){
      const drawdownPct = (peak - h.bankroll) / peak * 100;
      if(drawdownPct > maxDrawdownPct) maxDrawdownPct = drawdownPct;
    }
  }
  return maxDrawdownPct;
}

const TAG_LABELS = { arbitrage: 'ARB', calibration: 'CAL', mispricing: 'MIS' };
const STATUS_LABELS = { open: 'OPEN', won: 'WON', lost: 'LOST', void: 'VOID' };

let selectedTiers = new Set();
let selectedStatuses = new Set();
let allBets = [];

function renderBankrollChart(history, startingBankroll){
  const svg = document.getElementById('bankrollChart');
  const caption = document.getElementById('bankrollCaption');
  if(history.length < 2){
    svg.innerHTML = '';
    caption.textContent = 'Not enough history yet for a chart. Check back once a few bets have resolved.';
    return;
  }
  const W = 680, H = 220;
  const padL = 54, padR = 16, padT = 14, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  const values = history.map(h => h.bankroll);
  const minV = Math.min(startingBankroll, ...values);
  const maxV = Math.max(startingBankroll, ...values);
  const span = Math.max(maxV - minV, 1);
  const loV = minV - span * 0.08, hiV = maxV + span * 0.08;

  const x = i => padL + (i / (history.length - 1)) * plotW;
  const y = v => padT + (1 - (v - loV) / (hiV - loV)) * plotH;

  const finalUp = values[values.length - 1] >= startingBankroll;
  const lineColor = finalUp ? 'var(--arb)' : 'var(--loss)';

  let parts = [];
  [loV, (loV+hiV)/2, hiV].forEach(v => {
    parts.push(`<line class="grid-line" x1="${padL}" y1="${y(v)}" x2="${padL+plotW}" y2="${y(v)}"></line>`);
    parts.push(`<text class="axis-label" x="${padL-8}" y="${y(v)+3}" text-anchor="end">$${Math.round(v).toLocaleString('en-US')}</text>`);
  });

  const startY = y(startingBankroll);
  parts.push(`<path class="diag-line" d="M${padL},${startY} L${padL+plotW},${startY}"></path>`);

  const linePts = history.map((h,i) => `${x(i).toFixed(1)},${y(h.bankroll).toFixed(1)}`).join(' L');
  const areaPts = `M${x(0)},${padT+plotH} L` + linePts + ` L${x(history.length-1)},${padT+plotH} Z`;
  parts.push(`<path d="${areaPts}" fill="color-mix(in srgb, ${lineColor} 14%, transparent)"></path>`);
  parts.push(`<path d="M${linePts}" stroke="${lineColor}" stroke-width="2" fill="none"></path>`);

  const tooltips = history.map((h,i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(h.bankroll).toFixed(1)}" r="8" fill="transparent"><title>${h.date} · ${fmtUsd(h.bankroll)}</title></circle>`).join('');
  parts.push(tooltips);
  const last = history[history.length-1];
  parts.push(`<circle cx="${x(history.length-1).toFixed(1)}" cy="${y(last.bankroll).toFixed(1)}" r="3.5" fill="${lineColor}"></circle>`);

  svg.innerHTML = parts.join('');
  caption.innerHTML = `<b>${history.length}</b> daily snapshot${history.length===1?'':'s'} since Aug 6 2026. Dashed line = $1,000 starting point.`;
}

function emptyTagStats(){
  return { won:0, lost:0, void:0, open:0, stakeWon:0, stakeLost:0, stakeVoid:0, stakeOpen:0, pnl:0 };
}

function computeStats(bets){
  const byTag = { arbitrage: emptyTagStats(), calibration: emptyTagStats(), mispricing: emptyTagStats() };
  bets.forEach(b => {
    const s = byTag[b.tag];
    s[b.status]++;
    if(b.status === 'won') { s.stakeWon += b.stake_usd; s.pnl += b.pnl_usd; }
    else if(b.status === 'lost') { s.stakeLost += b.stake_usd; s.pnl += b.pnl_usd; }
    else if(b.status === 'void') { s.stakeVoid += b.stake_usd; }
    else if(b.status === 'open') { s.stakeOpen += b.stake_usd; }
  });
  return byTag;
}

// Win rate weighted by dollars staked rather than bet count: what fraction
// of the money put into DECIDED (won+lost) bets ended up in a winner. Can
// diverge a lot from the count-based rate if bigger-edge bets (bigger
// stakes) win or lose disproportionately often.
function stakeWeightedRate(stakeWon, stakeLost){
  const decided = stakeWon + stakeLost;
  return decided > 0 ? stakeWon / decided : null;
}

function renderStatCard(tag, stats){
  const decided = stats.won + stats.lost;
  const decidedStake = stats.stakeWon + stats.stakeLost;
  const rateEl = document.getElementById('rate-' + tag);
  const subEl = document.getElementById('sub-' + tag);
  const sub2El = document.getElementById('sub2-' + tag);
  rateEl.textContent = decided > 0 ? `${((stats.won/decided)*100).toFixed(0)}%` : '—';
  subEl.textContent = `${stats.won}W · ${stats.lost}L · ${stats.void}void · ${stats.open}open`;

  const stakeRate = stakeWeightedRate(stats.stakeWon, stats.stakeLost);
  const stakeRateText = stakeRate !== null ? `${(stakeRate*100).toFixed(0)}% by $` : '';
  const pnlText = decided > 0 ? `${fmtSignedUsd(stats.pnl)} realized on ${fmtUsd(decidedStake)} decided` : 'no resolved bets yet';
  sub2El.textContent = stakeRateText ? `${stakeRateText} · ${pnlText}` : pnlText;
}

const CAL_CHECK_BUCKET_WIDTH = 0.25;
const CAL_CHECK_FILLED_MIN_N = 5;

function renderCalibrationCheck(bets){
  const svg = document.getElementById('calCheckChart');
  const caption = document.getElementById('calCheckCaption');

  const sample = bets.filter(b =>
    (b.tag === 'calibration' || b.tag === 'mispricing') &&
    (b.status === 'won' || b.status === 'lost') &&
    typeof b.predicted_win_prob === 'number'
  );

  if(sample.length === 0){
    svg.innerHTML = '';
    caption.textContent = 'No resolved CAL/MIS bets with a recorded prediction yet. This fills in as bets resolve.';
    return;
  }

  const numBuckets = Math.round(1 / CAL_CHECK_BUCKET_WIDTH);
  const buckets = [];
  for(let i = 0; i < numBuckets; i++){
    const lo = i * CAL_CHECK_BUCKET_WIDTH, hi = lo + CAL_CHECK_BUCKET_WIDTH;
    const inBucket = sample.filter(b => (b.predicted_win_prob >= lo && b.predicted_win_prob < hi) || (i === numBuckets-1 && b.predicted_win_prob === 1));
    const n = inBucket.length;
    const k = inBucket.filter(b => b.status === 'won').length;
    buckets.push({ lo, hi, midpoint: (lo+hi)/2, n, realizedRate: n > 0 ? k/n : null });
  }

  const W = 680, H = 300;
  const padL = 44, padR = 16, padT = 14, padB = 34;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const x = p => padL + p * plotW;
  const y = r => padT + (1 - r) * plotH;

  let parts = [];
  [0, .25, .5, .75, 1].forEach(t => {
    parts.push(`<line class="grid-line" x1="${x(t)}" y1="${padT}" x2="${x(t)}" y2="${padT+plotH}"></line>`);
    parts.push(`<line class="grid-line" x1="${padL}" y1="${y(t)}" x2="${padL+plotW}" y2="${y(t)}"></line>`);
    parts.push(`<text class="axis-label" x="${x(t)}" y="${padT+plotH+16}" text-anchor="middle">${Math.round(t*100)}%</text>`);
    parts.push(`<text class="axis-label" x="${padL-8}" y="${y(t)+3}" text-anchor="end">${Math.round(t*100)}%</text>`);
  });
  parts.push(`<text class="axis-label" x="${padL+plotW/2}" y="${H-4}" text-anchor="middle">predicted win probability →</text>`);
  parts.push(`<path class="diag-line" d="M${x(0)},${y(0)} L${x(1)},${y(1)}"></path>`);

  buckets.forEach(b => {
    if(b.n === 0) return;
    const midX = x(b.midpoint);
    const cy = y(b.realizedRate);
    const rr = Math.max(3, Math.min(12, Math.sqrt(b.n) * 2));
    const cls = b.n >= CAL_CHECK_FILLED_MIN_N ? 'predict-dot-filled' : 'predict-dot-open';
    const lo = Math.round(b.lo*100), hi = Math.round(b.hi*100), rate = (b.realizedRate*100).toFixed(0);
    parts.push(`<circle class="${cls}" cx="${midX}" cy="${cy}" r="${rr}"><title>${lo}-${hi}% predicted · n=${b.n} · realized ${rate}%</title></circle>`);
  });

  svg.innerHTML = parts.join('');

  const avgPredicted = sample.reduce((s,b) => s + b.predicted_win_prob, 0) / sample.length * 100;
  const won = sample.filter(b => b.status === 'won').length;
  const actualRate = (won / sample.length) * 100;
  caption.innerHTML = `Across <b>${sample.length}</b> resolved bet${sample.length===1?'':'s'}, average predicted win probability was ` +
    `<b>${avgPredicted.toFixed(0)}%</b>; actual win rate was <b>${actualRate.toFixed(0)}%</b>.`;
}

function applyFilterAndRender(){
  const grid = document.getElementById('grid');
  const list = allBets
    .filter(b => selectedTiers.size === 0 || selectedTiers.has(b.tag))
    .filter(b => selectedStatuses.size === 0 || selectedStatuses.has(b.status));

  if(list.length === 0){
    grid.innerHTML = allBets.length === 0
      ? `<div class="state"><div class="state-msg">No bets placed yet</div><div class="state-sub">The archive starts filling in once a scan finds an ARB, CAL, or MIS signal. Zero opportunities today just means the market looked balanced. Check back tomorrow.</div></div>`
      : `<div class="state"><div class="state-msg">No bets in this filter</div></div>`;
    return;
  }
  grid.innerHTML = list.map(renderEntry).join('');
}

function renderEntry(b){
  const statusClass = b.status;
  const statusInner = b.status === 'open'
    ? `<span class="pulse"></span>${STATUS_LABELS.open}`
    : STATUS_LABELS[b.status];
  const pnlText = b.status === 'open' ? 'pending'
    : b.status === 'void' ? 'stake returned'
    : fmtSignedUsd(b.pnl_usd);
  const sideText = b.recommended_side ? `${b.recommended_side} · ` : '';
  const dateText = b.status === 'open'
    ? `placed ${fmtShortDate(b.placed_at)} · resolves ~${fmtShortDate(b.deadline)}`
    : `placed ${fmtShortDate(b.placed_at)} · resolved ${fmtShortDate(b.resolved_at)}`;

  return `
    <a class="entry" data-tier="${b.tag}" href="${escapeHtml(b.url || '#')}" target="_blank" rel="noopener">
      <div class="entry-head">
        <span class="tag">${TAG_LABELS[b.tag]}</span>
        <span class="status-badge ${statusClass}">${statusInner}</span>
        <p class="entry-title">${escapeHtml(b.market_question)}</p>
        <span class="entry-pnl ${statusClass}">${pnlText}</span>
      </div>
      <div class="entry-meta">${sideText}entry cost $${b.entry_cost.toFixed(3)} · edge ${b.edge_pct_at_placement.toFixed(1)}% at placement</div>
      <div class="entry-foot">
        <span>stake <span class="stake">${fmtUsd(b.stake_usd)}</span> · ${dateText}</span>
        <span class="open-link">↗ open on polymarket</span>
      </div>
    </a>`;
}

function updateCounts(){
  const counts = { arbitrage: 0, calibration: 0, mispricing: 0 };
  allBets.forEach(b => counts[b.tag]++);
  document.getElementById('cnt-all').textContent = allBets.length;
  document.getElementById('cnt-arbitrage').textContent = counts.arbitrage;
  document.getElementById('cnt-calibration').textContent = counts.calibration;
  document.getElementById('cnt-mispricing').textContent = counts.mispricing;
}

async function loadBetLog(){
  const grid = document.getElementById('grid');
  try{
    const res = await fetch('./bet_log.json', { cache: 'no-store' });
    if(!res.ok) throw new Error('not found');
    const data = await res.json();

    const bankroll = data.bankroll;
    const starting = data.starting_bankroll;
    const returnPct = ((bankroll - starting) / starting) * 100;
    const up = returnPct >= 0;

    const bankrollEl = document.getElementById('bankrollAmount');
    bankrollEl.textContent = fmtUsd(bankroll);
    bankrollEl.className = 'hero-count ' + (up ? 'up' : 'down');

    const returnEl = document.getElementById('returnPct');
    returnEl.textContent = (up ? '+' : '') + returnPct.toFixed(1) + '%';
    returnEl.className = up ? 'up' : 'down';

    allBets = (data.bets || []).slice().sort((a,b) => new Date(b.placed_at) - new Date(a.placed_at));

    const resolved = allBets.filter(b => b.status !== 'open');
    const open = allBets.filter(b => b.status === 'open');
    const openStakeTotal = open.reduce((s,b) => s + b.stake_usd, 0);
    document.getElementById('tkTotal').textContent = fmtInt(allBets.length);
    document.getElementById('tkOpen').textContent = fmtInt(open.length);
    document.getElementById('tkOpenUsd').textContent = fmtUsd(openStakeTotal);
    document.getElementById('tkResolved').textContent = fmtInt(resolved.length);

    document.getElementById('pendingAmount').textContent = `${fmtUsd(openStakeTotal)} (${open.length} bet${open.length===1?'':'s'})`;
    document.getElementById('availableAmount').textContent = fmtUsd(bankroll - openStakeTotal);

    const headline = allBets.filter(b => (b.tag === 'calibration' || b.tag === 'mispricing') && (b.status === 'won' || b.status === 'lost'));
    const headlineWon = headline.filter(b => b.status === 'won').length;
    const headlineEl = document.getElementById('headlineWinRate');
    if(headline.length > 0){
      const [ciLow, ciHigh] = wilsonInterval(headlineWon, headline.length);
      const rate = (headlineWon / headline.length) * 100;
      headlineEl.textContent = `${rate.toFixed(0)}% [${(ciLow*100).toFixed(0)}–${(ciHigh*100).toFixed(0)}%] (n=${headline.length})`;
    } else {
      headlineEl.textContent = 'not enough data yet';
    }

    const headlineStakeWon = headline.filter(b => b.status === 'won').reduce((s,b) => s + b.stake_usd, 0);
    const headlineStakeLost = headline.filter(b => b.status === 'lost').reduce((s,b) => s + b.stake_usd, 0);
    const headlineStakeRate = stakeWeightedRate(headlineStakeWon, headlineStakeLost);
    const headlineStakeEl = document.getElementById('headlineWinRateByStake');
    headlineStakeEl.textContent = headlineStakeRate !== null
      ? `${(headlineStakeRate*100).toFixed(0)}% (${fmtUsd(headlineStakeWon)} won of ${fmtUsd(headlineStakeWon+headlineStakeLost)} decided)`
      : 'not enough data yet';

    const drawdownEl = document.getElementById('maxDrawdown');
    const maxDrawdownPct = computeMaxDrawdown(data.bankroll_history || []);
    drawdownEl.textContent = maxDrawdownPct > 0 ? `-${maxDrawdownPct.toFixed(1)}%` : '—';

    const stats = computeStats(allBets);
    renderStatCard('arbitrage', stats.arbitrage);
    renderStatCard('calibration', stats.calibration);
    renderStatCard('mispricing', stats.mispricing);

    document.getElementById('bankrollMetaDate').textContent = data.bankroll_history && data.bankroll_history.length
      ? 'last snapshot: ' + fmtShortDate(data.bankroll_history[data.bankroll_history.length-1].date)
      : '—';
    renderBankrollChart(data.bankroll_history || [], starting);

    document.getElementById('calCheckMeta').textContent = resolved.length
      ? `${resolved.length} resolved bet${resolved.length===1?'':'s'} total`
      : '—';
    renderCalibrationCheck(allBets);

    updateCounts();
    applyFilterAndRender();
  }catch(err){
    grid.innerHTML = `<div class="state"><div class="state-msg">Archive not available yet</div><div class="state-sub">bet_log.json hasn't been generated yet. It's created the first time the daily scan workflow runs with the paper-trading tracker in place.</div></div>`;
  }
}

function toggleChipFilter(groupId, selectedSet, valueAttr, allValue, btn){
  const value = btn.dataset[valueAttr];
  if(value === allValue){
    selectedSet.clear();
  } else if(selectedSet.has(value)){
    selectedSet.delete(value);
  } else {
    selectedSet.add(value);
  }
  const group = document.getElementById(groupId);
  group.querySelectorAll('.chip-btn').forEach(b => {
    const v = b.dataset[valueAttr];
    const active = v === allValue ? selectedSet.size === 0 : selectedSet.has(v);
    b.classList.toggle('active', active);
  });
}

document.getElementById('filterCategory').addEventListener('click', (e) => {
  const btn = e.target.closest('.chip-btn');
  if(!btn) return;
  toggleChipFilter('filterCategory', selectedTiers, 'tier', 'all', btn);
  applyFilterAndRender();
});

document.getElementById('filterStatus').addEventListener('click', (e) => {
  const btn = e.target.closest('.chip-btn');
  if(!btn) return;
  toggleChipFilter('filterStatus', selectedStatuses, 'status', 'all', btn);
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

loadBetLog();
