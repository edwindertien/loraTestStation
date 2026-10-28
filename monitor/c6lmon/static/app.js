'use strict';
/*
 * c6l monitor UI - vanilla JS, no framework, no build step.
 *
 * Security note: packet text arrives over the air from ANYONE's transmitter, so it is
 * untrusted. Nothing received is ever put through innerHTML - every node is built with
 * el() below, which only ever sets textContent / attributes.
 */
(() => {

// ============================================================ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid instanceof Node ? kid : String(kid));      // strings become text nodes - never markup
  }
  return n;
}
const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

const pad = n => String(n).padStart(2, '0');
function fmtTs(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
const fmtClock = ts => fmtTs(ts).slice(11);
function fmtVal(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '\u2013';
  return String(+Number(v).toFixed(digits));
}
function fmtAgo(sec) {
  sec = Math.max(0, sec);
  if (sec < 90) return `${Math.round(sec)} s ago`;
  if (sec < 5400) return `${Math.round(sec / 60)} min ago`;
  if (sec < 172800) return `${(sec / 3600).toFixed(1)} h ago`;
  return `${Math.round(sec / 86400)} d ago`;
}
function localToEpoch(v) {
  if (!v) return null;
  const t = new Date(v).getTime();
  return Number.isNaN(t) ? null : t / 1000;
}
const epochToLocal = ts => { const d = new Date(ts * 1000); return `${fmtTs(ts).slice(0, 10)}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };

async function api(path, opts) {
  const r = await fetch(path, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((j && j.error) || `${r.status} ${r.statusText}`);
  return j;
}
const send = (method, path, body) =>
  api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
const post = (path, body) => send('POST', path, body);

const S = {                // shared state
  device: null, offset: 0, netErr: null, formDirty: false,
  metrics: [], fields: [], nodes: [],
};

// ============================================================ tabs
const loaders = { settings: () => loadSettingsTab(), log: () => loadLogTab(), graphs: () => loadGraphsTab() };
let activeTab = 'live';
function showTab(name) {
  if (!$('#tab-' + name)) name = 'live';
  activeTab = name;
  $$('.tabs button').forEach(b => {
    const on = b.dataset.tab === name;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  $$('.pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (loaders[name]) loaders[name]();
  try { history.replaceState(null, '', '#' + name); } catch (e) { /* ignore */ }
}

// ============================================================ connection bar + banner
function renderBanner() {
  const b = $('#banner');
  const dev = S.device;
  const msgs = [];
  if (S.netErr) msgs.push('Lost contact with the monitor server (' + S.netErr + ') - retrying.');
  else if (dev && dev.error && !dev.connected) msgs.push('Device: ' + dev.error);
  else if (dev && dev.error && dev.connected) msgs.push('Device: ' + dev.error);
  b.hidden = msgs.length === 0;
  b.textContent = msgs.join(' ');
}

function renderConn(dev) {
  const pill = $('#conn-pill');
  if (dev.connected) { pill.className = 'pill pill-on'; pill.textContent = 'connected \u00b7 ' + dev.port; }
  else if (dev.want_port) { pill.className = 'pill pill-wait'; pill.textContent = 'connecting\u2026'; }
  else { pill.className = 'pill pill-off'; pill.textContent = 'disconnected'; }
  const inp = $('#port-input');
  if (!inp.value && document.activeElement !== inp) inp.value = dev.want_port || dev.port || '';
  $('#btn-disconnect').disabled = !dev.connected && !dev.want_port;
}

async function loadPorts() {
  try {
    const j = await api('/api/ports');
    const dl = clear($('#port-options'));
    for (const p of j.ports) {
      dl.append(el('option', { value: p.device }, (p.likely ? '\u2605 ' : '') + (p.description || '')));
    }
    const inp = $('#port-input');
    if (!inp.value) {
      const likely = j.ports.find(p => p.likely);
      if (likely) inp.value = likely.device;
    }
  } catch (e) { /* the banner will say so */ }
}

// ============================================================ live
const L = { lastId: -1, consoleSeq: 0, rows: [], consoleLines: 0 };

function fieldsSummary(f, max = 6) {
  const e = Object.entries(f || {});
  if (!e.length) return '';
  return e.slice(0, max).map(([k, v]) => `${k} ${fmtVal(v)}`).join('   ') + (e.length > max ? '   \u2026' : '');
}
function dataCell(p) {
  if (!p.ok) return el('span', { class: 'bad' }, `not decoded (error ${p.err}) - header or CRC check failed`);
  const f = fieldsSummary(p.fields);
  if (f) return el('span', {}, f);
  if (p.text !== null && p.text !== undefined) return el('span', {}, p.text.length > 120 ? p.text.slice(0, 120) + '\u2026' : p.text);
  return el('span', { class: 'mono muted' }, (p.hex || '').slice(0, 48) + ((p.hex || '').length > 48 ? '\u2026' : ''));
}

function liveRow(p) {
  return el('tr', {},
    el('td', { class: 'nowrap' }, fmtClock(p.ts)),
    el('td', { class: 'nowrap' }, p.node || (p.ok ? '(untagged)' : '\u2013')),
    el('td', { class: 'num' }, fmtVal(p.rssi, 1)),
    el('td', { class: 'num' }, fmtVal(p.snr, 1)),
    el('td', { class: 'num' }, p.ok ? String(p.len) : '\u2013'),
    el('td', { class: 'data' }, dataCell(p)));
}

function renderLiveTable() {
  const tb = clear($('#live-table tbody'));
  for (const p of L.rows) tb.append(liveRow(p));
  $('#live-empty').hidden = L.rows.length > 0;
}

function cfgText(c) {
  if (!c) return '\u2013';
  return `${Number(c.freq).toFixed(3)} MHz \u00b7 SF${c.sf} \u00b7 BW${c.bw} \u00b7 CR 4/${c.cr} \u00b7 sync ${c.sync}`;
}

function renderLiveCards(dev, db) {
  $('#live-cfg').textContent = dev.config ? cfgText(dev.config) : (dev.connected ? 'reading settings\u2026' : '\u2013');
  $('#live-cfg-state').textContent = dev.config ? 'settings: ' + dev.config.state : '';
  const now = dev.now;
  let alive = 'offline';
  if (dev.connected) {
    alive = dev.last_heartbeat_ts ? 'alive \u00b7 heartbeat ' + fmtAgo(now - dev.last_heartbeat_ts)
                                  : (dev.last_line_ts ? 'connected' : 'connected, silent');
  }
  $('#live-alive').textContent = alive;
  $('#live-boots').textContent = dev.json_mode ? `JSON mode on \u00b7 board restarts seen: ${dev.boots}` : `board restarts seen: ${dev.boots}`;
  $('#live-session').textContent = `${dev.session.rx_ok} / ${dev.session.rx_fail}`;
  $('#live-db').textContent = String(db.packets);
}

function renderLast() {
  const p = L.rows.find(r => r.ok);
  const card = $('#last-card');
  card.hidden = !p;
  if (!p) return;
  const head = clear($('#last-head'));
  head.append(el('span', {}, p.node || '(untagged)'), el('span', {}, fmtTs(p.ts)),
              el('span', {}, `RSSI ${fmtVal(p.rssi, 1)} dBm`), el('span', {}, `SNR ${fmtVal(p.snr, 1)} dB`));
  const tiles = clear($('#last-fields'));
  for (const [k, v] of Object.entries(p.fields || {})) {
    tiles.append(el('div', { class: 'tile' }, el('div', { class: 'k' }, k), el('div', { class: 'v' }, fmtVal(v))));
  }
  $('#last-text').textContent = p.text === null || p.text === undefined ? 'binary payload: ' + (p.hex || '') : p.text;
  tickAges();
}

function tickAges() {
  const p = L.rows.find(r => r.ok);
  if (p) $('#last-age').textContent = '\u00b7 ' + fmtAgo(Date.now() / 1000 + S.offset - p.ts);
}

function addConsole(lines) {
  const pre = $('#console');
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
  for (const c of lines) {
    pre.append(document.createTextNode(`${fmtClock(c.ts)}  ${c.text}\n`));
    L.consoleLines++;
    L.consoleSeq = Math.max(L.consoleSeq, c.seq);
  }
  while (L.consoleLines > 800 && pre.firstChild) { pre.removeChild(pre.firstChild); L.consoleLines--; }
  if (stick) pre.scrollTop = pre.scrollHeight;
}

function applyDevice(dev, db) {
  S.device = dev;
  S.offset = dev.now - Date.now() / 1000;
  renderConn(dev);
  renderLiveCards(dev, db);
  renderBanner();
  onConfigChanged(dev.config);
}

let liveTimer = null;
async function pollLive() {
  try {
    const q = `/api/live?after=${L.lastId}&console_after=${L.consoleSeq}` + (L.lastId < 0 ? '&limit=50' : '');
    const j = await api(q);
    S.netErr = null;
    if (L.lastId >= 0 && j.db.max_id < L.lastId) {          // the database was emptied or replaced
      L.lastId = -1; L.rows = []; L.consoleSeq = 0; clear($('#console')); L.consoleLines = 0;
      renderLiveTable();
    } else {
      if (j.packets.length) {
        for (const p of j.packets) L.rows.unshift(p);
        L.rows.sort((a, b) => b.id - a.id);
        L.rows = L.rows.slice(0, 100);
        renderLiveTable();
        renderLast();
      }
      if (L.lastId < 0) {
        L.lastId = j.packets.length ? Math.max(...j.packets.map(p => p.id)) : 0;
        if (!j.packets.length) renderLiveTable();
      } else if (j.packets.length) {
        L.lastId = Math.max(L.lastId, ...j.packets.map(p => p.id));
      }
    }
    if (j.console.length) addConsole(j.console);
    applyDevice(j.device, j.db);
  } catch (e) {
    S.netErr = e.message;
    renderBanner();
  } finally {
    liveTimer = setTimeout(pollLive, 1000);
  }
}

// ============================================================ settings
const BANDWIDTHS = ['7.8', '10.4', '15.6', '20.8', '31.25', '41.7', '62.5', '125', '250', '500'];
const EU868 = [868.1, 868.3, 868.5, 867.1, 867.3, 867.5, 867.7, 867.9];
const BUILTIN_PRESETS = [
  { name: 'SEN66 test link', data: { freq: 868.1, bw: 125, sf: 7, cr: 7, sync: '12', pre: 8 },
    note: '868.1 MHz, SF7, BW125, CR 4/7, sync 0x12 - what the SEN66/PicoW node uses' },
  { name: 'LoRaWAN EU868 ch0 SF7', data: { freq: 868.1, bw: 125, sf: 7, cr: 5, sync: '34', pre: 8 },
    note: 'standard LoRaWAN PHY: CR 4/5, sync 0x34' },
];

function buildStaticOptions() {
  const bw = $('#f-bw');
  for (const b of BANDWIDTHS) bw.append(el('option', { value: b }, b));
  const sf = $('#f-sf');
  for (let n = 5; n <= 12; n++) sf.append(el('option', { value: n }, 'SF' + n));
  const cr = $('#f-cr');
  for (let n = 5; n <= 8; n++) cr.append(el('option', { value: n }, `${n}  (4/${n})`));
  const ch = $('#f-ch');
  EU868.forEach((f, i) => ch.append(el('option', { value: f }, `ch ${i} \u00b7 ${f}`)));
}

function fillForm(cfg) {
  if (!cfg) return;
  $('#f-freq').value = Number(cfg.freq).toFixed(3);
  $('#f-bw').value = String(cfg.bw);
  $('#f-sf').value = String(cfg.sf);
  $('#f-cr').value = String(cfg.cr);
  $('#f-sync').value = String(cfg.sync).replace(/^0x/i, '');
  $('#f-pre').value = cfg.pre;
  syncChannelSelect();
}
function syncChannelSelect() {
  const f = parseFloat($('#f-freq').value);
  const i = EU868.findIndex(c => Math.abs(c - f) < 0.0005);
  $('#f-ch').value = i >= 0 ? String(EU868[i]) : '';
}
function readForm() {
  return {
    freq: parseFloat($('#f-freq').value), bw: parseFloat($('#f-bw').value),
    sf: parseInt($('#f-sf').value, 10), cr: parseInt($('#f-cr').value, 10),
    sync: $('#f-sync').value.trim(), pre: parseInt($('#f-pre').value, 10),
  };
}

let lastCfgKey = '';
function onConfigChanged(cfg) {
  const key = JSON.stringify(cfg);
  if (key === lastCfgKey) return;
  lastCfgKey = key;
  $('#cfg-state').textContent = cfg ? cfg.state : '\u2013';
  if (cfg && !S.formDirty) fillForm(cfg);
}

function showResults(box, lines) {
  const b = clear(box);
  for (const [ok, text] of lines) b.append(el('div', { class: ok ? 'okline' : 'errline' }, (ok ? '\u2713 ' : '\u2717 ') + text));
}

async function applyForm(ev) {
  ev.preventDefault();
  const btn = $('#btn-apply');
  btn.disabled = true;
  try {
    const j = await post('/api/config', readForm());
    const lines = j.results.map(r => [r.ok, `${r.key}: ${r.reply}`]);
    if (!lines.length) lines.push([true, 'No changes - the device already has these settings.']);
    showResults($('#cfg-result'), lines);
    if (j.ok) { S.formDirty = false; fillForm(j.config); }
    if (j.config) { lastCfgKey = ''; onConfigChanged(j.config); }
  } catch (e) {
    showResults($('#cfg-result'), [[false, e.message]]);
  } finally { btn.disabled = false; }
}

async function memAction(path, confirmText) {
  if (confirmText && !window.confirm(confirmText)) return;
  try {
    const j = await post(path);
    showResults($('#mem-result'), [[j.ok, j.reply]]);
    if (j.config) { S.formDirty = false; lastCfgKey = ''; onConfigChanged(j.config); fillForm(j.config); }
  } catch (e) { showResults($('#mem-result'), [[false, e.message]]); }
}

let savedPresets = [];
async function loadPresets() {
  try { savedPresets = (await api('/api/saved/preset')).items; } catch (e) { savedPresets = []; }
  const sel = clear($('#preset-select'));
  BUILTIN_PRESETS.forEach((p, i) => sel.append(el('option', { value: 'b:' + i, title: p.note }, 'built-in: ' + p.name)));
  savedPresets.forEach(p => sel.append(el('option', { value: 'u:' + p.name }, p.name)));
}
function presetData(value) {
  if (value.startsWith('b:')) return BUILTIN_PRESETS[+value.slice(2)].data;
  const it = savedPresets.find(p => 'u:' + p.name === value);
  return it ? it.data : null;
}
function loadSettingsTab() {
  loadPresets();
  if (S.device && S.device.config && !S.formDirty) fillForm(S.device.config);
}

// ============================================================ log
const LG = { offset: 0, total: 0 };
function logParams() {
  const p = new URLSearchParams();
  const from = localToEpoch($('#l-from').value), to = localToEpoch($('#l-to').value);
  if (from !== null) p.set('from', from);
  if (to !== null) p.set('to', to);
  const node = $('#l-node').value;
  if (node === '__untagged__') p.set('node', '');
  else if (node) p.set('node', node);
  if ($('#l-ok').value) p.set('ok', $('#l-ok').value);
  if ($('#l-q').value.trim()) p.set('q', $('#l-q').value.trim());
  return p;
}
const logLimit = () => parseInt($('#l-limit').value, 10) || 50;

async function refreshMetrics() {
  try {
    const m = await api('/api/metrics');
    S.metrics = m.metrics; S.fields = m.fields; S.nodes = m.nodes;
  } catch (e) { /* keep the old lists */ }
}

function fillNodeSelect(sel, { any, untagged }) {
  const keep = sel.value;
  clear(sel);
  sel.append(el('option', { value: '' }, any));
  if (S.nodes.includes('')) sel.append(el('option', { value: '__untagged__' }, untagged));
  for (const n of S.nodes) if (n) sel.append(el('option', { value: n }, n));
  if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
}

function logRow(p) {
  const rx = p.sf ? `SF${p.sf} BW${fmtVal(p.bw)} ${fmtVal(p.freq, 3)}` : '\u2013';
  const main = el('tr', { class: 'clickable', title: 'click for details' },
    el('td', { class: 'nowrap' }, fmtTs(p.ts)),
    el('td', { class: 'nowrap' }, p.node || '\u2013'),
    el('td', { class: 'nowrap ' + (p.ok ? 'good' : 'bad') }, p.ok ? 'decoded' : `failed (${p.err})`),
    el('td', { class: 'num' }, fmtVal(p.rssi, 1)),
    el('td', { class: 'num' }, fmtVal(p.snr, 1)),
    el('td', { class: 'num' }, p.ok ? String(p.len) : '\u2013'),
    el('td', { class: 'nowrap small muted' }, rx),
    el('td', { class: 'data' }, dataCell(p)));
  const detail = el('tr', { class: 'detail', hidden: true },
    el('td', { colspan: 8 },
      el('div', { class: 'small' }, `packet #${p.id} \u00b7 device uptime ${p.up_ms === null || p.up_ms === undefined ? '\u2013' : (p.up_ms / 1000).toFixed(1) + ' s'}` +
        ` \u00b7 coding rate ${p.cr ? '4/' + p.cr : '\u2013'} \u00b7 sync ${p.sync === null || p.sync === undefined ? '\u2013' : '0x' + p.sync.toString(16).toUpperCase().padStart(2, '0')}`),
      p.text !== null && p.text !== undefined ? el('div', { class: 'mono small' }, 'text: ' + p.text) : null,
      p.hex ? el('div', { class: 'mono small' }, 'hex: ' + p.hex) : null,
      Object.keys(p.fields || {}).length ? el('div', { class: 'mono small' }, 'fields: ' + JSON.stringify(p.fields)) : null));
  main.addEventListener('click', () => { detail.hidden = !detail.hidden; });
  return [main, detail];
}

