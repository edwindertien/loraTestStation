// Drives the REAL page (served by the real app, talking to the simulator) in jsdom.
// Only the chart canvas is mocked (jsdom has no canvas): the test checks what the app would
// hand to Chart.js, not the pixels.   usage: node smoke.js http://127.0.0.1:5099
const { JSDOM } = require('jsdom');
const BASE = process.argv[2] || 'http://127.0.0.1:5099';

let failures = 0, checks = 0;
const ok = (cond, msg) => { checks++; if (!cond) { failures++; console.log('  FAIL: ' + msg); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(fn, ms = 8000, label = '') {
  const end = Date.now() + ms;
  while (Date.now() < end) { try { const v = fn(); if (v) return v; } catch (e) { /* retry */ } await sleep(60); }
  console.log('  (timed out waiting for ' + label + ')');
  return null;
}
const apiJson = async (p, o) => (await fetch(BASE + p, o)).json();

(async () => {
  const html = await (await fetch(BASE + '/')).text();
  const appJs = await (await fetch(BASE + '/static/app.js')).text();

  const charts = [];
  class ChartMock {
    constructor(canvas, cfg) { this.canvas = canvas; this.config = cfg; charts.push(this); }
    destroy() { this.destroyed = true; }
    toBase64Image() { return 'data:image/png;base64,AAAA'; }
  }
  ChartMock.defaults = {};

  const clicked = [];
  const dom = new JSDOM(html, {
    url: BASE + '/', runScripts: 'outside-only', pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, BASE + '/').toString(), o);
      w.confirm = () => true;
      w.Chart = ChartMock;
      w.HTMLAnchorElement.prototype.click = function () { clicked.push(this.getAttribute('href')); };
    },
  });
  const w = dom.window, d = w.document;
  const $ = s => d.querySelector(s), $$ = s => Array.from(d.querySelectorAll(s));
  const scriptsBefore = $$('script').length;
  const errors = [];
  w.addEventListener('error', e => errors.push(e.message));
  w.eval(appJs);

  const change = (el, v) => { el.value = v; el.dispatchEvent(new w.Event('change', { bubbles: true })); el.dispatchEvent(new w.Event('input', { bubbles: true })); };
  const submit = f => f.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  const tab = name => $(`.tabs button[data-tab="${name}"]`).click();

  // ---------------------------------------------------------------- LIVE
  console.log('live tab');
  await waitFor(() => $$('#live-table tbody tr').length >= 2, 10000, 'live rows');
  ok($$('#live-table tbody tr').length >= 2, 'live table fills with packets');
  ok($('#conn-pill').textContent.startsWith('connected'), 'pill says connected: ' + $('#conn-pill').textContent);
  ok($('#conn-pill').className.includes('pill-on'), 'pill is green');
  await waitFor(() => $('#live-cfg').textContent.includes('868.100'), 5000, 'config card');
  ok($('#live-cfg').textContent.includes('868.100 MHz') && $('#live-cfg').textContent.includes('SF7'), 'radio card shows settings: ' + $('#live-cfg').textContent);
  ok($('#live-cfg-state').textContent.includes('defaults'), 'settings state shown');
  ok(!$('#last-card').hidden && $$('#last-fields .tile').length >= 6, 'last-packet card shows payload fields as tiles');
  ok($('#last-head').textContent.includes('SEN66-0'), 'last-packet head names the node');
  ok($('#console').textContent.includes('json=on'), 'console shows the json handshake');
  ok(!$('#console').textContent.includes('listening...'), 'heartbeats are kept out of the console');
  ok(/^\d+ \/ \d+$/.test($('#live-session').textContent), 'session counters: ' + $('#live-session').textContent);
  ok(parseInt($('#live-db').textContent) >= 2, 'db counter');
  const row = $('#live-table tbody tr').textContent;
  ok(/SEN66-0\d/.test(row) || row.includes('not decoded'), 'rows show node or failure: ' + row.slice(0, 80));

  // console send
  $('#send-line').value = 'stats';
  submit($('#send-form'));
  ok(await waitFor(() => $('#console').textContent.includes('RX ok:'), 5000, 'stats reply in console'), 'sent line gets a reply in the console');

  // ---------------------------------------------------------------- SETTINGS
  console.log('settings tab');
  tab('settings');
  ok($('#tab-settings').classList.contains('active') && !$('#tab-live').classList.contains('active'), 'tab switches');
  ok($('#f-sf').value === '7' && $('#f-bw').value === '125' && $('#f-cr').value === '7' && $('#f-sync').value === '12', 'form filled from device: sf=' + $('#f-sf').value + ' bw=' + $('#f-bw').value + ' cr=' + $('#f-cr').value + ' sync=' + $('#f-sync').value);
  ok($('#f-freq').value === '868.100' && $('#f-ch').value === '868.1', 'frequency and matching EU868 channel');
  ok($$('#f-ch option').length === 9 && $$('#f-bw option').length === 10 && $$('#f-sf option').length === 8, 'option lists built');
  ok(await waitFor(() => $$('#preset-select option').length >= 2, 5000, 'presets'), 'built-in presets listed');

  change($('#f-ch'), '867.5');
  ok($('#f-freq').value === '867.500', 'picking a channel fills the frequency');
  change($('#f-sf'), '9');
  submit($('#cfg-form'));
  await waitFor(() => $('#cfg-result').textContent.includes('sf:'), 5000, 'apply result');
  ok($('#cfg-result .okline') && $('#cfg-result').textContent.includes('freq: ok') && $('#cfg-result').textContent.includes('sf: ok'), 'apply shows per-setting results: ' + $('#cfg-result').textContent);
  let st = await apiJson('/api/status');
  ok(st.device.config.sf === 9 && Math.abs(st.device.config.freq - 867.5) < 1e-6, 'device really changed (sf=' + st.device.config.sf + ')');
  ok(await waitFor(() => $('#cfg-state').textContent === 'modified', 4000, 'state label'), 'state shows modified');

  change($('#f-sf'), '99'.slice(0, 1) === '9' ? '12' : '12');                     // valid value...
  $('#f-sync').value = 'zz'; $('#f-sync').dispatchEvent(new w.Event('input', { bubbles: true }));   // ...plus junk
  submit($('#cfg-form'));
  await waitFor(() => $('#cfg-result .errline'), 5000, 'error line');
  ok($('#cfg-result .errline') && $('#cfg-result').textContent.includes('invalid value'), 'a rejected setting is shown as an error: ' + $('#cfg-result').textContent);

  $('#btn-form-reset').click();
  ok($('#f-sync').value === '12' && $('#f-sf').value !== '99', 'Reset form restores device values');

  $('#btn-defaults').click();
  await waitFor(() => $('#mem-result').textContent.includes('defaults restored'), 5000, 'defaults');
  ok($('#mem-result .okline'), 'restore defaults reports ok: ' + $('#mem-result').textContent);
  await waitFor(() => $('#f-sf').value === '7', 5000, 'form back to defaults');
  ok($('#f-sf').value === '7' && $('#cfg-state').textContent === 'defaults', 'form and state back to defaults');

  $('#f-sf').value = '8'; $('#f-sf').dispatchEvent(new w.Event('input', { bubbles: true })); submit($('#cfg-form'));
  await waitFor(() => $('#cfg-state').textContent === 'modified', 5000, 'modified');
  $('#btn-save').click();
  ok(await waitFor(() => $('#cfg-state').textContent === 'saved', 5000, 'saved state'), 'Save to device -> state saved');
  $('#btn-defaults').click();
  await waitFor(() => $('#cfg-state').textContent === 'defaults', 5000, 'defaults again');

  // presets: built-in load, user save / load / delete
  change($('#preset-select'), 'b:1');
  $('#btn-preset-load').click();
  ok($('#f-sync').value === '34' && $('#f-cr').value === '5', 'loading the LoRaWAN preset fills sync 34 / CR 5');
  $('#preset-name').value = 'my <b>preset</b>';
  $('#btn-preset-save').click();
  ok(await waitFor(() => $$('#preset-select option').some(o => o.textContent === 'my <b>preset</b>'), 5000, 'saved preset'), 'saved preset appears (name shown as text)');
  ok($$('#preset-select b').length === 0, 'a preset name containing markup is not rendered as markup');
  $('#btn-preset-delete').click();
  ok(await waitFor(() => !$$('#preset-select option').some(o => o.textContent === 'my <b>preset</b>'), 5000, 'deleted preset'), 'preset can be deleted');

  // ---------------------------------------------------------------- LOG
  console.log('log tab');
  tab('log');
  await waitFor(() => $$('#log-table tbody tr.clickable').length >= 2, 8000, 'log rows');
  ok($$('#log-table tbody tr.clickable').length >= 2, 'log table fills');
  ok($('#log-count').textContent.includes('packets'), 'count line: ' + $('#log-count').textContent);
  ok($$('#l-node option').some(o => o.value === 'SEN66-01'), 'node filter lists nodes');
  const det = $('#log-table tbody tr.detail');
  ok(det.hidden, 'detail row starts collapsed');
  $('#log-table tbody tr.clickable').click();
  ok(!det.hidden && det.textContent.includes('packet #'), 'clicking a row expands its detail: ' + det.textContent.slice(0, 60));
  ok($('#exp-csv').getAttribute('href').startsWith('/api/packets/export.csv'), 'export links built');

  change($('#l-node'), 'SEN66-01'); submit($('#log-filter'));
  await waitFor(() => $('#exp-csv').getAttribute('href').includes('node=SEN66-01'), 4000, 'filter href');
  ok($('#exp-csv').getAttribute('href').includes('node=SEN66-01'), 'node filter reaches the export link');
  await waitFor(() => $$('#log-table tbody tr.clickable').every(r => r.textContent.includes('SEN66-01')), 4000, 'filtered rows');
  ok($$('#log-table tbody tr.clickable').every(r => r.textContent.includes('SEN66-01')), 'only SEN66-01 rows remain');
  $('#btn-log-reset').click();

  change($('#l-limit'), '25'); $('#l-q').value = 'script'; submit($('#log-filter'));
  await waitFor(() => $$('#log-table tbody tr.clickable').some(r => r.textContent.includes('script')), 5000, 'hostile rows');

  // ---- hostile payloads (inserted into the DB beforehand by the runner) must stay inert text
  console.log('hostile payloads');
  const bodyText = d.body.textContent;
  ok(bodyText.includes('<script>window.__pwned=1</script>'), 'the literal "<script>" text is shown as text');
  ok($$('script').length === scriptsBefore, 'no <script> elements were created by payload content');
  ok($$('img, iframe, object, embed, svg').length === 0, 'no <img>/<iframe>/... elements created by payload content');
  ok(!w.__pwned, 'no payload code ran');
  ok($$('#log-table [onerror], #live-table [onerror], [onclick*="__pwned"]').length === 0, 'no event-handler attributes from payload content');
  $('#l-q').value = ''; $('#btn-log-reset').click();

  // ---------------------------------------------------------------- REPORTS
  console.log('reports tab');
  tab('reports');
  change($('#r-range'), 'all');
  $('#btn-report').click();
  await waitFor(() => $('#report-body .stats'), 8000, 'report');
  ok($$('#report-body .stat').length === 6, 'six summary tiles');
  ok($('#report-body').textContent.includes('SEN66-01'), 'per-node table lists SEN66-01');
  ok(/Typical interval/.test($('#report-body').textContent), 'interval column present');
  const bar = charts.filter(c => c.config.type === 'bar').pop();
  ok(bar && bar.config.data.datasets.length === 2 && bar.config.data.labels.length >= 1, 'activity chart gets decoded/failed datasets');
  ok($('#exp-report').getAttribute('href').startsWith('/api/report.csv'), 'report CSV link set');
  change($('#r-range'), 'custom');
  ok($$('#tab-reports .custom-only').every(n => !n.hidden), 'custom range shows from/to inputs');
  change($('#r-range'), '3600');
  ok($$('#tab-reports .custom-only').every(n => n.hidden), 'and hides them again');

  // ---------------------------------------------------------------- GRAPHS
  console.log('graphs tab');
  tab('graphs');
  await waitFor(() => charts.some(c => c.config.type === 'line' && !c.destroyed), 8000, 'line chart');
  let line = charts.filter(c => c.config.type === 'line' && !c.destroyed).pop();
  ok(line, 'a line chart is created');
  ok($$('#series-list .series-row').length >= 1, 'series rows built');
  ok($$('#series-list select[data-role="metric"] option').some(o => o.value === 'co2'), 'payload fields offered as metrics');
  ok(line.config.options.scales.x.type === 'time', 'x axis is a time scale');
  await waitFor(() => line.config.data.datasets[0].data.length >= 1, 6000, 'points');
  ok(line.config.data.datasets[0].data.length >= 1 && typeof line.config.data.datasets[0].data[0].x === 'number', 'datasets carry {x: epoch ms, y} points');

  // the default graph already has two series: RSSI on the left axis, a payload field on the right
  ok($$('#series-list .series-row').length === 2, 'default graph has two series: ' + $$('#series-list .series-row').length);
  await waitFor(() => line.config.data.datasets.length === 2, 6000, 'two datasets');
  ok(line.config.data.datasets.length === 2, 'default graph plots both series');
  ok(line.config.data.datasets[1].yAxisID === 'y1' && line.config.options.scales.y1.display === true, 'second series goes on the right axis and the axis appears');
  $$('#series-list .series-row button')[1].click();                       // remove the second series
  await waitFor(() => charts.filter(c => c.config.type === 'line' && !c.destroyed).pop().config.data.datasets.length === 1, 6000, 'one dataset');
  line = charts.filter(c => c.config.type === 'line' && !c.destroyed).pop();
  ok($$('#series-list .series-row').length === 1 && line.config.data.datasets.length === 1 && line.config.options.scales.y1.display === false,
     'removing a series replots with one dataset and hides the right axis');
  $('#btn-add-series').click();                                           // add one back
  await waitFor(() => charts.filter(c => c.config.type === 'line' && !c.destroyed).pop().config.data.datasets.length === 2, 6000, 'two datasets again');
  const m2 = $$('#series-list select[data-role="metric"]')[1];
  change(m2, 'co2');
  await waitFor(() => charts.filter(c => c.config.type === 'line' && !c.destroyed).pop().config.data.datasets[1].label.includes('co2'), 6000, 'co2 series');
  line = charts.filter(c => c.config.type === 'line' && !c.destroyed).pop();
  ok(line.config.data.datasets[1].label.includes('co2') && line.config.data.datasets[1].data.length >= 1, 'changing a series metric replots it');
  ok(line.config.options.scales.y1.title.text.includes('co2'), 'right axis is titled after its series');

  change($('#g-type'), 'points');
  await waitFor(() => charts.filter(c => c.config.type === 'line' && !c.destroyed).pop().config.data.datasets[0].showLine === false, 6000, 'points style');
  ok(charts.filter(c => c.config.type === 'line' && !c.destroyed).pop().config.data.datasets[0].showLine === false, 'style "points" turns the connecting line off');

  const cfgStored = JSON.parse(w.localStorage.getItem('c6lmon.graph'));
  ok(cfgStored && cfgStored.series.length === 2 && cfgStored.type === 'points' && cfgStored.series[1].metric === 'co2', 'graph config is remembered locally');

  $('#graph-name').value = 'overview';
  $('#btn-graph-save').click();
  ok(await waitFor(() => $$('#graph-select option').some(o => o.value === 'overview'), 5000, 'saved graph'), 'graph can be saved');
  $('#btn-add-series').click();                                  // change something (now 3 series), then load the saved one back
  await sleep(300);
  ok($$('#series-list .series-row').length === 3, 'a third series was added before loading');
  change($('#graph-select'), 'overview'); $('#btn-graph-load').click();
  await waitFor(() => $$('#series-list .series-row').length === 2, 4000, 'restored series');
  ok($$('#series-list .series-row').length === 2 && $('#g-type').value === 'points', 'loading a saved graph restores series and options');
  $('#btn-graph-delete').click();
  ok(await waitFor(() => !$$('#graph-select option').some(o => o.value === 'overview'), 5000, 'graph deleted'), 'saved graph can be deleted');
  $('#btn-png').click();
  ok(clicked.some(h => h && h.startsWith('data:image/png')), 'PNG download uses the chart image');

  // ---- a stale (slow) plot response must not overwrite a newer chart
  console.log('plot ordering');
  const realFetch = w.fetch; let slowOnce = true;
  w.fetch = (u, o) => (String(u).includes('/api/series') && slowOnce && (slowOnce = false, true))
        ? sleep(1200).then(() => realFetch(u, o)) : realFetch(u, o);
  change($('#g-bucket'), '60');                                           // plot #1: slow
  await sleep(100);
  change($('#g-bucket'), '300');                                          // plot #2: fast, issued later
  await sleep(2200);
  w.fetch = realFetch;
  const finalChart = charts.filter(c => c.config.type === 'line' && !c.destroyed).pop();
  ok(/5 min/.test($('#graph-note').textContent) && !/1 min/.test($('#graph-note').textContent), 'the newest request wins even if an older one finishes later: ' + $('#graph-note').textContent);
  ok(finalChart && charts.filter(c => c.config.type === 'line' && !c.destroyed).length === 1, 'only one live chart remains');

  // ---------------------------------------------------------------- connection
  console.log('connection');
  $('#btn-disconnect').click();
  ok(await waitFor(() => $('#conn-pill').textContent === 'disconnected', 6000, 'disconnect'), 'Disconnect works: ' + $('#conn-pill').textContent);
  ok($('#btn-disconnect').disabled, 'Disconnect button disabled when disconnected');
  $('#btn-connect').click();
  ok(await waitFor(() => $('#conn-pill').textContent.startsWith('connected'), 10000, 'reconnect'), 'Connect works again');
  $('#port-input').value = ''; $('#btn-connect').click();
  ok(!$('#banner').hidden && $('#banner').textContent.includes('serial port'), 'Connect with no port explains what to do');

  ok(errors.length === 0, 'no uncaught JS errors: ' + errors.join(' | '));
  console.log(`\n${checks} checks, ${failures ? failures + ' FAILED' : 'all passed'}`);
  w.close();
  process.exit(failures ? 1 : 0);
})().catch(e => { console.log('SMOKE TEST CRASHED:', e); process.exit(2); });
