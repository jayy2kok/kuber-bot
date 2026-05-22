/* ─────────────────────────────────────────────────────────────────────────
   KuberBot Watchlist — Frontend Logic
   Fetches /api/watchlist + /api/watchlist/summary, renders tables & cards
───────────────────────────────────────────────────────────────────────── */

'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let _data        = { accepted: [], not_accepted: [], last_refresh: null };
let _summary     = {};
let _activeTab   = 'accepted';
let _sortCol     = null;
let _sortDir     = 1;   // 1 = asc, -1 = desc
let _autoTimer   = null;

const AUTO_REFRESH_MS = 60_000;   // poll API every 60 s

// ── Column definitions ────────────────────────────────────────────────────
const COLS_ACCEPTED = [
  { key: 'date',          label: 'Date',       align: 'left',  fmt: fmtDate },
  { key: 'symbol',        label: 'Stock',      align: 'left',  fmt: fmtStock },
  { key: 'signal',        label: 'Signal',     align: 'left',  fmt: fmtSignal },
  { key: 'qty',           label: 'Qty',        fmt: fmtNum0 },
  { key: 'avg_price',     label: 'Avg ₹',      fmt: fmtPrice },
  { key: 'cmp',           label: 'CMP ₹',      fmt: fmtPrice },
  { key: 'target_1',      label: 'T1 ₹',       fmt: fmtPrice },
  { key: 'target_2',      label: 'T2 ₹',       fmt: fmtPriceOpt },
  { key: 'stop_loss',     label: 'SL ₹',       fmt: fmtPrice },
  { key: 'watchlist_status', label: 'Status',  fmt: fmtStatus },
  { key: 'invested',      label: 'Invested ₹', fmt: fmtMoney },
  { key: 'current_value', label: 'Current ₹',  fmt: fmtMoney },
  { key: 'pnl_pct',       label: 'P&L %',      fmt: fmtPct },
  { key: 'pnl_abs',       label: 'P&L ₹',      fmt: fmtPnlAbs },
];

const COLS_NOT_ACCEPTED = [
  { key: 'date',          label: 'Date',         align: 'left', fmt: fmtDate },
  { key: 'symbol',        label: 'Stock',        align: 'left', fmt: fmtStock },
  { key: 'signal',        label: 'Signal',       align: 'left', fmt: fmtSignal },
  { key: 'entry_price',   label: 'Entry ₹',      fmt: fmtPrice },
  { key: 'cmp',           label: 'CMP ₹',        fmt: fmtPrice },
  { key: 'target_1',      label: 'T1 ₹',         fmt: fmtPrice },
  { key: 'target_2',      label: 'T2 ₹',         fmt: fmtPriceOpt },
  { key: 'stop_loss',     label: 'SL ₹',         fmt: fmtPrice },
  { key: 'watchlist_status', label: 'Status',    fmt: fmtStatus },
  { key: 'invested',      label: 'If ₹1,000',    fmt: fmtMoneyNotional },
  { key: 'current_value', label: 'Would Be ₹',   fmt: fmtMoneyNotional },
  { key: 'pnl_pct',       label: 'P&L %',        fmt: fmtPct },
  { key: 'pnl_abs',       label: 'P&L ₹',        fmt: fmtPnlAbs },
];

// ── Formatters ────────────────────────────────────────────────────────────
function fmtDate(v)    { return `<span class="cell-date">${v}</span>`; }
function fmtNum0(v)    { return v != null ? Number(v).toLocaleString('en-IN') : '—'; }
function fmtPrice(v)   { return v != null ? `₹${Number(v).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}` : '—'; }
function fmtPriceOpt(v){ return v != null ? fmtPrice(v) : '<span style="color:var(--muted)">—</span>'; }
function fmtMoney(v)   { return v != null ? `₹${Number(v).toLocaleString('en-IN', {minimumFractionDigits:0,maximumFractionDigits:0})}` : '—'; }
function fmtMoneyNotional(v) {
  return v != null
    ? `<span class="cell-notional">₹${Number(v).toLocaleString('en-IN', {minimumFractionDigits:0,maximumFractionDigits:0})}</span>`
    : '—';
}

function fmtStock(_, row) {
  return `<div class="cell-symbol">${row.symbol}</div><div class="cell-name" title="${row.name}">${row.name}</div>`;
}

function fmtSignal(v) {
  const labels = {
    strong_buy:  '🟢🟢 STRONG BUY',
    buy:         '🟢 BUY',
    sell:        '🔴 SELL',
    strong_sell: '🔴🔴 STRONG SELL',
    hold:        '🔵 HOLD',
  };
  return `<span class="cell-signal signal-${v}">${labels[v] || v.toUpperCase()}</span>`;
}