async function loadLog() {
  const limit = logLimit();
  const p = logParams();
  const q = new URLSearchParams(p);
  q.set('limit', limit); q.set('offset', LG.offset);
  try {
    const j = await api('/api/packets?' + q.toString());
    LG.total = j.total;
    const tb = clear($('#log-table tbody'));
    for (const pk of j.packets) tb.append(...logRow(pk));
    const a = j.total ? LG.offset + 1 : 0, b = LG.offset + j.packets.length;
    $('#log-count').textContent = `${a}\u2013${b} of ${j.total} packets (newest first)`;
    $('#btn-prev').disabled = LG.offset <= 0;
    $('#btn-next').disabled = LG.offset + limit >= j.total;
    $('#exp-csv').href = '/api/packets/export.csv?' + p.toString();
    $('#exp-json').href = '/api/packets/export.json?' + p.toString();
  } catch (e) { $('#log-count').textContent = 'Could not load: ' + e.message; }
}

async function loadLogTab() {
  await refreshMetrics();
  fillNodeSelect($('#l-node'), { any: 'any', untagged: '(untagged / undecoded)' });
  loadLog();
}

async function deleteData(query, label) {
  if (!window.confirm(label)) return;
  try {
    const j = await api('/api/packets?' + query, { method: 'DELETE' });
    $('#del-result').textContent = `Deleted ${j.deleted} packets.`;
    LG.offset = 0; L.lastId = -1; L.rows = []; renderLiveTable();
    loadLog();
  } catch (e) { $('#del-result').textContent = e.message; }
}

