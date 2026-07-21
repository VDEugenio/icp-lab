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

// ---------- overview ----------

async function loadStats() {
  try {
    const data = await api('/api/stats');
    renderKpis(data.overall);
    renderFunnel(data.overall);
    renderChannelTable(data.by_channel);
  } catch (err) {
    document.getElementById('kpis').innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderKpis(o) {
  document.getElementById('kpis').innerHTML = `
    <div class="tile"><div class="label">Contacts</div><div class="value">${num(o.contacted)}</div><div class="hint">all rows</div></div>
    <div class="tile"><div class="label">Clicked link</div><div class="value">${pct(o.click_rate)}</div><div class="hint">${num(o.clicked)} contacts</div></div>
    <div class="tile"><div class="label">Responded</div><div class="value">${pct(o.response_rate)}</div><div class="hint">${num(o.responded)} contacts</div></div>
    <div class="tile"><div class="label">Clicked → responded</div><div class="value">${o.clicked ? pct(o.responded / o.clicked) : '—'}</div><div class="hint">of those who clicked</div></div>`;
}

function renderFunnel(o) {
  // per-bar label ink: dark text on the light funnel step, white on the darker two
  const stages = [
    ['Contacted', o.contacted, 'var(--funnel-1)', '#0b0b0b'],
    ['Clicked', o.clicked, 'var(--funnel-2)', '#ffffff'],
    ['Responded', o.responded, 'var(--funnel-3)', '#ffffff'],
  ];
  const max = Math.max(o.contacted, 1);
  document.getElementById('funnel').innerHTML = stages.map(([name, val, color, inkOnBar], i) => {
    const w = Math.max((val / max) * 100, 0.5);
    const prev = i > 0 ? stages[i - 1][1] : null;
    const conv = prev ? ` <span class="conv">${pct(val / prev)} of ${esc(stages[i - 1][0].toLowerCase())}</span>` : '';
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
  document.getElementById('channel-table').innerHTML = `<table><thead><tr>
      <th>Channel</th><th class="num">Contacted</th><th class="num">Clicked</th>
      <th class="num">Click rate</th><th class="num">Responded</th><th class="num">Response rate</th>
    </tr></thead><tbody>` +
    rows.map((r) => `<tr>
      <td>${esc(label[r.channel] || r.channel)}</td>
      <td class="num">${num(r.contacted)}</td>
      <td class="num">${num(r.clicked)}</td>
      <td class="num">${pct(r.click_rate)}</td>
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
    const data = await api(`/api/timeseries?granularity=${currentGranularity}`);
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
      ['Clicked', `${num(p.clicked)} (${pct(p.click_rate)})`],
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
    const data = await api(`/api/breakdown?dim=${currentDim}`);
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
  if (!groups.length) { host.innerHTML = '<div class="loading">No data.</div>'; return; }
  // one shared scale for both bars, so lengths are comparable across the table
  const scale = Math.max(0.05, ...groups.flatMap((g) => [g.response_rate || 0, g.click_rate || 0]));
  host.innerHTML = `<table><thead><tr>
      <th>Group</th><th class="num">n</th>
      <th class="bar-cell">Response rate</th><th class="bar-cell">Click rate</th>
      <th class="num">Responded</th><th class="num">Clicked</th>
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
        <td class="bar-cell">${bar(g.click_rate, 'click')}</td>
        <td class="num">${num(g.responded)}</td>
        <td class="num">${num(g.clicked)}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

// ---------- contacts ----------

const OUTCOMES = ['call', 'referral', 'ghost', 'rejected', 'other'];
let allContacts = [];

document.getElementById('contact-search').addEventListener('input', () => renderContacts());

async function loadContacts() {
  const host = document.getElementById('contacts-table');
  try {
    const data = await api('/api/contacts');
    allContacts = data.contacts;
    renderContacts();
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
    <th>Contacted</th><th class="num">Visits</th>
    <th>Responded</th><th>Responded at</th><th>Outcome</th>
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
    <td><span class="chip">${esc(chLabel)}</span></td>
    <td title="${esc(c.contacted_at || '')}">${dateStr(c.contacted_at)}</td>
    <td class="num">${c.visit_count > 0 ? `<b>${num(c.visit_count)}</b>` : '0'}</td>`;

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

  return tr;
}

async function saveContact(tr, c, patch) {
  tr.classList.add('saving');
  try {
    const data = await api(`/api/contacts/${encodeURIComponent(c.uid)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    Object.assign(c, data.contact); // server truth: responded, responded_at, outcome
    tr.replaceWith(contactRow(c));
    toast('Saved');
  } catch (err) {
    tr.replaceWith(contactRow(c)); // revert to last known state
    toast(`Save failed: ${err.message}`, true);
  } finally {
    tr.classList.remove('saving');
  }
}

// ---------- init ----------

loadStats();
loadTimeseries();
loadBreakdown();
loadContacts();