function fmtStatus(v) {
  const cfg = {
    active:       { cls: 'badge-active',       icon: '🔵', label: 'Active' },
    target_1_hit: { cls: 'badge-target_1_hit', icon: '✅', label: 'Target 1 Hit' },
    target_2_hit: { cls: 'badge-target_2_hit', icon: '✅✅', label: 'Target 2 Hit' },
    sl_hit:       { cls: 'badge-sl_hit',        icon: '🔴', label: 'SL Hit' },
  };
  const c = cfg[v] || cfg['active'];
  return `<span class="badge ${c.cls}">${c.icon} ${c.label}</span>`;
}

function fmtPct(v) {
  if (v == null) return '—';
  const cls = v > 0 ? 'cell-pnl-pos' : v < 0 ? 'cell-pnl-neg' : 'cell-pnl-zero';
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${Number(v).toFixed(2)}%</span>`;
}

function fmtPnlAbs(v) {
  if (v == null) return '—';
  const cls = v > 0 ? 'cell-pnl-pos' : v < 0 ? 'cell-pnl-neg' : 'cell-pnl-zero';
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}₹${Math.abs(Number(v)).toLocaleString('en-IN', {minimumFractionDigits:0,maximumFractionDigits:0})}</span>`;
}

function fmtLastRefresh(iso) {
  if (!iso) return 'Never';
  const d = new Date(iso + 'Z');   // UTC
  const now = new Date();
  const diffMs = now - d;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1)  return 'Just now';
  if (diffMin < 60) return `${diffMin} min ago`;
  const diffH = Math.floor(diffMin / 60);
  return `${diffH}h ${diffMin % 60}m ago`;
}

function fmtMoneySummary(v) {
  if (v == null) return '—';
  const sign = v >= 0 ? '+' : '';
  return `${sign}₹${Math.abs(Number(v)).toLocaleString('en-IN', {maximumFractionDigits: 0})}`;
}

// ── Render summary cards ──────────────────────────────────────────────────
function renderSummary(s) {
  _q('#cTotal').textContent = s.total_recommendations ?? '—';

  const acc = s.accepted || {};
  _q('#cAccepted').textContent = acc.count ?? '—';
  const accPnl = acc.total_pnl_abs;
  const accPct = acc.avg_pnl_pct;
  _q('#cAcceptedPnl').textContent = accPnl != null
    ? `${fmtSign(accPnl)}₹${Math.abs(accPnl).toLocaleString('en-IN', {maximumFractionDigits:0})} (${fmtSign(accPct)}${accPct?.toFixed(1)}%)`
    : '—';

  const na = s.not_accepted || {};
  _q('#cNotAccepted').textContent = na.count ?? '—';
  const naPnl = na.total_pnl_abs;
  const naPct = na.avg_pnl_pct;
  _q('#cNotAcceptedPnl').textContent = naPnl != null
    ? `${fmtSign(naPnl)}₹${Math.abs(naPnl).toLocaleString('en-IN', {maximumFractionDigits:0})} (${fmtSign(naPct)}${naPct?.toFixed(1)}%)`
    : '—';

  // Overall P&L card
  if (accPnl != null) {
    const cls = accPnl >= 0 ? 'cell-pnl-pos' : 'cell-pnl-neg';
    _q('#cOverallPnl').innerHTML = `<span class="${cls}">${fmtSign(accPnl)}₹${Math.abs(accPnl).toLocaleString('en-IN', {maximumFractionDigits:0})}</span>`;
    _q('#cAvgPct').innerHTML = `<span class="${accPct >= 0 ? 'cell-pnl-pos' : 'cell-pnl-neg'}">${fmtSign(accPct)}${accPct?.toFixed(2)}% avg</span>`;
  }

  // Missed gains card
  const missed = s.missed_gains ?? 0;
  _q('#cMissed').innerHTML = missed > 0
    ? `<span class="cell-pnl-neg">₹${missed.toLocaleString('en-IN', {maximumFractionDigits:0})}</span>`
    : `<span class="cell-pnl-zero">₹0</span>`;

  // Tab badges
  _q('#badgeAccepted').textContent   = acc.count ?? 0;
  _q('#badgeNotAccepted').textContent = na.count ?? 0;

  // Missed banner
  const banner = _q('#missedBanner');
  if (_activeTab === 'not_accepted' && missed > 0) {
    banner.classList.remove('hidden');
    _q('#missedAmt').textContent = `₹${missed.toLocaleString('en-IN', {maximumFractionDigits:0})}`;
  } else {
    banner.classList.add('hidden');
  }
}