// ============================================================ reports
let reportChart = null;
function rangeValues(sel, fromEl, toEl) {
  const v = $(sel).value;
  if (v === 'all') return {};
  if (v === 'custom') return { from: localToEpoch($(fromEl).value), to: localToEpoch($(toEl).value) };
  const now = Date.now() / 1000 + S.offset;
  return { from: now - Number(v), to: null };
}
function qs(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== null && v !== undefined && v !== '') p.set(k, v);
  return p.toString();
}
const stat = (k, v, s) => el('div', { class: 'stat' }, el('div', { class: 'k' }, k), el('div', { class: 'v' }, v), s ? el('div', { class: 's' }, s) : null);
const range3 = (r, d = 1) => r ? `${fmtVal(r.min, d)} / ${fmtVal(r.avg, d)} / ${fmtVal(r.max, d)}` : '\u2013';

async function loadReport() {
  const q = qs(rangeValues('#r-range', '#r-from', '#r-to'));
  $('#exp-report').href = '/api/report.csv?' + q;
  const body = clear($('#report-body'));
  let rep;
  try { rep = await api('/api/report?' + q); } catch (e) { body.textContent = 'Could not load: ' + e.message; return; }
  const o = rep.overall;
  if (!o.total) { body.textContent = 'No packets in this period.'; return; }
  body.append(el('div', { class: 'stats' },
    stat('Received', String(o.total), `${fmtTs(o.first)} \u2192 ${fmtTs(o.last)}`),
    stat('Decoded', String(o.ok)),
    stat('Failed', String(o.fail), 'heard, but header/CRC check failed'),
    stat('Success', o.success_pct === null ? '\u2013' : o.success_pct + ' %'),
    stat('RSSI dBm', fmtVal(o.rssi && o.rssi.avg, 1), 'min / avg / max ' + range3(o.rssi)),
    stat('SNR dB', fmtVal(o.snr && o.snr.avg, 1), 'min / avg / max ' + range3(o.snr))));

  body.append(el('h2', {}, 'Per node (decoded packets)'));
  const rows = rep.nodes.map(n => el('tr', {},
    el('td', { class: 'nowrap' }, n.label), el('td', { class: 'num' }, String(n.count)),
    el('td', { class: 'num' }, n.interval_s === null ? '\u2013' : n.interval_s + ' s'),
    el('td', { class: 'num' }, n.missed === null ? '\u2013' : String(n.missed)),
    el('td', { class: 'num' }, n.completeness_pct === null ? '\u2013' : n.completeness_pct + ' %'),
    el('td', { class: 'num' }, range3(n.rssi)), el('td', { class: 'num' }, range3(n.snr)),
    el('td', { class: 'nowrap small' }, fmtTs(n.first)), el('td', { class: 'nowrap small' }, fmtTs(n.last))));
  body.append(el('div', { class: 'tablewrap' }, el('table', { class: 'grid' },
    el('thead', {}, el('tr', {}, ...['Node', 'Packets', 'Typical interval', 'Missed', 'Completeness', 'RSSI min/avg/max', 'SNR min/avg/max', 'First', 'Last'].map(h => el('th', {}, h)))),
    el('tbody', {}, ...rows))));
  body.append(el('p', { class: 'muted small' },
    'Typical interval is the median gap between decoded packets; "missed" counts gaps of more than 1.5\u00d7 that. ' +
    'A packet that was heard but failed its CRC shows up here as missed, because only decoded packets are counted per node.'));
  if (rep.failed.count) {
    body.append(el('p', {}, `${rep.failed.count} failed packets \u2013 RSSI min/avg/max ${range3(rep.failed.rssi)}, SNR ${range3(rep.failed.snr)}`));
  }

  body.append(el('h2', {}, 'Activity'));
  const box = el('div', { class: 'chartbox short' }, el('canvas', { id: 'report-canvas' }));
  body.append(box);
  const tl = rep.timeline;
  const fmtBucket = t => tl.bucket_s >= 86400 ? fmtTs(t).slice(5, 10) : fmtTs(t).slice(5, 16);
  if (reportChart) { reportChart.destroy(); reportChart = null; }
  reportChart = new Chart($('#report-canvas'), {
    type: 'bar',
    data: { labels: tl.points.map(p => fmtBucket(p.t)),
            datasets: [{ label: 'decoded', data: tl.points.map(p => p.ok), backgroundColor: '#1a9b6c' },
                       { label: 'failed', data: tl.points.map(p => p.fail), backgroundColor: '#c0392b' }] },
    options: { maintainAspectRatio: false, animation: false,
               scales: { x: { stacked: true, ticks: { maxRotation: 0, autoSkip: true } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } },
               plugins: { title: { display: true, text: `packets per ${tl.bucket_s >= 86400 ? 'day' : tl.bucket_s >= 3600 ? (tl.bucket_s / 3600) + ' h' : (tl.bucket_s / 60) + ' min'}` } } },
  });
}

