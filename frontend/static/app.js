/* icp-lab dashboard — vanilla JS, no build step. */
'use strict';

// ---------- utilities ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 401) { location.href = '/login'; throw new Error('unauthenticated'); }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const pct = (x) => (x == null ? '—' : (x * 100).toFixed(1) + '%');
const num = (x) => (x == null ? '—' : Number(x).toLocaleString());
const dateStr = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—');

const LOW_N = 8;

let toastTimer;
function toast(msg, isErr = false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 2600);
}

// Shared tooltip layer
const tooltip = document.getElementById('tooltip');
function showTooltip(html, x, y) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const pad = 14;
  const r = tooltip.getBoundingClientRect();
  let left = x + pad, top = y + pad;
  if (left + r.width > innerWidth - 8) left = x - r.width - pad;
  if (top + r.height > innerHeight - 8) top = y - r.height - pad;
  tooltip.style.left = left + 'px';
  tooltip.style.top = top + 'px';
}
function hideTooltip() { tooltip.hidden = true; }

function ttRows(title, rows) {
  return `<div class="tt-title">${esc(title)}</div>` +
    rows.map(([k, v]) => `<div class="tt-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
}

// ---------- tabs ----------

document.getElementById('tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-tab]');
  if (!btn) return;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b === btn));
  for (const sec of document.querySelectorAll('main > section')) {
    sec.hidden = sec.id !== 'tab-' + btn.dataset.tab;
  }
});

document.getElementById('logout').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
});

// ---------- scope (work vs job search vs all) ----------
// Threads through every analytics endpoint. Work rows have no tracking links,
// so under the work scope all click metrics are hidden rather than shown as a
// misleading permanent 0%.

const SCOPE_KEY = 'icp_scope';
let scope = localStorage.getItem(SCOPE_KEY);
if (!['work', 'job', 'all'].includes(scope)) scope = 'work';
const workScope = () => scope === 'work';

const scopeToggle = document.getElementById('scope-toggle');
function syncScopeButtons() {
  scopeToggle.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.scope === scope));
}
syncScopeButtons();
scopeToggle.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-scope]');
  if (!btn || btn.dataset.scope === scope) return;
  scope = btn.dataset.scope;
  localStorage.setItem(SCOPE_KEY, scope);
  syncScopeButtons();
  refreshAnalytics();
  loadContacts();
});

// ---------- overview ----------

async function loadStats() {
  try {
    const data = await api(`/api/stats?scope=${scope}`);
    renderKpis(data.overall);
    renderFunnel(data.overall);
    renderChannelTable(data.by_channel);
  } catch (err) {
    document.getElementById('kpis').innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderKpis(o) {
  // work messages carry no tracking link — click tiles would always read 0%
  const clickTiles = workScope() ? '' : `
    <div class="tile"><div class="label">Clicked link</div><div class="value">${pct(o.click_rate)}</div><div class="hint">${num(o.clicked)} contacts</div></div>`;
  const clickConv = workScope() ? '' : `
    <div class="tile"><div class="label">Clicked → responded</div><div class="value">${o.clicked ? pct(o.responded / o.clicked) : '—'}</div><div class="hint">of those who clicked</div></div>`;
  document.getElementById('kpis').innerHTML = `
    <div class="tile"><div class="label">Contacts</div><div class="value">${num(o.contacted)}</div><div class="hint">all rows</div></div>${clickTiles}
    <div class="tile"><div class="label">Responded</div><div class="value">${pct(o.response_rate)}</div><div class="hint">${num(o.responded)} contacts</div></div>${clickConv}`;
}

function renderFunnel(o) {
  // per-bar label ink: dark text on the light funnel step, white on the darker two
  const stages = workScope() ? [
    ['Contacted', o.contacted, 'var(--funnel-1)', '#0b0b0b'],
    ['Responded', o.responded, 'var(--funnel-3)', '#ffffff'],
  ] : [
    ['Contacted', o.contacted, 'var(--funnel-1)', '#0b0b0b'],
    ['Clicked', o.clicked, 'var(--funnel-2)', '#ffffff'],
    ['Responded', o.responded, 'var(--funnel-3)', '#ffffff'],
  ];
  const max = Math.max(o.contacted, 1);
  document.getElementById('funnel').innerHTML = stages.map(([name, val, color, inkOnBar], i) => {
    const w = Math.max((val / max) * 100, 0.5);
    const prev = i > 0 ? stages[i - 1][1] : null;
    let conv = prev ? ` <span class="conv">${pct(val / prev)} of ${esc(stages[i - 1][0].toLowerCase())}</span>` : '';
    // from stage 3 on, also show the rate against the full contacted base
    if (i > 1 && o.contacted) {
      conv += ` <span class="conv">· ${pct(val / o.contacted)} of contacted</span>`;
    }
    // label sits inside the bar when it fits, otherwise just right of it
    const inside = w > 45;
    const labelStyle = inside ? `left:0;color:${inkOnBar}` : `left:${w}%;color:var(--ink)`;
    return `<div class="stage">
      <div class="name">${esc(name)}</div>
      <div class="barwrap">
        <div class="bar" style="width:${w}%;background:${color}"></div>
        <div class="bar-label" style="${labelStyle}">${num(val)}${conv}</div>
      </div>
    </div>`;
  }).join('');
}

function renderChannelTable(rows) {
  const label = { copy: 'LinkedIn DM', email: 'Email' };
  const clickCols = !workScope();
  document.getElementById('channel-table').innerHTML = `<table><thead><tr>
      <th>Channel</th><th class="num">Contacted</th>${clickCols ? `<th class="num">Clicked</th>
      <th class="num">Click rate</th>` : ''}<th class="num">Responded</th><th class="num">Response rate</th>
    </tr></thead><tbody>` +
    rows.map((r) => `<tr>
      <td>${esc(label[r.channel] || r.channel)}</td>
      <td class="num">${num(r.contacted)}</td>
      ${clickCols ? `<td class="num">${num(r.clicked)}</td>
      <td class="num">${pct(r.click_rate)}</td>` : ''}
      <td class="num">${num(r.responded)}</td>
      <td class="num"><b>${pct(r.response_rate)}</b></td>
    </tr>`).join('') + '</tbody></table>';
}

// ---------- timeseries chart ----------

let currentGranularity = 'week';

document.getElementById('granularity').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-g]');
  if (!btn || btn.dataset.g === currentGranularity) return;
  currentGranularity = btn.dataset.g;
  document.querySelectorAll('#granularity button').forEach((b) => b.classList.toggle('active', b === btn));
  loadTimeseries();
});

function periodLabel(iso, granularity) {
  const d = new Date(iso);
  return granularity === 'month'
    ? d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

async function loadTimeseries() {
  const host = document.getElementById('timeseries');
  try {
    const data = await api(`/api/timeseries?granularity=${currentGranularity}&scope=${scope}`);
    renderTimeseries(host, data.periods, data.granularity);
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderTimeseries(host, periods, granularity) {
  if (!periods.length) {
    host.innerHTML = '<div class="loading">No dated contacts yet.</div>';
    return;
  }
  const W = 900, H = 280;
  const M = { l: 46, r: 16 };
  const rateTop = 12, rateBot = 168;      // response-rate line panel
  const volTop = 196, volBot = 250;       // volume bars panel
  const plotW = W - M.l - M.r;
  const n = periods.length;
  const xAt = (i) => M.l + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);

  const maxRate = Math.max(0.05, ...periods.map((p) => p.response_rate || 0));
  const yRate = (r) => rateBot - ((r || 0) / maxRate) * (rateBot - rateTop);
  const maxVol = Math.max(1, ...periods.map((p) => p.contacted));
  const volH = (v) => (v / maxVol) * (volBot - volTop);

  const linePath = periods.map((p, i) => `${i ? 'L' : 'M'}${xAt(i).toFixed(1)},${yRate(p.response_rate).toFixed(1)}`).join('');

  // horizontal gridlines at 0 / mid / max of the rate scale
  const gridVals = [0, maxRate / 2, maxRate];
  const grid = gridVals.map((v) => {
    const y = yRate(v).toFixed(1);
    return `<line class="${v === 0 ? 'axisline' : 'gridline'}" x1="${M.l}" x2="${W - M.r}" y1="${y}" y2="${y}"/>
      <text x="${M.l - 8}" y="${+y + 4}" text-anchor="end">${(v * 100).toFixed(0)}%</text>`;
  }).join('');

  const barW = Math.max(2, Math.min(26, (plotW / n) - 2));
  const bars = periods.map((p, i) =>
    `<rect class="vol-bar" x="${(xAt(i) - barW / 2).toFixed(1)}" y="${(volBot - volH(p.contacted)).toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(volH(p.contacted), 1).toFixed(1)}" rx="2"/>`
  ).join('');

  // x labels: at most ~8, evenly thinned
  const step = Math.ceil(n / 8);
  const xLabels = periods.map((p, i) => (i % step === 0 || i === n - 1)
    ? `<text x="${xAt(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(periodLabel(p.period, granularity))}</text>` : ''
  ).join('');

  // hover: one invisible band per period
  const bandW = plotW / n;
  const bands = periods.map((_, i) =>
    `<rect data-i="${i}" x="${(xAt(i) - bandW / 2).toFixed(1)}" y="0" width="${bandW.toFixed(1)}" height="${H}" fill="transparent"/>`
  ).join('');

  host.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Response rate and contact volume over time">
    ${grid}
    <line class="axisline" x1="${M.l}" x2="${W - M.r}" y1="${volBot}" y2="${volBot}"/>
    <text x="${M.l - 8}" y="${volTop + 8}" text-anchor="end">vol</text>
    ${bars}
    <path class="series-line" d="${linePath}"/>
    <line id="ts-crosshair" class="crosshair" y1="${rateTop}" y2="${volBot}" hidden/>
    <circle id="ts-dot" class="hover-dot" r="4.5" hidden/>
    ${bands}
    ${xLabels}
  </svg>`;

  const svg = host.querySelector('svg');
  const crosshair = svg.querySelector('#ts-crosshair');
  const dot = svg.querySelector('#ts-dot');
  svg.addEventListener('mousemove', (e) => {
    const band = e.target.closest('rect[data-i]');
    if (!band) return;
    const i = +band.dataset.i;
    const p = periods[i];
    const x = xAt(i);
    crosshair.setAttribute('x1', x); crosshair.setAttribute('x2', x);
    crosshair.hidden = false;
    dot.setAttribute('cx', x); dot.setAttribute('cy', yRate(p.response_rate));
    dot.hidden = false;
    showTooltip(ttRows(periodLabel(p.period, granularity), [
      ['Contacted', num(p.contacted)],
      ...(workScope() ? [] : [['Clicked', `${num(p.clicked)} (${pct(p.click_rate)})`]]),
      ['Responded', `${num(p.responded)} (${pct(p.response_rate)})`],
    ]), e.clientX, e.clientY);
  });
  svg.addEventListener('mouseleave', () => {
    crosshair.hidden = true; dot.hidden = true; hideTooltip();
  });
}

// ---------- breakdowns ----------

const DIMS = [
  ['seniority', 'Seniority'],
  ['company_size', 'Company size'],
  ['industry', 'Industry'],
  ['connection_degree', 'Connection'],
  ['country', 'Country'],
  ['target_role', 'Target role'],
  ['premium', 'Premium'],
  ['channel', 'Channel'],
];
const SIZE_ORDER = ['1-10', '11-50', '51-200', '201-1000', '1001-5000', '5000+', 'Unknown'];
let currentDim = 'seniority';

const dimPicker = document.getElementById('dim-picker');
dimPicker.innerHTML = DIMS.map(([k, label]) =>
  `<button data-dim="${k}"${k === currentDim ? ' class="active"' : ''}>${label}</button>`).join('');
dimPicker.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-dim]');
  if (!btn || btn.dataset.dim === currentDim) return;
  currentDim = btn.dataset.dim;
  dimPicker.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
  loadBreakdown();
});

async function loadBreakdown() {
  const host = document.getElementById('breakdown-table');
  host.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const data = await api(`/api/breakdown?dim=${currentDim}&scope=${scope}`);
    let groups = data.groups;
    if (currentDim === 'company_size') {
      groups = [...groups].sort((a, b) => SIZE_ORDER.indexOf(a.grp) - SIZE_ORDER.indexOf(b.grp));
    }
    renderBreakdown(host, groups);
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderBreakdown(host, groups) {
  const clickCols = !workScope();
  document.getElementById('breakdown-legend').hidden = !clickCols;
  if (!groups.length) { host.innerHTML = '<div class="loading">No data.</div>'; return; }
  // one shared scale for both bars, so lengths are comparable across the table
  const scale = Math.max(0.05, ...groups.flatMap((g) => [g.response_rate || 0, clickCols ? g.click_rate || 0 : 0]));
  host.innerHTML = `<table><thead><tr>
      <th>Group</th><th class="num">n</th>
      <th class="bar-cell">Response rate</th>${clickCols ? '<th class="bar-cell">Click rate</th>' : ''}
      <th class="num">Responded</th>${clickCols ? '<th class="num">Clicked</th>' : ''}
    </tr></thead><tbody>` +
    groups.map((g) => {
      const lowN = g.contacted < LOW_N;
      const bar = (rate, cls) => `<div class="rate-bar">
          <div class="track"><div class="fill ${cls}" style="width:${((rate || 0) / scale * 100).toFixed(1)}%"></div></div>
          <span class="pct${lowN ? ' low-n' : ''}">${pct(rate)}</span>
        </div>`;
      return `<tr>
        <td>${esc(g.grp)}${lowN ? '<span class="badge">low n</span>' : ''}</td>
        <td class="num">${num(g.contacted)}</td>
        <td class="bar-cell">${bar(g.response_rate, 'resp')}</td>
        ${clickCols ? `<td class="bar-cell">${bar(g.click_rate, 'click')}</td>` : ''}
        <td class="num">${num(g.responded)}</td>
        ${clickCols ? `<td class="num">${num(g.clicked)}</td>` : ''}
      </tr>`;
    }).join('') + '</tbody></table>';
}

// ---------- ICP finder ----------

const DIM_LABEL = Object.fromEntries(DIMS);
let icpMetric = 'click';

const icpDims = document.getElementById('icp-dims');
icpDims.innerHTML = DIMS.map(([k, label]) => {
  const on = k === 'seniority' || k === 'company_size'; // sensible starting combo
  return `<label><input type="checkbox" value="${k}"${on ? ' checked' : ''}>${label}</label>`;
}).join('');
icpDims.addEventListener('change', loadIcp);

document.getElementById('icp-min-n').addEventListener('change', loadIcp);

document.getElementById('icp-metric').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-m]');
  if (!btn || btn.dataset.m === icpMetric) return;
  icpMetric = btn.dataset.m;
  document.querySelectorAll('#icp-metric button').forEach((b) => b.classList.toggle('active', b === btn));
  loadIcp();
});

function icpSelectedDims() {
  return [...icpDims.querySelectorAll('input:checked')].map((i) => i.value);
}

async function loadIcp() {
  const host = document.getElementById('icp-table');
  const dims = icpSelectedDims();
  if (!dims.length) {
    host.innerHTML = '<div class="loading">Select at least one dimension.</div>';
    return;
  }
  const minN = Math.max(1, parseInt(document.getElementById('icp-min-n').value, 10) || 8);
  // no tracking links on work rows: response rate is the only ranking metric
  const metric = workScope() ? 'response' : icpMetric;
  document.getElementById('icp-metric').hidden = workScope();
  host.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const data = await api(`/api/icp?dims=${dims.join(',')}&min_n=${minN}&metric=${metric}&scope=${scope}`);
    renderIcp(host, data);
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderIcp(host, data) {
  const { dims, groups, min_n } = data;
  if (!groups.length) {
    host.innerHTML = `<div class="loading">No combination of these dimensions reaches n ≥ ${min_n}. Lower the threshold or pick fewer dimensions.</div>`;
    return;
  }
  const clickCols = !workScope();
  const scale = Math.max(0.05, ...groups.flatMap((g) => [clickCols ? g.click_rate || 0 : 0, g.response_rate || 0]));
  const bar = (rate, cls) => `<div class="rate-bar">
      <div class="track"><div class="fill ${cls}" style="width:${((rate || 0) / scale * 100).toFixed(1)}%"></div></div>
      <span class="pct">${pct(rate)}</span>
    </div>`;
  host.innerHTML = `<table><thead><tr>
      <th class="num">#</th>
      ${dims.map((d) => `<th>${esc(DIM_LABEL[d] || d)}</th>`).join('')}
      <th class="num">n</th>
      ${clickCols ? '<th class="bar-cell">Click rate</th>' : ''}<th class="bar-cell">Response rate</th>
      ${clickCols ? '<th class="num">Clicked</th>' : ''}<th class="num">Responded</th>
    </tr></thead><tbody>` +
    groups.map((g, i) => `<tr>
      <td class="num rank">${i + 1}</td>
      ${dims.map((d) => `<td>${esc(g[d])}</td>`).join('')}
      <td class="num">${num(g.contacted)}</td>
      ${clickCols ? `<td class="bar-cell">${bar(g.click_rate, 'click')}</td>` : ''}
      <td class="bar-cell">${bar(g.response_rate, 'resp')}</td>
      ${clickCols ? `<td class="num">${num(g.clicked)}</td>` : ''}
      <td class="num">${num(g.responded)}</td>
    </tr>`).join('') + '</tbody></table>' +
    (groups.length === 50 ? '<div class="sub" style="margin-top:10px">Showing top 50 combinations.</div>' : '');
}

// ---------- enrichment ----------

const ENRICH_KEY_FIELDS = ['title', 'seniority', 'company_name', 'company_size', 'company_industry', 'country'];
const ENRICH_FORM = [
  ['first_name', 'First name', 'text'],
  ['last_name', 'Last name', 'text'],
  ['title', 'Title', 'text', 'dl-titles'],
  ['seniority', 'Seniority', 'combo', 'seniorities'],
  ['company_name', 'Company', 'text', 'dl-orgs'],
  ['company_size', 'Company size', 'number'],
  ['company_industry', 'Industry', 'text', 'dl-industries'],
  ['city', 'City', 'text'],
  ['state', 'State', 'text'],
  ['country', 'Country', 'combo', 'countries'],
  ['years_at_company', 'Years at company', 'number'],
  ['connection_degree', 'Connection', 'select', ['', '1st', '2nd', '3rd']],
  ['premium', 'Premium', 'bool'],
  ['follower_count', 'Followers', 'number'],
  ['departments', 'Departments (comma-sep)', 'text'],
  ['target_role', 'Target role', 'text'],
  ['target_company', 'Target company', 'text'],
  ['channel', 'Channel', 'select', ['', 'copy', 'email']],
  ['contacted_at', 'Contacted date', 'date'],
];

let enrichMeta = null;
let enrichSelectedUid = null;
let enrichFilterMode = 'missing';

const missingCount = (c) => ENRICH_KEY_FIELDS.filter((f) => c[f] == null || c[f] === '').length;

async function loadEnrichMeta() {
  try {
    enrichMeta = await api('/api/enrich-meta');
    const fill = (id, values) => {
      document.getElementById(id).innerHTML =
        values.map((v) => `<option value="${esc(v)}"></option>`).join('');
    };
    fill('dl-industries', enrichMeta.industries);
    fill('dl-titles', enrichMeta.titles);
    document.getElementById('dl-orgs').innerHTML = enrichMeta.orgs.map((o) =>
      `<option value="${esc(o.company_name)}" label="${esc(`${o.n} contact${o.n > 1 ? 's' : ''}${o.company_industry ? ' · ' + o.company_industry : ''}`)}"></option>`
    ).join('');
  } catch (err) {
    // don't clobber the contact list — suggestions are an enhancement
    toast(`Couldn't load org suggestions: ${err.message}`, true);
  }
}