function fmtSign(v) { return v >= 0 ? '+' : ''; }

// ── Render table ──────────────────────────────────────────────────────────
function renderTable() {
  const cols = _activeTab === 'accepted' ? COLS_ACCEPTED : COLS_NOT_ACCEPTED;
  const rows = [...(_data[_activeTab] || [])];

  // Sort
  if (_sortCol !== null) {
    const col = cols[_sortCol];
    rows.sort((a, b) => {
      const av = a[col.key];
      const bv = b[col.key];
      if (av == null && bv == null) return 0;
      if (av == null) return _sortDir;
      if (bv == null) return -_sortDir;
      return av < bv ? -_sortDir : av > bv ? _sortDir : 0;
    });
  }

  // Header
  const head = _q('#tableHead');
  head.innerHTML = `<tr>${cols.map((c, i) => {
    const sorted = _sortCol === i;
    const arrow  = sorted ? (_sortDir === 1 ? '▲' : '▼') : '▲';
    const cls    = sorted ? 'sorted' : '';
    return `<th class="${cls}" data-col="${i}">${c.label} <span class="sort-arrow">${arrow}</span></th>`;
  }).join('')}</tr>`;

  // Bind sort clicks
  head.querySelectorAll('th').forEach(th => {
    th.addEventListener('click', () => {
      const col = Number(th.dataset.col);
      if (_sortCol === col) _sortDir *= -1;
      else { _sortCol = col; _sortDir = -1; }
      renderTable();
    });
  });

  // Body
  const body = _q('#tableBody');
  const empty = _q('#emptyState');

  if (!rows.length) {
    body.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  body.innerHTML = rows.map(row => {
    const rowCls = row.pnl_pct > 0 ? 'profit' : row.pnl_pct < 0 ? 'loss' : '';
    const cells = cols.map(c => {
      const raw = row[c.key];
      const html = c.fmt ? c.fmt(raw, row) : (raw ?? '—');
      return `<td>${html}</td>`;
    }).join('');
    return `<tr class="${rowCls}" data-id="${row.id}">${cells}</tr>`;
  }).join('');
}

// ── Fetch data ────────────────────────────────────────────────────────────
async function fetchAll() {
  try {
    const [dataRes, summaryRes] = await Promise.all([
      fetch('/api/watchlist'),
      fetch('/api/watchlist/summary'),
    ]);
    if (!dataRes.ok || !summaryRes.ok) throw new Error('API error');
    _data    = await dataRes.json();
    _summary = await summaryRes.json();

    renderSummary(_summary);
    renderTable();
    updateRefreshLabel(_data.last_refresh);
  } catch (e) {
    console.error('Watchlist fetch failed:', e);
    _q('#tableBody').innerHTML =
      `<tr><td colspan="14" class="loading" style="color:var(--red)">Failed to load data. Retrying…</td></tr>`;
  }
}

function updateRefreshLabel(iso) {
  const lbl = _q('#refreshLabel');
  lbl.textContent = iso ? `Last CMP refresh: ${fmtLastRefresh(iso)}` : 'CMP not yet refreshed';
}

// ── Manual refresh button ─────────────────────────────────────────────────
async function triggerRefresh() {
  const btn = _q('#btnRefresh');
  btn.classList.add('loading');
  btn.disabled = true;
  try {
    await fetch('/api/watchlist/refresh', { method: 'POST' });
    await fetchAll();
  } catch (e) {
    console.error('Refresh failed:', e);
  } finally {
    btn.classList.remove('loading');
    btn.disabled = false;
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────
function switchTab(tab) {
  _activeTab = tab;
  _sortCol   = null;
  _sortDir   = -1;

  document.querySelectorAll('.tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });

  renderSummary(_summary);
  renderTable();
}

// ── Auto-refresh ticker ───────────────────────────────────────────────────
function startAutoRefresh() {
  if (_autoTimer) clearInterval(_autoTimer);
  _autoTimer = setInterval(fetchAll, AUTO_REFRESH_MS);
  // Update the "X min ago" label every 30s without re-fetching
  setInterval(() => updateRefreshLabel(_data.last_refresh), 30_000);
}

// ── Utility ───────────────────────────────────────────────────────────────
function _q(sel) { return document.querySelector(sel); }

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Tab buttons
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Refresh button
  _q('#btnRefresh').addEventListener('click', triggerRefresh);

  // Initial load
  fetchAll();
  startAutoRefresh();
});