// ============================================================ graphs
const PALETTE = ['#2557d6', '#e0702b', '#1a9b6c', '#b5379b', '#d4a017', '#0e9ac4', '#c0392b', '#6b7280'];
const COUNT_METRICS = ['packets', 'packets_ok', 'packets_fail'];
const METRIC_LABELS = { rssi: 'RSSI (dBm)', snr: 'SNR (dB)', packets: 'packets / bucket', packets_ok: 'decoded packets / bucket', packets_fail: 'failed packets / bucket' };
const G = { series: [], chart: null, timer: null, restored: false, seq: 0 };
let savedGraphs = [];

function metricLabel(m) { return METRIC_LABELS[m] || m; }

function buildSeriesRows() {
  const list = clear($('#series-list'));
  G.series.forEach((s, i) => {
    const metricSel = el('select', { 'data-role': 'metric' });
    const grp = (label, items) => items.length ? el('optgroup', { label }, ...items.map(m => el('option', { value: m }, metricLabel(m)))) : null;
    metricSel.append(grp('Link quality', ['rssi', 'snr']), grp('Counts', COUNT_METRICS), grp('Payload fields', S.fields));
    if (![...metricSel.options].some(o => o.value === s.metric)) metricSel.append(el('option', { value: s.metric }, s.metric));
    metricSel.value = s.metric;

    const nodeSel = el('select', { 'data-role': 'node' }, el('option', { value: '' }, 'any node'));
    if (S.nodes.includes('')) nodeSel.append(el('option', { value: '__untagged__' }, '(untagged)'));
    for (const n of S.nodes) if (n) nodeSel.append(el('option', { value: n }, n));
    if (s.node && ![...nodeSel.options].some(o => o.value === s.node)) nodeSel.append(el('option', { value: s.node }, s.node));
    nodeSel.value = s.node || '';

    const aggSel = el('select', { 'data-role': 'agg', title: 'how values are combined within a time bucket' },
      ...['avg', 'min', 'max', 'sum'].map(a => el('option', { value: a }, a)));
    aggSel.value = s.agg || 'avg';
    aggSel.disabled = COUNT_METRICS.includes(s.metric);

    const axisSel = el('select', { 'data-role': 'axis', title: 'which vertical axis to use' },
      el('option', { value: 'L' }, 'left axis'), el('option', { value: 'R' }, 'right axis'));
    axisSel.value = s.axis || 'L';

    const sync = () => {
      s.metric = metricSel.value; s.node = nodeSel.value; s.agg = aggSel.value; s.axis = axisSel.value;
      aggSel.disabled = COUNT_METRICS.includes(s.metric);
    };
    for (const c of [metricSel, nodeSel, aggSel, axisSel]) c.addEventListener('change', () => { sync(); plot(); });
    list.append(el('div', { class: 'series-row' },
      el('span', { class: 'swatch', style: `background:${PALETTE[i % PALETTE.length]}` }),
      metricSel, nodeSel, aggSel, axisSel,
      el('button', { type: 'button', class: 'small', title: 'remove this series', onclick: () => { G.series.splice(i, 1); buildSeriesRows(); plot(); } }, '\u2715')));
  });
}