document.getElementById('enrich-search').addEventListener('input', renderEnrichList);
document.getElementById('enrich-filter').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-f]');
  if (!btn || btn.dataset.f === enrichFilterMode) return;
  enrichFilterMode = btn.dataset.f;
  document.querySelectorAll('#enrich-filter button').forEach((b) => b.classList.toggle('active', b === btn));
  renderEnrichList();
});

function enrichQueue() {
  const q = document.getElementById('enrich-search').value.trim().toLowerCase();
  let rows = allContacts.filter((c) => {
    if (enrichFilterMode === 'missing' && missingCount(c) === 0) return false;
    if (!q) return true;
    const hay = [c.first_name, c.last_name, c.company_name].filter(Boolean).join(' ').toLowerCase();
    return q.split(/\s+/).every((w) => hay.includes(w));
  });
  // most incomplete first, then most recently added — a natural work queue
  return rows.sort((a, b) => missingCount(b) - missingCount(a)
    || String(b.created_at || '').localeCompare(String(a.created_at || '')));
}

function renderEnrichList() {
  const host = document.getElementById('enrich-list');
  if (!allContacts.length) { host.innerHTML = '<div class="loading">Loading…</div>'; return; }
  const rows = enrichQueue();
  document.getElementById('enrich-count').textContent =
    `${rows.length} contact${rows.length === 1 ? '' : 's'}`;
  if (!rows.length) { host.innerHTML = '<div class="loading">Nothing to enrich 🎉</div>'; return; }
  host.replaceChildren(...rows.map((c) => {
    const div = document.createElement('div');
    div.className = 'enrich-item' + (c.uid === enrichSelectedUid ? ' active' : '');
    div.dataset.uid = c.uid;
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.uid;
    const miss = missingCount(c);
    div.innerHTML = `${miss ? `<span class="missing-chip">${miss} missing</span>` : ''}
      <div class="who">${esc(name)}</div>
      <div class="org">${esc(c.company_name || c.title || '—')}</div>`;
    div.addEventListener('click', () => selectEnrich(c.uid));
    return div;
  }));
}