function graphConfig() {
  return {
    range: $('#g-range').value, from: $('#g-from').value, to: $('#g-to').value,
    bucket: $('#g-bucket').value, type: $('#g-type').value, okonly: $('#g-okonly').checked,
    series: G.series.map(s => ({ metric: s.metric, node: s.node || '', agg: s.agg || 'avg', axis: s.axis || 'L' })),
  };
}
function applyGraphConfig(c) {
  if (!c) return;
  $('#g-range').value = c.range || '86400';
  $('#g-from').value = c.from || ''; $('#g-to').value = c.to || '';
  $('#g-bucket').value = c.bucket || ''; $('#g-type').value = c.type || 'line';
  $('#g-okonly').checked = c.okonly !== false;
  G.series = (c.series && c.series.length ? c.series : [{ metric: 'rssi' }]).map(s => ({ metric: s.metric, node: s.node || '', agg: s.agg || 'avg', axis: s.axis || 'L' }));
  toggleCustom('#g-range');
  buildSeriesRows();
}
function toggleCustom(rangeSel) {
  const custom = $(rangeSel).value === 'custom';
  $$('.custom-only', $(rangeSel).closest('.filters')).forEach(n => { n.hidden = !custom; });
}

async function loadGraphSaved() {
  try { savedGraphs = (await api('/api/saved/graph')).items; } catch (e) { savedGraphs = []; }
  const sel = clear($('#graph-select'));
  if (!savedGraphs.length) sel.append(el('option', { value: '' }, '(none saved)'));
  savedGraphs.forEach(g => sel.append(el('option', { value: g.name }, g.name)));
}

async function loadGraphsTab() {
  await refreshMetrics();
  if (!G.restored) {
    G.restored = true;
    let cfg = null;
    try { cfg = JSON.parse(localStorage.getItem('c6lmon.graph') || 'null'); } catch (e) { /* ignore */ }
    if (!cfg) {
      const first = S.fields.includes('co2') ? 'co2' : S.fields.includes('t') ? 't' : 'rssi';
      cfg = { range: '86400', series: [{ metric: 'rssi' }].concat(first !== 'rssi' ? [{ metric: first, axis: 'R' }] : []) };
    }
    applyGraphConfig(cfg);
  } else buildSeriesRows();
  loadGraphSaved();
  plot();
}

async function plot() {
  // Several things can trigger a plot in quick succession (editing a series, auto-refresh, ...).
  // Only the most recent request may draw, or a slow older response could overwrite a newer chart.
  const seq = ++G.seq;
  const cfg = graphConfig();
  try { localStorage.setItem('c6lmon.graph', JSON.stringify(cfg)); } catch (e) { /* ignore */ }
  const r = rangeValues('#g-range', '#g-from', '#g-to');
  const note = $('#graph-note');
  if (!G.series.length) { note.textContent = 'Add a series to plot.'; if (G.chart) { G.chart.destroy(); G.chart = null; } return; }
  note.textContent = 'Loading\u2026';
  let results;
  try {
    results = await Promise.all(G.series.map(s => {
      const p = { metric: s.metric, from: r.from, to: r.to, bucket: cfg.bucket, agg: s.agg };
      if (s.node === '__untagged__') p.node = ''; else if (s.node) p.node = s.node;
      if ((s.metric === 'rssi' || s.metric === 'snr') && cfg.okonly) p.ok = 'ok';
      return api('/api/series?' + qs(p));
    }));
  } catch (e) { if (seq === G.seq) note.textContent = 'Could not load: ' + e.message; return; }
  if (seq !== G.seq) return;                                 // superseded while we were waiting

  const used = { L: [], R: [] };
  const datasets = results.map((res, i) => {
    const s = G.series[i];
    const color = PALETTE[i % PALETTE.length];
    const side = s.axis === 'R' ? 'R' : 'L';
    used[side].push(metricLabel(s.metric));
    const pts = cfg.type === 'points';
    return {
      label: metricLabel(s.metric) + (s.node ? ' \u00b7 ' + (s.node === '__untagged__' ? '(untagged)' : s.node) : ''),
      data: res.points.map(([x, y]) => ({ x, y })), yAxisID: side === 'R' ? 'y1' : 'y',
      borderColor: color, backgroundColor: color, borderWidth: 1.5, tension: 0,
      pointRadius: pts ? 3 : 1.5, showLine: !pts,
    };
  });
  const total = datasets.reduce((n, d) => n + d.data.length, 0);
  const buckets = [...new Set(results.map(x => x.bucket).filter(Boolean))];
  note.textContent = total ? `${total} points` + (buckets.length ? ` \u00b7 aggregated per ${buckets.map(b => b >= 3600 ? (b / 3600) + ' h' : b >= 60 ? (b / 60) + ' min' : b + ' s').join(' / ')}` : '')
                           : 'No data for these series in this period.';

  if (G.chart) { G.chart.destroy(); G.chart = null; }
  const hasRight = used.R.length > 0;
  G.chart = new Chart($('#graph-canvas'), {
    type: 'line', data: { datasets },
    options: {
      parsing: false, normalized: true, animation: false, maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      scales: {
        x: { type: 'time', time: { tooltipFormat: 'yyyy-MM-dd HH:mm:ss' }, ticks: { maxRotation: 0, autoSkipPadding: 24 } },
        y: { position: 'left', title: { display: true, text: [...new Set(used.L)].join(', ') } },
        y1: { position: 'right', display: hasRight, grid: { drawOnChartArea: false }, title: { display: hasRight, text: [...new Set(used.R)].join(', ') } },
      },
      plugins: { legend: { display: true } },
    },
  });
}