function selectEnrich(uid) {
  enrichSelectedUid = uid;
  renderEnrichList();
  const item = document.querySelector(`.enrich-item[data-uid="${CSS.escape(uid)}"]`);
  if (item) item.scrollIntoView({ block: 'nearest' });
  renderEnrichForm();
}

function renderEnrichForm() {
  const host = document.getElementById('enrich-form-wrap');
  const c = allContacts.find((x) => x.uid === enrichSelectedUid);
  if (!c) { host.innerHTML = '<div class="loading">Select a contact to enrich.</div>'; return; }
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.uid;

  const form = document.createElement('form');
  const head = document.createElement('div');
  head.className = 'form-head';
  head.innerHTML = `<h3>${esc(name)}</h3>
    ${c.linkedin_url
      ? `<a class="linkedin-btn" href="${esc(c.linkedin_url)}" target="_blank" rel="noopener noreferrer">Open LinkedIn ↗</a>`
      : '<span class="no-linkedin">no LinkedIn URL on record</span>'}
    <span class="chip">${esc(c.uid)}</span>
    <span class="chip">${num(c.visit_count)} visit${c.visit_count === 1 ? '' : 's'}</span>`;
  form.appendChild(head);

  // open LinkedIn in a separate window (not a tab) sized for a profile
  const lnk = head.querySelector('.linkedin-btn');
  if (lnk) {
    lnk.addEventListener('click', (e) => {
      e.preventDefault();
      window.open(lnk.href, '_blank', 'noopener,noreferrer,width=1250,height=950');
    });
  }

  const grid = document.createElement('div');
  grid.className = 'field-grid';
  const inputs = {};
  const wraps = {};
  for (const [key, label, type, extra] of ENRICH_FORM) {
    const wrap = document.createElement('label');
    wraps[key] = wrap;
    wrap.textContent = label;
    let el;
    if (type === 'select') {
      el = document.createElement('select');
      for (const v of extra) {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = v === '' ? '—' : (v === 'copy' ? 'LinkedIn DM' : v === 'email' ? 'Email' : v);
        el.appendChild(o);
      }
      el.value = c[key] || '';
    } else if (type === 'combo') {
      // dropdown of known values + a Custom… option that reveals free text;
      // the text input always holds the real value the save logic reads
      el = document.createElement('input');
      el.type = 'text';
      el.placeholder = 'custom value';
      const CUSTOM = '__custom__';
      const known = enrichMeta?.[extra] || [];
      const sel = document.createElement('select');
      for (const [v, t] of [['', '—'], ...known.map((k) => [k, k]), [CUSTOM, 'Custom…']]) {
        const o = document.createElement('option');
        o.value = v; o.textContent = t;
        sel.appendChild(o);
      }
      const cur = (c[key] ?? '').trim();
      sel.value = cur === '' ? '' : (known.includes(cur) ? cur : CUSTOM);
      el.value = cur;
      el.hidden = sel.value !== CUSTOM;
      sel.addEventListener('change', () => {
        if (sel.value === CUSTOM) {
          el.hidden = false;
          el.value = '';
          el.focus();
        } else {
          el.hidden = true;
          el.value = sel.value;
        }
      });
      wrap.appendChild(sel);
    } else if (type === 'bool') {
      el = document.createElement('select');
      for (const [v, t] of [['', '—'], ['true', 'yes'], ['false', 'no']]) {
        const o = document.createElement('option');
        o.value = v; o.textContent = t;
        el.appendChild(o);
      }
      el.value = c[key] == null ? '' : String(c[key]);
    } else {
      el = document.createElement('input');
      el.type = type;
      if (type === 'number') el.step = 'any';
      if (extra) el.setAttribute('list', extra);
      el.value = c[key] == null ? '' : (type === 'date' ? String(c[key]).slice(0, 10) : c[key]);
    }
    inputs[key] = el;
    wrap.appendChild(el);
    grid.appendChild(wrap);
  }
  form.appendChild(grid);

  // org autofill: as soon as the typed name matches a known company
  // (case-insensitive), fill size + industry and say so
  const orgHint = document.createElement('div');
  orgHint.className = 'org-hint';
  wraps.company_name.appendChild(orgHint);

  const findOrg = (name) => enrichMeta?.orgs.find(
    (o) => o.company_name.toLowerCase() === name.trim().toLowerCase());

  const applyOrgAutofill = (normalizeSpelling) => {
    const typed = inputs.company_name.value.trim();
    const org = findOrg(typed);
    if (!org) {
      orgHint.textContent = typed && typed !== (c.company_name || '')
        ? 'new company — fill size & industry once and it autofills next time'
        : '';
      return;
    }
    // snap to the DB's canonical spelling so the org groups as one entity
    if (normalizeSpelling && inputs.company_name.value !== org.company_name) {
      inputs.company_name.value = org.company_name;
    }
    const filled = [];
    if (org.company_size != null) {
      inputs.company_size.value = org.company_size;
      inputs.company_size.classList.add('autofilled');
      filled.push('size');
    }
    if (org.company_industry) {
      inputs.company_industry.value = org.company_industry;
      inputs.company_industry.classList.add('autofilled');
      filled.push('industry');
    }
    orgHint.textContent = `known org · ${org.n} contact${org.n > 1 ? 's' : ''}`
      + (filled.length ? ` — autofilled ${filled.join(' & ')}` : ' — no size/industry on file yet');
  };
  inputs.company_name.addEventListener('input', () => applyOrgAutofill(false));
  inputs.company_name.addEventListener('change', () => applyOrgAutofill(true));

  const actions = document.createElement('div');
  actions.className = 'enrich-actions';
  const save = document.createElement('button');
  save.type = 'submit'; save.className = 'save'; save.textContent = 'Save';
  const prev = document.createElement('button');
  prev.type = 'button'; prev.className = 'nav-btn'; prev.textContent = '↑ Prev';
  const next = document.createElement('button');
  next.type = 'button'; next.className = 'nav-btn'; next.textContent = '↓ Next';
  const hint = document.createElement('span');
  hint.className = 'hint'; hint.textContent = 'Only changed fields are saved.';
  actions.append(save, prev, next, hint);
  form.appendChild(actions);

  const step = (dir) => {
    const queue = enrichQueue();
    const i = queue.findIndex((x) => x.uid === enrichSelectedUid);
    const target = queue[i + dir];
    if (target) selectEnrich(target.uid);
  };
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const patch = {};
    for (const [key, , type] of ENRICH_FORM) {
      const raw = inputs[key].value.trim();
      let val;
      if (raw === '') val = null;
      else if (type === 'number') val = Number(raw);
      else if (type === 'bool') val = raw === 'true';
      else val = raw;
      const orig = type === 'date' ? (c[key] ? String(c[key]).slice(0, 10) : null) : (c[key] ?? null);
      const normOrig = type === 'number' && orig != null ? Number(orig) : orig;
      if (val !== normOrig) patch[key] = val;
    }
    if (!Object.keys(patch).length) { toast('No changes'); return; }
    save.disabled = true;
    try {
      const data = await api(`/api/contacts/${encodeURIComponent(c.uid)}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      Object.assign(c, data.contact);
      toast('Saved');
      renderEnrichList();
      renderContacts();
      refreshAnalytics();
      renderEnrichForm(); // re-render so diffing baseline is the saved state
    } catch (err) {
      toast(`Save failed: ${err.message}`, true);
    } finally {
      save.disabled = false;
    }
  });

  host.replaceChildren(form);
}

async function copyMessageAndOpen(p, cardDiv, btn) {
  const role = currentRole();
  const company = jdCompany;
  const first = (p.name || '').split(' ')[0];
  const rest = (p.name || '').split(' ').slice(1).join(' ').replace(/\.$/, '');

  // warn (don't silently proceed) if the message would ship with a gap
  if (!role || !company) {
    if (!confirm(`${!role ? 'Role' : 'Company'} is empty — the message would have a hole in it. Copy anyway?`)) return;
  }

  btn.textContent = 'Working…';
  try {
    // create (or dedupe by linkedin_url) the contact via outreach-backend —
    // that's where uids and tracking links are minted
    if (!p.outreach) {
      p.outreach = await api('/api/outreach-contact', {
        method: 'POST',
        body: JSON.stringify({
          first_name: first,
          last_name: rest || null,
          linkedin_url: p.linkedin_url,
          target_role: role || null,
          target_company: company || null,
        }),
      });
    }
    const msg = buildMessage(first, role, company, p.outreach.tracking_url);
    if (msg.length > MESSAGE_LIMIT) {
      if (!confirm(`Message is ${msg.length} chars (limit ${MESSAGE_LIMIT}). LinkedIn may truncate it. Copy anyway? (Tip: shorten the Role field.)`)) {
        btn.textContent = 'Copy msg + LinkedIn ↗';
        return;
      }
    }
    await navigator.clipboard.writeText(msg);
    window.open(p.linkedin_url, '_blank', 'noopener,noreferrer,width=1250,height=950');
    cardDiv.classList.add('visited');
    btn.textContent = 'Copy msg + LinkedIn ↗';
    toast(`Message copied (${msg.length} chars) · contact ${p.outreach.uid}`);
    // stamp contacted (channel=copy, sets contacted_at) — fire and forget
    api('/api/outreach-contacted', {
      method: 'POST',
      body: JSON.stringify({ uid: p.outreach.uid }),
    }).then(() => { loadContacts(); refreshAnalytics(); }).catch(() => toast('Contacted-stamp failed', true));
  } catch (err) {
    btn.textContent = 'Copy msg + LinkedIn ↗';
    toast(`Failed: ${err.message}`, true);
  }
}

// analytics views cache their data at load; refresh quietly after any edit
function refreshAnalytics() {
  loadStats();
  loadTimeseries();
  loadBreakdown();
  loadIcp();
}

// ---------- prospect finder (JD → scored people) ----------

const jdGo = document.getElementById('jd-go');
const jdStatus = document.getElementById('jd-status');

jdGo.addEventListener('click', async () => {
  const jd = document.getElementById('jd-input').value.trim();
  if (!jd) { toast('Paste a job description first', true); return; }
  const perCategory = Math.max(1, Math.min(25,
    parseInt(document.getElementById('jd-max').value, 10) || 15));
  jdGo.disabled = true;
  jdStatus.textContent = 'Parsing JD with Claude, then searching Apollo… (~15s)';
  document.getElementById('jd-results').innerHTML = '';
  try {
    const data = await api('/api/jd-search', {
      method: 'POST',
      body: JSON.stringify({ job_description: jd, per_category: perCategory }),
    });
    renderProspects(data);
    jdStatus.textContent = '';
    if (document.getElementById('jd-autoreveal').checked) {
      await autoRevealAll(data.categories);
    }
  } catch (err) {
    jdStatus.textContent = '';
    document.getElementById('jd-results').innerHTML =
      `<div class="card"><div class="error-box">${esc(err.message)}</div></div>`;
  } finally {
    jdGo.disabled = false;
  }
});

// The auto-reveal toggle is the consent: with it on, every unrevealed result
// is revealed right after the search — 1 Apollo credit per person.
async function autoRevealAll(categories) {
  const targets = categories.flatMap((c) => c.people).filter((p) => !p.linkedin_url && !p.revealed);
  if (!targets.length) return;
  const host = document.getElementById('jd-results');
  let done = 0, failed = 0;
  for (const p of targets) {
    jdStatus.textContent = `Auto-revealing ${done + failed + 1}/${targets.length} (1 credit each)…`;
    try {
      const d = await api('/api/prospect-reveal', {
        method: 'POST',
        body: JSON.stringify({ id: p.id }),
      });
      Object.assign(p, d);
      const el = host.querySelector(`[data-pid="${CSS.escape(p.id)}"]`);
      if (el) el.replaceWith(prospectCard(p));
      done++;
    } catch {
      failed++;
    }
  }
  // drop the now-pointless per-column Reveal-all buttons
  host.querySelectorAll('.reveal-all').forEach((b) => b.remove());
  jdStatus.textContent = '';
  toast(failed ? `Auto-revealed ${done}, ${failed} failed` : `Auto-revealed ${done} people`, failed > 0);
}

function scoreTooltip(score, name) {
  return ttRows(`${name} — est. click rate ${score.pct}%`, [
    ...score.parts.map((p) => [
      `${p.dim}: ${p.segment}`,
      `${(p.rate * 100).toFixed(1)}% (n=${p.n})`,
    ]),
    ['Tier', score.tier],
  ]);
}

// current search context: company from the parse, role editable by the user
// (abbreviate it to keep the outreach message under 300 chars)
let jdCompany = '';

const MESSAGE_LIMIT = 300;

function buildMessage(first, role, company, link) {
  return `Hi ${first}!\n\nI'm very interested in the ${role} opening at ${company} and wanted to reach out. I'd love to hear about your experience with the company and get any insight you're willing to share.\n\nWould you be open to a quick chat?\n\nTalk soon,\nVaughn\n${link}`;
}

function currentRole() {
  const el = document.getElementById('jd-role');
  return el ? el.value.trim() : '';
}

function updateRoleCounter() {
  const counter = document.getElementById('jd-msg-len');
  if (!counter) return;
  // estimate with a typical first name and a vaughneugenio.com/r/xxx-length
  // tracking link; the exact check happens on copy with the real values
  const len = buildMessage('Firstname', currentRole(), jdCompany, 'x'.repeat(31)).length;
  counter.textContent = `message ≈ ${len} chars${len > MESSAGE_LIMIT ? ' — over 300, shorten the role!' : ''}`;
  counter.style.color = len > MESSAGE_LIMIT ? 'var(--danger)' : 'var(--muted)';
}

function renderProspects(data) {
  const host = document.getElementById('jd-results');
  const { parsed, company_profile: cp, categories } = data;
  const total = categories.reduce((s, c) => s + c.people.length, 0);
  jdCompany = parsed.company_name || '';

  const card = document.createElement('div');
  card.className = 'card';

  const sizeHist = cp.size_history, indHist = cp.industry_history;
  card.innerHTML = `
    <div class="parsed-bar">
      <span class="pill">Company: <b>${esc(parsed.company_name)}</b></span>
      <label class="pill role-pill">Role: <input id="jd-role" value="${esc(parsed.role_title || '')}" title="Editable — this goes into the outreach message. Abbreviate to stay under 300 chars."></label>
      <span class="pill">${esc(parsed.department || '')}</span>
      <span class="pill">${esc(parsed.seniority || '')}</span>
      <span class="pill">${total} people found</span>
      <span class="hint" id="jd-msg-len"></span>
    </div>
    <div class="company-fit">
      Company fit vs your history: size <b>${esc(cp.size_bucket)}</b>
      (${(sizeHist.rate * 100).toFixed(1)}% click, n=${sizeHist.n})${cp.industry ? `,
      industry <b>${esc(cp.industry)}</b> (${(indHist.rate * 100).toFixed(1)}% click, n=${indHist.n})` : ''}
      — your overall click rate is ${(cp.overall_click_rate * 100).toFixed(1)}%.
      Scores below are per-person estimates from seniority + country history; low-n segments are shrunk toward the average.
      Clicking LinkedIn on a revealed card copies your outreach message (with tracking link) and stamps the contact.
    </div>`;

  card.querySelector('#jd-role').addEventListener('input', updateRoleCounter);

  const grid = document.createElement('div');
  grid.className = 'prospect-grid';
  for (const cat of categories) {
    const col = document.createElement('div');
    col.className = 'prospect-col';
    const head = document.createElement('div');
    head.className = 'col-head';
    const h = document.createElement('h3');
    h.textContent = `${cat.label} (${cat.people.length})`;
    head.appendChild(h);

    const unrevealed = cat.people.filter((p) => !p.linkedin_url);
    if (unrevealed.length) {
      const btn = document.createElement('button');
      btn.className = 'reveal-all';
      btn.textContent = `Reveal all · ${unrevealed.length} cr`;
      btn.addEventListener('click', () => revealAll(cat, col, btn));
      head.appendChild(btn);
    }
    col.appendChild(head);

    for (const p of cat.people) col.appendChild(prospectCard(p));
    if (!cat.people.length) {
      const empty = document.createElement('div');
      empty.className = 'loading';
      empty.textContent = 'No matches.';
      col.appendChild(empty);
    }
    grid.appendChild(col);
  }
  card.appendChild(grid);
  host.replaceChildren(card);
  updateRoleCounter();
}

async function revealAll(cat, col, btn) {
  const targets = cat.people.filter((p) => !p.linkedin_url && !p.revealed);
  if (!targets.length) { btn.remove(); return; }
  if (!confirm(`Reveal ${targets.length} people in "${cat.label}"? This spends ${targets.length} Apollo credit${targets.length > 1 ? 's' : ''}.`)) return;
  btn.disabled = true;
  let done = 0, failed = 0;
  for (const p of targets) {
    btn.textContent = `Revealing ${done + failed + 1}/${targets.length}…`;
    try {
      const d = await api('/api/prospect-reveal', {
        method: 'POST',
        body: JSON.stringify({ id: p.id }),
      });
      Object.assign(p, d);
      const el = col.querySelector(`[data-pid="${CSS.escape(p.id)}"]`);
      if (el) el.replaceWith(prospectCard(p));
      done++;
    } catch {
      failed++;
    }
  }
  btn.remove();
  toast(failed ? `Revealed ${done}, ${failed} failed` : `Revealed ${done} people`, failed > 0);
}

function prospectCard(p) {
  const div = document.createElement('div');
  div.className = 'prospect-card';
  div.dataset.pid = p.id;

  const badge = document.createElement('span');
  badge.className = `score-badge ${p.score.tier}`;
  badge.innerHTML = `<span class="dot"></span>${p.score.pct}% · ${p.score.tier === 'strong' ? 'strong fit' : p.score.tier === 'weak' ? 'weak fit' : 'avg fit'}`;
  badge.addEventListener('mousemove', (e) => showTooltip(scoreTooltip(p.score, p.name), e.clientX, e.clientY));
  badge.addEventListener('mouseleave', hideTooltip);

  div.innerHTML = `
    <div class="p-name">${esc(p.name)}</div>
    <div class="p-title">${esc(p.title || '—')}${p.country ? ` · ${esc(p.country)}` : ''}</div>`;

  const row = document.createElement('div');
  row.className = 'p-row';
  row.appendChild(badge);

  if (p.known) {
    const chip = document.createElement('span');
    chip.className = 'known-chip';
    const prefix = p.known.fuzzy ? 'Likely in your DB' : 'In your DB';
    chip.textContent = prefix + (p.known.responded ? ' · responded' : p.known.clicked ? ' · clicked' : '');
    chip.addEventListener('mousemove', (e) => showTooltip(ttRows(
      p.known.fuzzy ? `Probably ${p.known.name} (name-pattern match)` : 'Already contacted', [
      ['uid', p.known.uid],
      ['Clicked', p.known.clicked ? 'yes' : 'no'],
      ['Responded', p.known.responded ? 'yes' : 'no'],
      ['Outcome', p.known.outcome || '—'],
    ]), e.clientX, e.clientY));
    chip.addEventListener('mouseleave', hideTooltip);
    row.appendChild(chip);
  }

  if (p.linkedin_url) {
    const btn = document.createElement('a');
    btn.className = 'p-linkedin';
    btn.href = p.linkedin_url;
    btn.textContent = 'Copy msg + LinkedIn ↗';
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      copyMessageAndOpen(p, div, btn);
    });
    row.appendChild(btn);
  } else {
    // Apollo's free search hides the profile URL; the real one costs 1 credit
    // via people/match — the click on this button IS the confirmation
    const btn = document.createElement('button');
    btn.className = 'p-linkedin p-reveal';
    btn.type = 'button';
    btn.textContent = 'Reveal · 1 credit';
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = 'Revealing…';
      try {
        const d = await api('/api/prospect-reveal', {
          method: 'POST',
          body: JSON.stringify({ id: p.id }),
        });
        Object.assign(p, d); // real name, direct URL, country, re-score
        div.replaceWith(prospectCard(p));
        toast(d.linkedin_url ? 'Revealed' : 'Revealed, but Apollo has no LinkedIn URL for them', !d.linkedin_url);
      } catch (err) {
        toast(`Reveal failed: ${err.message}`, true);
        btn.disabled = false;
        btn.textContent = 'Reveal · 1 credit';
      }
    });
    row.appendChild(btn);
  }

  div.appendChild(row);
  return div;
}

// ---------- work tab (persona → Apollo people search) ----------

const workGo = document.getElementById('work-go');
const workStatus = document.getElementById('work-status');
// current search: parsed titles are passed back on Load more to skip re-parsing
let workSearchCtx = null; // { titles, page, perPage, total, shown, excluded }

async function loadWorkSettings() {
  try {
    const s = await api('/api/work-settings');
    document.getElementById('work-persona').value = s.persona || '';
    document.getElementById('work-locations').value = (s.locations || []).join(', ');
    document.getElementById('work-exclusions').value = (s.exclusions || []).join(', ');
    document.getElementById('work-template').value = s.template || '';
    if (!s.table_ready) {
      document.getElementById('work-settings-status').textContent =
        'app_settings table missing — settings will not persist (one-time SQL in docs/architecture.md).';
    }
    updateWorkTemplateLen();
  } catch { /* defaults stay; the card still works for this session */ }
}

function workFormValues() {
  const split = (id) => document.getElementById(id).value.split(',').map((s) => s.trim()).filter(Boolean);
  return {
    persona: document.getElementById('work-persona').value.trim(),
    locations: split('work-locations'),
    exclusions: split('work-exclusions'),
    template: document.getElementById('work-template').value,
  };
}

document.getElementById('work-save-settings').addEventListener('click', async () => {
  const btn = document.getElementById('work-save-settings');
  btn.disabled = true;
  try {
    await api('/api/work-settings', { method: 'PUT', body: JSON.stringify(workFormValues()) });
    toast('Work settings saved');
  } catch (err) {
    toast(`Save failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
  }
});

function buildWorkMessage(p) {
  const first = (p.name || '').split(' ')[0];
  return document.getElementById('work-template').value
    .replaceAll('{first_name}', first)
    .replaceAll('{company}', p.company_name || '')
    .replaceAll('{title}', p.title || '');
}

function updateWorkTemplateLen() {
  const el = document.getElementById('work-template-len');
  const len = document.getElementById('work-template').value
    .replaceAll('{first_name}', 'Firstname').replaceAll('{company}', 'Some Company Inc')
    .replaceAll('{title}', 'Commercial Insurance Broker').length;
  el.textContent = `message ≈ ${len} chars${len > MESSAGE_LIMIT ? ' — over 300; LinkedIn connection notes get truncated' : ''}`;
  el.style.color = len > MESSAGE_LIMIT ? 'var(--danger)' : 'var(--muted)';
}
document.getElementById('work-template').addEventListener('input', updateWorkTemplateLen);

workGo.addEventListener('click', () => workSearch(false));

async function workSearch(loadMore) {
  const { persona, locations, exclusions } = workFormValues();
  if (!persona) { toast('Describe the persona first', true); return; }
  const perPage = Math.max(1, Math.min(50, parseInt(document.getElementById('work-per-page').value, 10) || 25));
  const page = loadMore ? workSearchCtx.page + 1 : 1;
  workGo.disabled = true;
  workStatus.textContent = loadMore ? 'Loading more…' : 'Expanding persona with Claude, then searching Apollo…';
  try {
    const data = await api('/api/work-search', {
      method: 'POST',
      body: JSON.stringify({
        persona, locations, exclusions, per_page: perPage, page,
        titles: loadMore ? workSearchCtx.titles : null,
      }),
    });
    if (!loadMore) {
      workSearchCtx = { titles: data.titles, page: 1, perPage, total: data.total_entries, shown: 0, excluded: 0 };
      renderWorkResultsShell(data);
    }
    workSearchCtx.page = page;
    workSearchCtx.shown += data.people.length;
    workSearchCtx.excluded += data.excluded_count;
    appendWorkCards(data);
    workStatus.textContent = '';
  } catch (err) {
    workStatus.textContent = '';
    if (!loadMore) {
      document.getElementById('work-results').innerHTML =
        `<div class="card"><div class="error-box">${esc(err.message)}</div></div>`;
    } else {
      toast(`Load more failed: ${err.message}`, true);
    }
  } finally {
    workGo.disabled = false;
  }
}

function renderWorkResultsShell(data) {
  const host = document.getElementById('work-results');
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <div class="parsed-bar">
      ${data.titles.map((t) => `<span class="pill">${esc(t)}</span>`).join('')}
      <span class="pill"><b>${num(data.total_entries)}</b> matches</span>
      <span class="hint" id="work-results-note"></span>
    </div>
    <div class="sub">Apollo's free search masks last names and hides profile URLs — Reveal (1 credit) gets the real profile. "Search ↗" is the free fallback: a LinkedIn people search for the name + title + company. Clicking a revealed card copies your message and stamps the contact.</div>
    <div class="work-list" id="work-list"></div>
    <div class="enrich-actions" id="work-more-wrap" style="margin-top:10px"></div>`;
  host.replaceChildren(card);
}

function appendWorkCards(data) {
  const list = document.getElementById('work-list');
  if (!list) return;
  for (const p of data.people) list.appendChild(workCard(p));
  const note = document.getElementById('work-results-note');
  if (note) {
    note.textContent = `showing ${workSearchCtx.shown}`
      + (workSearchCtx.excluded ? ` · ${workSearchCtx.excluded} excluded (client list)` : '');
  }
  const moreWrap = document.getElementById('work-more-wrap');
  moreWrap.replaceChildren();
  if (workSearchCtx.shown + workSearchCtx.excluded < (workSearchCtx.total || 0)) {
    const btn = document.createElement('button');
    btn.className = 'nav-btn';
    btn.textContent = 'Load more';
    btn.addEventListener('click', () => workSearch(true));
    moreWrap.appendChild(btn);
  }
  if (!workSearchCtx.shown) {
    list.innerHTML = '<div class="loading">No matches (after exclusions).</div>';
  }
}

function workCard(p) {
  const div = document.createElement('div');
  div.className = 'prospect-card';
  div.dataset.pid = p.id;

  div.innerHTML = `
    <div class="p-name">${esc(p.name)}</div>
    <div class="p-title">${esc(p.title || '—')}${p.company_name ? ` · ${esc(p.company_name)}` : ''}${p.country ? ` · ${esc(p.country)}` : ''}</div>`;

  const row = document.createElement('div');
  row.className = 'p-row';

  if (p.known) {
    const chip = document.createElement('span');
    chip.className = 'known-chip';
    const prefix = p.known.fuzzy ? 'Likely in your DB' : 'In your DB';
    chip.textContent = prefix + (p.known.responded ? ' · responded' : '');
    chip.title = `${p.known.name} (${p.known.uid})${p.known.outcome ? ' · ' + p.known.outcome : ''}`;
    row.appendChild(chip);
  }

  // free fallback while unrevealed (or if Apollo has no URL): LinkedIn search
  if (!p.linkedin_url && p.linkedin_search_url) {
    const a = document.createElement('a');
    a.className = 'work-search-link';
    a.href = p.linkedin_search_url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = 'Search ↗';
    a.title = 'Free: LinkedIn people search for this name + title + company';
    row.appendChild(a);
  }

  if (p.linkedin_url) {
    const btn = document.createElement('a');
    btn.className = 'p-linkedin';
    btn.href = p.linkedin_url;
    btn.textContent = 'Copy msg + LinkedIn ↗';
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      copyWorkMessageAndOpen(p, div, btn);
    });
    row.appendChild(btn);
  } else {
    const btn = document.createElement('button');
    btn.className = 'p-linkedin p-reveal';
    btn.type = 'button';
    btn.textContent = 'Reveal · 1 credit';
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = 'Revealing…';
      try {
        const d = await api('/api/prospect-reveal', { method: 'POST', body: JSON.stringify({ id: p.id }) });
        // keep company_name from the search card — reveal doesn't return it
        Object.assign(p, { ...d, company_name: p.company_name, score: undefined });
        div.replaceWith(workCard(p));
        toast(d.linkedin_url ? 'Revealed' : 'Revealed, but Apollo has no LinkedIn URL for them', !d.linkedin_url);
      } catch (err) {
        toast(`Reveal failed: ${err.message}`, true);
        btn.disabled = false;
        btn.textContent = 'Reveal · 1 credit';
      }
    });
    row.appendChild(btn);
  }

  div.appendChild(row);
  return div;
}