function setAutoRefresh(on) {
  if (G.timer) { clearInterval(G.timer); G.timer = null; }
  if (on) G.timer = setInterval(() => { if (activeTab === 'graphs') plot(); }, 30000);
}

// ============================================================ wiring
function wire() {
  $$('.tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));

  $('#btn-connect').addEventListener('click', async () => {
    const port = $('#port-input').value.trim();
    if (!port) { S.netErr = null; $('#banner').hidden = false; $('#banner').textContent = 'Type or pick a serial port first.'; return; }
    try { await post('/api/connect', { port }); } catch (e) { $('#banner').hidden = false; $('#banner').textContent = e.message; }
  });
  $('#btn-disconnect').addEventListener('click', () => post('/api/disconnect').catch(() => {}));
  $('#btn-rescan').addEventListener('click', loadPorts);

  $('#send-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    const inp = $('#send-line'), line = inp.value.trim();
    if (!line) return;
    try { await post('/api/send', { line }); inp.value = ''; }
    catch (e) { addConsole([{ seq: L.consoleSeq, ts: Date.now() / 1000 + S.offset, text: '[monitor] ' + e.message }]); }
  });

  // settings
  $('#cfg-form').addEventListener('submit', applyForm);
  $('#cfg-form').addEventListener('input', () => { S.formDirty = true; });
  $('#f-freq').addEventListener('input', syncChannelSelect);
  $('#f-ch').addEventListener('change', () => { if ($('#f-ch').value) $('#f-freq').value = Number($('#f-ch').value).toFixed(3); S.formDirty = true; });
  $$('button[data-sync]').forEach(b => b.addEventListener('click', () => { $('#f-sync').value = b.dataset.sync; S.formDirty = true; }));
  $('#btn-form-reset').addEventListener('click', () => { S.formDirty = false; fillForm(S.device && S.device.config); });
  $('#btn-save').addEventListener('click', () => memAction('/api/config/save'));
  $('#btn-defaults').addEventListener('click', () => memAction('/api/config/defaults', 'Restore the firmware defaults and forget any saved settings on the device?'));
  $('#btn-preset-load').addEventListener('click', () => {
    const d = presetData($('#preset-select').value);
    if (d) { fillForm(d); S.formDirty = true; }
  });
  $('#btn-preset-save').addEventListener('click', async () => {
    const name = $('#preset-name').value.trim();
    if (!name) return;
    try { await send('PUT', '/api/saved/preset', { name, data: readForm() }); $('#preset-name').value = ''; await loadPresets(); $('#preset-select').value = 'u:' + name; }
    catch (e) { showResults($('#mem-result'), [[false, e.message]]); }
  });
  $('#btn-preset-delete').addEventListener('click', async () => {
    const v = $('#preset-select').value;
    if (!v.startsWith('u:')) return;
    try { await api('/api/saved/preset?name=' + encodeURIComponent(v.slice(2)), { method: 'DELETE' }); await loadPresets(); } catch (e) { /* ignore */ }
  });

  // log
  $('#log-filter').addEventListener('submit', ev => { ev.preventDefault(); LG.offset = 0; loadLog(); });
  $('#btn-log-reset').addEventListener('click', () => {
    $('#log-filter').reset(); LG.offset = 0; loadLog();
  });
  $('#btn-prev').addEventListener('click', () => { LG.offset = Math.max(0, LG.offset - logLimit()); loadLog(); });
  $('#btn-next').addEventListener('click', () => { LG.offset += logLimit(); loadLog(); });
  $('#btn-del-old').addEventListener('click', () => {
    const days = Math.max(1, parseInt($('#del-days').value, 10) || 30);
    deleteData('before=' + (Date.now() / 1000 + S.offset - days * 86400), `Delete all packets older than ${days} days? This cannot be undone.`);
  });
  $('#btn-del-all').addEventListener('click', () => deleteData('all=1', 'Delete ALL stored packets? This cannot be undone.'));

  // reports
  $('#r-range').addEventListener('change', () => toggleCustom('#r-range'));
  $('#btn-report').addEventListener('click', loadReport);

  // graphs
  $('#g-range').addEventListener('change', () => { toggleCustom('#g-range'); plot(); });
  for (const id of ['#g-from', '#g-to', '#g-bucket', '#g-type', '#g-okonly']) $(id).addEventListener('change', plot);
  $('#g-auto').addEventListener('change', ev => setAutoRefresh(ev.target.checked));
  $('#btn-add-series').addEventListener('click', () => {
    G.series.push({ metric: S.fields[0] || 'rssi', node: '', agg: 'avg', axis: G.series.length ? 'R' : 'L' });
    buildSeriesRows(); plot();
  });
  $('#btn-plot').addEventListener('click', plot);
  $('#btn-png').addEventListener('click', () => {
    if (!G.chart) return;
    el('a', { href: G.chart.toBase64Image(), download: 'c6l-graph.png' }).click();
  });
  $('#btn-graph-load').addEventListener('click', () => {
    const g = savedGraphs.find(x => x.name === $('#graph-select').value);
    if (g) { applyGraphConfig(g.data); plot(); }
  });
  $('#btn-graph-save').addEventListener('click', async () => {
    const name = $('#graph-name').value.trim();
    if (!name) return;
    try { await send('PUT', '/api/saved/graph', { name, data: graphConfig() }); $('#graph-name').value = ''; await loadGraphSaved(); $('#graph-select').value = name; }
    catch (e) { $('#graph-note').textContent = e.message; }
  });
  $('#btn-graph-delete').addEventListener('click', async () => {
    const name = $('#graph-select').value;
    if (!name) return;
    try { await api('/api/saved/graph?name=' + encodeURIComponent(name), { method: 'DELETE' }); await loadGraphSaved(); } catch (e) { /* ignore */ }
  });
}

function init() {
  if (window.Chart) {
    Chart.defaults.color = getComputedStyle(document.body).color;
    Chart.defaults.borderColor = 'rgba(128,128,128,0.25)';
  }
  buildStaticOptions();
  wire();
  loadPorts();
  showTab((location.hash || '#live').slice(1));
  pollLive();
  setInterval(tickAges, 1000);
}
init();

})();