async function copyWorkMessageAndOpen(p, cardDiv, btn) {
  const parts = (p.name || '').split(' ');
  const first = parts[0];
  const rest = parts.slice(1).join(' ').replace(/\.$/, '');
  btn.textContent = 'Working…';
  try {
    if (!p.outreach) {
      p.outreach = await api('/api/outreach-contact', {
        method: 'POST',
        body: JSON.stringify({
          first_name: first,
          last_name: rest || null,
          linkedin_url: p.linkedin_url,
          purpose: 'work',
        }),
      });
      // enrich the fresh row from what the search/reveal already told us,
      // so work contacts don't land as a wall of Unknowns
      const enrich = {};
      if (p.title) enrich.title = p.title;
      if (p.company_name) enrich.company_name = p.company_name;
      if (p.country) enrich.country = p.country;
      if (p.seniority && p.seniority !== 'Unknown') enrich.seniority = p.seniority;
      if (Object.keys(enrich).length) {
        api(`/api/contacts/${encodeURIComponent(p.outreach.uid)}`, {
          method: 'PATCH',
          body: JSON.stringify(enrich),
        }).catch(() => {});
      }
    }
    const msg = buildWorkMessage(p);
    if (msg.length > MESSAGE_LIMIT) {
      if (!confirm(`Message is ${msg.length} chars (limit ${MESSAGE_LIMIT} for connection notes). Copy anyway?`)) {
        btn.textContent = 'Copy msg + LinkedIn ↗';
        return;
      }
    }
    await navigator.clipboard.writeText(msg);
    window.open(p.linkedin_url, '_blank', 'noopener,noreferrer,width=1250,height=950');
    cardDiv.classList.add('visited');
    btn.textContent = 'Copy msg + LinkedIn ↗';
    toast(`Message copied (${msg.length} chars) · contact ${p.outreach.uid}`);
    api('/api/outreach-contacted', {
      method: 'POST',
      body: JSON.stringify({ uid: p.outreach.uid }),
    }).then(() => { loadContacts(); refreshAnalytics(); }).catch(() => toast('Contacted-stamp failed', true));
  } catch (err) {
    btn.textContent = 'Copy msg + LinkedIn ↗';
    toast(`Failed: ${err.message}`, true);
  }
}

// ---------- contacts ----------

const OUTCOMES = ['call', 'referral', 'ghost', 'rejected', 'other'];
let allContacts = [];

document.getElementById('contact-search').addEventListener('input', () => renderContacts());

async function loadContacts() {
  const host = document.getElementById('contacts-table');
  try {
    const data = await api(`/api/contacts?scope=${scope}`);
    allContacts = data.contacts;
    renderContacts();
    renderEnrichList();
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function contactMatches(c, q) {
  if (!q) return true;
  const hay = [c.first_name, c.last_name, c.company_name, c.title, c.target_role, c.target_company, c.outcome]
    .filter(Boolean).join(' ').toLowerCase();
  return q.split(/\s+/).every((w) => hay.includes(w));
}

function renderContacts() {
  const host = document.getElementById('contacts-table');
  const q = document.getElementById('contact-search').value.trim().toLowerCase();
  const rows = allContacts.filter((c) => contactMatches(c, q));
  document.getElementById('contact-count').textContent =
    q ? `${rows.length} of ${allContacts.length} contacts` : `${allContacts.length} contacts`;

  const table = document.createElement('table');
  table.innerHTML = `<thead><tr>
    <th>Name</th><th>Title</th><th>Company</th><th>Target</th><th>Channel</th>
    <th>Purpose</th>
    <th>Contacted</th>${workScope() ? '' : '<th class="num">Visits</th>'}
    <th>Responded</th><th>Responded at</th><th>Outcome</th>
    <th>Added by</th><th>Last edited by</th>
  </tr></thead>`;
  const tbody = document.createElement('tbody');
  for (const c of rows) tbody.appendChild(contactRow(c));
  table.appendChild(tbody);
  host.replaceChildren(table);
}

function contactRow(c) {
  const tr = document.createElement('tr');
  tr.dataset.uid = c.uid;

  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.uid;
  const target = [c.target_role, c.target_company].filter(Boolean).join(' @ ');
  const chLabel = c.channel === 'copy' ? 'DM' : c.channel === 'email' ? 'Email' : (c.channel || '—');

  tr.innerHTML = `
    <td class="name-cell">${c.linkedin_url
      ? `<a href="${esc(c.linkedin_url)}" target="_blank" rel="noopener noreferrer">${esc(name)}</a>`
      : esc(name)}</td>
    <td class="title-cell" title="${esc(c.title || '')}">${esc(c.title || '—')}</td>
    <td class="title-cell">${esc(c.company_name || '—')}</td>
    <td class="title-cell" title="${esc(target)}">${esc(target || '—')}</td>
    <td><span class="chip">${esc(chLabel)}</span></td>`;

  // purpose select (null = job search, 'work' = work outreach)
  const tdPurpose = document.createElement('td');
  const psel = document.createElement('select');
  for (const [v, label] of [['', 'job'], ['work', 'work']]) {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = label;
    psel.appendChild(opt);
  }
  psel.value = c.purpose === 'work' ? 'work' : '';
  psel.addEventListener('change', () => saveContact(tr, c, { purpose: psel.value || null }));
  tdPurpose.appendChild(psel);
  tr.appendChild(tdPurpose);

  const tdContacted = document.createElement('td');
  tdContacted.title = c.contacted_at || '';
  tdContacted.textContent = dateStr(c.contacted_at);
  tr.appendChild(tdContacted);

  if (!workScope()) {
    const tdVisits = document.createElement('td');
    tdVisits.className = 'num';
    tdVisits.innerHTML = c.visit_count > 0 ? `<b>${num(c.visit_count)}</b>` : '0';
    tr.appendChild(tdVisits);
  }

  // responded checkbox
  const tdResp = document.createElement('td');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = c.responded === true;
  cb.addEventListener('change', () => saveContact(tr, c, { responded: cb.checked }));
  tdResp.appendChild(cb);
  tr.appendChild(tdResp);

  // responded_at date (editable backfill)
  const tdDate = document.createElement('td');
  const di = document.createElement('input');
  di.type = 'date';
  di.value = c.responded_at ? c.responded_at.slice(0, 10) : '';
  di.addEventListener('change', () => saveContact(tr, c, { responded_at: di.value || null }));
  tdDate.appendChild(di);
  tr.appendChild(tdDate);

  // outcome select
  const tdOut = document.createElement('td');
  const sel = document.createElement('select');
  const opts = ['', ...OUTCOMES];
  if (c.outcome && !OUTCOMES.includes(c.outcome)) opts.push(c.outcome); // legacy free-text value
  for (const o of opts) {
    const opt = document.createElement('option');
    opt.value = o;
    opt.textContent = o === '' ? '—' : (OUTCOMES.includes(o) ? o : `${o} (legacy)`);
    sel.appendChild(opt);
  }
  sel.value = c.outcome || '';
  sel.addEventListener('change', () => {
    if (sel.value && !OUTCOMES.includes(sel.value)) return; // legacy option is display-only
    saveContact(tr, c, { outcome: sel.value || null });
  });
  tdOut.appendChild(sel);
  tr.appendChild(tdOut);

  // attribution (NULL = Chrome extension / pre-multi-user rows)
  const tdAdded = document.createElement('td');
  tdAdded.className = 'muted-cell';
  tdAdded.textContent = c.created_by_name || '—';
  tr.appendChild(tdAdded);
  const tdEdited = document.createElement('td');
  tdEdited.className = 'muted-cell';
  tdEdited.textContent = c.updated_by_name || '—';
  if (c.updated_at) tdEdited.title = new Date(c.updated_at).toLocaleString();
  tr.appendChild(tdEdited);

  return tr;
}

async function saveContact(tr, c, patch) {
  tr.classList.add('saving');
  try {
    const data = await api(`/api/contacts/${encodeURIComponent(c.uid)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    Object.assign(c, data.contact); // server truth: responded, responded_at, outcome, updated_by_name
    tr.replaceWith(contactRow(c));
    toast('Saved');
    refreshAnalytics();
  } catch (err) {
    tr.replaceWith(contactRow(c)); // revert to last known state
    toast(`Save failed: ${err.message}`, true);
  } finally {
    tr.classList.remove('saving');
  }
}

// ---------- replies (Gmail LinkedIn-notification scan) ----------

let repliesState = null;

async function loadReplies(kickScan = true) {
  try {
    repliesState = await api('/api/replies');
  } catch {
    return; // scanner is optional — never block the dashboard on it
  }
  renderReplies();
  if (kickScan && repliesState.configured && repliesState.table_ready) autoScanReplies();
}

// quiet page-load scan; the server throttles these to one per 30 minutes
async function autoScanReplies() {
  try {
    const s = await api('/api/replies/scan', { method: 'POST', body: JSON.stringify({ auto: true }) });
    if (s.throttled) return;
    afterScan(s);
  } catch { /* quiet — the manual button surfaces errors */ }
}

function afterScan(s) {
  if (s.auto_applied.length) {
    toast(`${s.auto_applied.length} repl${s.auto_applied.length === 1 ? 'y' : 'ies'} auto-marked responded`);
    loadContacts();
    refreshAnalytics();
  }
  loadReplies(false);
}

document.getElementById('replies-scan').addEventListener('click', async () => {
  const btn = document.getElementById('replies-scan');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try {
    // manual scans look back further — useful for the initial backfill
    const s = await api('/api/replies/scan', { method: 'POST', body: JSON.stringify({ days: 180 }) });
    toast(`${s.new_events} new notification${s.new_events === 1 ? '' : 's'} · ${s.auto_applied.length} auto-applied · ${s.pending_new} to review`);
    afterScan(s);
  } catch (err) {
    toast(`Scan failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Scan Gmail (6 mo)';
  }
});

function renderReplies() {
  const card = document.getElementById('replies-card');
  const sub = document.getElementById('replies-sub');
  const body = document.getElementById('replies-body');
  if (!repliesState || !repliesState.configured) { card.hidden = true; return; }
  card.hidden = false;

  if (!repliesState.table_ready) {
    sub.textContent = 'Gmail is connected, but the reply_events table is missing — run the one-time SQL in docs/architecture.md.';
    body.replaceChildren();
    return;
  }
  sub.textContent = 'LinkedIn "sent you a message" emails in Gmail. An exact name match to one contact flips responded automatically; ambiguous matches wait here.'
    + (repliesState.last_scan ? ` Last scan: ${new Date(repliesState.last_scan).toLocaleString()}.` : '');

  body.replaceChildren();

  if (!repliesState.pending.length) {
    const d = document.createElement('div');
    d.className = 'loading';
    d.textContent = 'No replies awaiting review.';
    body.appendChild(d);
  } else {
    const table = document.createElement('table');
    table.innerHTML = '<thead><tr><th>Sender</th><th>Received</th><th>Message</th><th>Match to</th><th></th></tr></thead>';
    const tbody = document.createElement('tbody');
    for (const ev of repliesState.pending) tbody.appendChild(replyRow(ev));
    table.appendChild(tbody);
    body.appendChild(table);
  }

  if (repliesState.recent_auto.length) {
    const recent = document.createElement('div');
    recent.className = 'sub';
    recent.style.marginTop = '10px';
    recent.textContent = 'Recently applied: ' + repliesState.recent_auto.map((r) =>
      `${r.sender_name || r.matched_name} → ${r.matched_name} (${dateStr(r.received_at)})`).join(' · ');
    body.appendChild(recent);
  }
}

function replyRow(ev) {
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><b>${esc(ev.sender_name)}</b></td>
    <td>${dateStr(ev.received_at)}</td>
    <td class="title-cell" title="${esc(ev.snippet || '')}">${esc(ev.snippet || '—')}</td>`;

  // candidate picker — pending events always carry at least one candidate
  const tdSel = document.createElement('td');
  const sel = document.createElement('select');
  for (const c of ev.candidates) {
    const o = document.createElement('option');
    o.value = c.uid;
    o.textContent = `${c.name}${c.company ? ' · ' + c.company : ''} (${c.uid})`
      + (c.responded ? ' — already responded' : '');
    sel.appendChild(o);
  }
  tdSel.appendChild(sel);
  tr.appendChild(tdSel);

  const tdAct = document.createElement('td');
  const confirmBtn = document.createElement('button');
  confirmBtn.className = 'save';
  confirmBtn.textContent = 'Confirm';
  confirmBtn.addEventListener('click', async () => {
    confirmBtn.disabled = true;
    try {
      await api(`/api/replies/${encodeURIComponent(ev.gmail_id)}/confirm`, {
        method: 'POST',
        body: JSON.stringify({ uid: sel.value }),
      });
      toast('Marked responded');
      loadContacts();
      refreshAnalytics();
      loadReplies(false);
    } catch (err) {
      toast(`Failed: ${err.message}`, true);
      confirmBtn.disabled = false;
    }
  });
  const dismissBtn = document.createElement('button');
  dismissBtn.className = 'nav-btn';
  dismissBtn.textContent = 'Dismiss';
  dismissBtn.addEventListener('click', async () => {
    dismissBtn.disabled = true;
    try {
      await api(`/api/replies/${encodeURIComponent(ev.gmail_id)}/dismiss`, { method: 'POST' });
      loadReplies(false);
    } catch (err) {
      toast(`Failed: ${err.message}`, true);
      dismissBtn.disabled = false;
    }
  });
  tdAct.className = 'reply-actions';
  tdAct.append(confirmBtn, dismissBtn);
  tr.appendChild(tdAct);
  return tr;
}

// ---------- admin (user accounts + per-user usage) ----------

let me = null;

async function loadMe() {
  me = await api('/api/me');
  document.getElementById('whoami').textContent = me.username;
  if (me.is_admin) {
    document.getElementById('admin-tab-btn').hidden = false;
    loadAdminUsers();
    loadAdminUsage();
  }
  if (!me.can_spend) {
    // Claude + Apollo calls are gated server-side too (403); this just
    // explains why the buttons are off.
    const note = 'Searching and Reveal are off for your account — ask Vaughn to enable spending.';
    for (const [btnId, statusId] of [['jd-go', 'jd-status'], ['work-go', 'work-status']]) {
      document.getElementById(btnId).disabled = true;
      document.getElementById(statusId).textContent = note;
    }
    const auto = document.getElementById('jd-autoreveal');
    if (auto) auto.disabled = true;
  }
}

async function loadAdminUsers() {
  const host = document.getElementById('admin-users');
  try {
    const { users } = await api('/api/admin/users');
    const table = document.createElement('table');
    table.innerHTML = `<thead><tr>
      <th>Username</th><th>Role</th><th>Can spend</th><th>Status</th>
      <th class="num">Added</th><th class="num">Last edited</th>
      <th>Created</th><th>Last login</th><th></th>
    </tr></thead>`;
    const tbody = document.createElement('tbody');
    for (const u of users) tbody.appendChild(adminUserRow(u));
    table.appendChild(tbody);
    host.replaceChildren(table);
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function adminUserRow(u) {
  const tr = document.createElement('tr');
  const self = me && u.username === me.username;
  tr.innerHTML = `
    <td><b>${esc(u.username)}</b></td>
    <td>${u.is_admin ? '<span class="chip">admin</span>' : 'user'}</td>`;

  const tdSpend = document.createElement('td');
  const spend = document.createElement('input');
  spend.type = 'checkbox';
  spend.checked = u.can_spend;
  spend.title = 'Allow Claude searches and Apollo Reveal (credits)';
  spend.addEventListener('change', () => adminPatch(u, { can_spend: spend.checked }));
  tdSpend.appendChild(spend);
  tr.appendChild(tdSpend);

  const tdStatus = document.createElement('td');
  tdStatus.textContent = u.disabled_at ? `disabled ${dateStr(u.disabled_at)}` : 'active';
  tr.appendChild(tdStatus);

  for (const n of [u.contacts_created, u.contacts_last_edited]) {
    const td = document.createElement('td');
    td.className = 'num';
    td.textContent = num(n);
    tr.appendChild(td);
  }
  for (const d of [u.created_at, u.last_login_at]) {
    const td = document.createElement('td');
    td.textContent = dateStr(d);
    tr.appendChild(td);
  }

  const tdActions = document.createElement('td');
  tdActions.className = 'admin-actions';
  const reset = document.createElement('button');
  reset.className = 'nav-btn';
  reset.textContent = 'Set password';
  reset.addEventListener('click', () => {
    const pw = prompt(`New password for ${u.username} (min 10 characters):`);
    if (pw) adminPatch(u, { password: pw }, 'Password updated');
  });
  tdActions.appendChild(reset);
  if (!self) {
    const toggle = document.createElement('button');
    toggle.className = 'nav-btn';
    toggle.textContent = u.disabled_at ? 'Enable' : 'Disable';
    toggle.addEventListener('click', () => {
      if (!u.disabled_at && !confirm(`Disable ${u.username}? They're signed out immediately.`)) return;
      adminPatch(u, { disabled: !u.disabled_at });
    });
    tdActions.appendChild(toggle);
  }
  tr.appendChild(tdActions);
  return tr;
}

async function adminPatch(u, patch, okMsg = 'Saved') {
  try {
    await api(`/api/admin/users/${u.id}`, { method: 'PATCH', body: JSON.stringify(patch) });
    toast(okMsg);
  } catch (err) {
    toast(`Save failed: ${err.message}`, true);
  }
  loadAdminUsers();
}

document.getElementById('admin-create').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const { user } = await api('/api/admin/users', {
      method: 'POST',
      body: JSON.stringify({
        username: f.username.value,
        password: f.password.value,
        can_spend: f.can_spend.checked,
      }),
    });
    toast(`Created ${user.username}`);
    f.reset();
    loadAdminUsers();
    loadAdminUsage();
  } catch (err) {
    toast(`Create failed: ${err.message}`, true);
  }
});

let usageDays = 30;

document.getElementById('usage-range').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-days]');
  if (!btn) return;
  usageDays = Number(btn.dataset.days);
  document.querySelectorAll('#usage-range button').forEach((b) => b.classList.toggle('active', b === btn));
  loadAdminUsage();
});

async function loadAdminUsage() {
  const host = document.getElementById('admin-usage');
  try {
    const data = await api(`/api/admin/usage?days=${usageDays}`);
    const table = document.createElement('table');
    table.innerHTML = `<thead><tr>
      <th>User</th><th class="num">Apollo credits</th><th class="num">Reveals</th>
      <th class="num">Searches</th><th class="num">Claude calls</th>
      <th class="num">Tokens in / out</th><th class="num">Est. Claude $</th><th>Last used</th>
    </tr></thead>`;
    const tbody = document.createElement('tbody');
    for (const r of data.totals) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><b>${esc(r.username)}</b></td>
        <td class="num">${num(r.apollo_credits)}</td>
        <td class="num">${num(r.reveals)}</td>
        <td class="num">${num(r.searches)}</td>
        <td class="num">${num(r.claude_calls)}</td>
        <td class="num">${num(r.input_tokens)} / ${num(r.output_tokens)}</td>
        <td class="num">$${Number(r.claude_cost_usd).toFixed(2)}</td>
        <td>${esc(dateStr(r.last_used))}</td>`;
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    host.replaceChildren(table);
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

// ---------- init ----------

loadMe().catch((err) => toast(`Couldn't load your account: ${err.message}`, true));
loadReplies();
loadStats();
loadTimeseries();
loadBreakdown();
loadIcp();
loadEnrichMeta();
loadContacts();
loadWorkSettings();
