'use strict';

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clock = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
const fmt = (value, unit) => unit === 'ms' ? `${Number(value).toLocaleString('en-US')} ms` : unit === '%' ? `${value}%` : String(value);

const state = { id: null, data: null, look: null, confirm: null, busy: false, briefings: null };

function remember(id) {
  try { id ? sessionStorage.setItem('incident-run', id) : sessionStorage.removeItem('incident-run'); } catch (_) { /* storage unavailable */ }
}
function recall() {
  try { return sessionStorage.getItem('incident-run'); } catch (_) { return null; }
}

async function api(path, body) {
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, options);
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof json.detail === 'string' ? json.detail : `Request failed (${res.status}).`);
  return json;
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
}

async function run(task) {
  if (state.busy) return;
  state.busy = true;
  document.body.classList.add('busy');
  try { await task(); } catch (err) { toast(err.message); } finally {
    state.busy = false;
    document.body.classList.remove('busy');
  }
}

function accept(data) {
  state.data = data;
  state.id = data.id;
  remember(data.id);
  if (data.result && data.result.view) state.look = data.result;
  render();
}

function show(section) {
  for (const id of ['intro', 'console', 'debrief']) $(`#${id}`).hidden = id !== section;
  window.scrollTo({ top: 0 });
}

// ---------- rendering -------------------------------------------------------------------------------------
function render() {
  const data = state.data;
  if (!data) return show('intro');
  if (data.view.ended) { renderDebrief(); return show('debrief'); }
  if ($('#console').hidden) show('console');
  renderStatus();
  renderLook();
  renderActions();
  renderTimeline($('#timeline'), data.view.events, true);
}

function renderStatus() {
  const v = state.data.view;
  $('#sb-title').textContent = v.title;
  $('#sb-clock').textContent = v.clock;
  $('#sb-elapsed').textContent = `${v.elapsed} of ${v.limit} min`;
  const status = $('#sb-status');
  status.textContent = v.status;
  status.className = `pill ${v.status}`;
  const err = $('#sb-error');
  err.textContent = `${v.error_rate}%`;
  err.className = `mono big-number ${v.error_rate > 5 ? 'err-high' : v.error_rate > 1 ? 'err-mid' : 'err-ok'}`;
  $('#sb-failed').textContent = v.failed.toLocaleString('en-US');
  const pending = $('#pending');
  pending.hidden = !v.pending.length;
  pending.innerHTML = v.pending.map((p) => `<span>${esc(p.label)}: about ${p.minutes_left} min left</span>`).join('');
  const alert = $('#alert');
  if (v.status === 'mitigated') {
    alert.className = 'alert calm';
    alert.innerHTML = `<b>OK</b> Checkout errors have stayed under 1% for 3+ minutes (now ${esc(v.error_rate)}%). Declare it resolved when you're confident.`;
  } else {
    alert.className = 'alert';
    alert.innerHTML = `<b>PAGE ${esc(v.alert.opened)}</b> checkout-api 5xx rate above ${esc(v.alert.threshold)}% · now ${esc(v.error_rate)}% · p99 ${esc(fmt(v.p99_ms, 'ms'))}`;
  }
}

function renderLook() {
  const look = state.look;
  document.querySelectorAll('[data-look]').forEach((b) => {
    b.classList.toggle('active', !!look && b.dataset.look === look.tool && (b.dataset.arg || null) === (look.arg || null));
  });
  if (!look) return;
  const event = state.data.view.events.find((e) => e.id === look.event);
  const when = event ? event.clock : '';
  const v = look.view;
  let title = '', body = '';
  if (look.tool === 'metrics') {
    title = `${v.service} metrics`;
    body = `<div class="charts">${v.charts.map((c) => chart(c, v.deploy_minute)).join('')}</div>`;
  } else if (look.tool === 'logs') {
    title = `${v.service} logs`;
    body = `<div class="log-tools"><label><input type="checkbox" id="errors-only"> Errors only</label></div>
      <div class="logs" id="log-lines">${v.lines.map((l) => `<div class="lvl-${esc(l.level)}"><span class="t">${esc(l.time)}</span> <span class="${esc(l.level)}">${esc(l.level.padEnd(5))}</span> ${esc(l.message)}</div>`).join('')}</div>`;
  } else if (look.tool === 'deploys') {
    title = 'Deploy history';
    body = `<div class="rows">${v.releases.map((r) => `<div class="row"><div><code>${esc(r.version)}</code> ${esc(r.title)}<div class="meta">${esc(r.service)} · ${esc(r.when)} · ${esc(r.author)} · ${esc(r.status)}</div></div>
      <button class="small-btn" data-release="${esc(r.version)}">Release notes (1 min)</button></div>`).join('')}</div>`;
  } else if (look.tool === 'release') {
    title = `Release notes: ${v.version}`;
    const m = v.migration;
    body = `<div class="release"><div class="meta">${esc(v.service)} · ${esc(v.when)} · ${esc(v.author)}</div><h3>${esc(v.title)}</h3>
      <ul>${v.changes.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>
      ${m ? `<div class="migration ${m.reversible ? 'safe' : 'unsafe'}"><b>Migration ${esc(m.name)}</b>: ${esc(m.note)}</div>` : '<div class="meta">No database migration.</div>'}</div>`;
  } else if (look.tool === 'flags') {
    title = 'Feature flags';
    const live = state.data.view.flags;
    body = `<div class="rows">${v.flags.map((f) => {
      const on = live[f.name];
      const pending = state.data.view.pending.some((p) => p.kind === 'flag' && p.arg === f.name);
      return `<div class="row"><div><code>${esc(f.name)}</code> <span class="flag-state ${on ? 'on' : 'off'}">${on ? `ON ${esc(f.rollout)}%` : 'OFF'}</span>
        <div class="meta">Changed ${esc(f.changed)}</div></div>
        <button class="small-btn" data-flag="${esc(f.name)}" ${pending ? 'disabled' : ''}>${pending ? 'Rolling out…' : on ? 'Turn off' : 'Turn on'}</button>
        <div class="desc">${esc(f.description)}</div></div>`;
    }).join('')}</div>`;
  } else if (look.tool === 'runbook') {
    title = 'Runbook: checkout';
    body = `<div class="runbook">${v.sections.map((s) => `<h3>${esc(s.title)}</h3><ul>${s.steps.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`).join('')}</div>`;
  }
  $('#look').innerHTML = `<div class="look-title"><b>${esc(title)}</b><span>Snapshot taken at ${esc(when)}</span></div>${body}`;
  const toggle = $('#errors-only');
  if (toggle) toggle.addEventListener('change', () => $('#log-lines').classList.toggle('errors-only', toggle.checked));
}

function chart(c, deployMinute) {
  const W = 600, H = 130, L = 40, R = 10, T = 10, B = 20;
  const xs = c.series.map((p) => p[0]), ys = c.series.map((p) => p[1]);
  const x0 = xs[0], x1 = Math.max(xs[xs.length - 1], x0 + 1);
  const yMax = Math.max(...ys, c.unit === '%' ? 2 : 1) * 1.15;
  const x = (m) => L + (m - x0) / (x1 - x0) * (W - L - R);
  const y = (v) => T + (1 - v / yMax) * (H - T - B);
  const points = c.series.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`);
  const line = `M${points.join('L')}`;
  const area = `${line}L${x(x1).toFixed(1)},${y(0)}L${x(x0).toFixed(1)},${y(0)}Z`;
  const dx = x(deployMinute);
  return `<div class="chart"><div class="chart-head"><span>${esc(c.title)}</span><b>${esc(fmt(c.latest, c.unit))}</b></div>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(c.title)} from ${clock(x0)} to ${clock(x1)}, latest ${esc(fmt(c.latest, c.unit))}">
      <line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>
      <line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(yMax / 2)}" y2="${y(yMax / 2)}"/>
      <text class="axis" x="${L - 4}" y="${y(yMax / 2) + 3}" text-anchor="end">${esc(fmt(Math.round(yMax / 2), c.unit === 'ms' ? '' : c.unit))}</text>
      <text class="axis" x="${L - 4}" y="${y(0) + 3}" text-anchor="end">0</text>
      <path class="area" d="${area}"/><path class="line" d="${line}"/>
      <line class="deploy" x1="${dx}" x2="${dx}" y1="${T}" y2="${H - B}"/>
      <text class="deploy-label" x="${dx > W * 0.7 ? dx - 4 : dx + 4}" y="${T + 9}" text-anchor="${dx > W * 0.7 ? 'end' : 'start'}">v2.14.0 deploy</text>
      <text class="axis" x="${L}" y="${H - 5}">${clock(x0)}</text>
      <text class="axis" x="${W - R}" y="${H - 5}" text-anchor="end">${clock(x1)}</text>
    </svg></div>`;
}

function renderActions() {
  const v = state.data.view;
  const box = $('#actions');
  const c = state.confirm;
  const confirm = c ? `<div class="confirm" role="group" aria-label="Confirm action"><span>${esc(c.label)}${c.delay ? `: takes about ${c.delay} min to take effect` : ''}. Go ahead?</span>
    <div><button class="btn primary" id="confirm-yes">Confirm</button><button class="btn" id="confirm-no">Cancel</button></div></div>` : '';
  const items = Object.entries(v.actions).filter(([name]) => name !== 'status').map(([name, a]) => {
    if (name === 'flag') return `<button class="action" data-open-flags><span>Change a feature flag</span><small>opens Flags · 1 min</small></button>`;
    return `<button class="action" data-action="${esc(name)}" ${a.disabled ? 'disabled' : ''} title="${esc(a.disabled)}"><span>${esc(a.label)}</span><small>${a.disabled ? esc(a.disabled) : `${a.delay} min`}</small></button>`;
  });
  box.innerHTML = confirm + items.join('');
  const text = $('#status-text');
  if (!text.value) text.value = 'Investigating: some customers cannot complete checkout since about 14:03. We are working on a fix and will update within 15 minutes.';
}

function renderTimeline(list, events, follow) {
  if (!events.length) {
    list.innerHTML = '<li class="timeline-empty">Nothing yet. Your first look or action will appear here.</li>';
    return;
  }
  list.innerHTML = events.map((e) => `<li id="${list.id}-${esc(e.id)}" class="k-${esc(e.kind)}"><span class="id">${esc(e.id)}</span><span class="when">${esc(e.clock)}</span>
    <span class="what"><b>${esc(e.label)}</b>${e.detail ? `<small>${esc(e.detail)}</small>` : ''}</span></li>`).join('');
  if (follow) list.scrollTop = list.scrollHeight;
}

function renderDebrief() {
  const data = state.data, v = data.view, d = data.debrief;
  const s = d.stats;
  $('#db-eyebrow').textContent = v.variant === 'B' ? 'Debrief · Pressure Replay' : 'Debrief';
  $('#db-headline').textContent = s.minutes_to_mitigate !== null
    ? `Checkout recovered ${s.minutes_to_mitigate} minutes into your shift.`
    : 'Checkout was still failing when your shift ended.';
  const stats = [
    ['Time to healthy', s.minutes_to_mitigate !== null ? `${s.minutes_to_mitigate} min` : 'not reached'],
    ['Failed checkouts', s.failed.toLocaleString('en-US')],
    ['Checked before first change', String(s.looks_before_first_change)],
    ['Unhelpful changes', String(s.unhelpful_changes)],
  ];
  $('#db-stats').innerHTML = stats.map(([k, val]) => `<div class="stat"><span>${esc(k)}</span><b>${esc(val)}</b></div>`).join('');
  const tones = { good: 'Went well', gap: 'Gap', info: 'Fact' };
  $('#db-facts').innerHTML = d.facts.map((f) => `<li class="fact ${esc(f.tone)}"><span class="tone">${esc(tones[f.tone])}</span><p>${esc(f.text)}</p>
    ${f.events.length ? `<div class="refs">${f.events.map((id) => `<button class="ref" data-ref="${esc(id)}">${esc(id)}</button>`).join('')}</div>` : ''}</li>`).join('');
  renderTimeline($('#db-timeline'), v.events, false);
  const compare = data.comparison;
  $('#db-compare').hidden = !compare;
  if (compare) {
    $('#db-compare-rows').innerHTML = compare.map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(r.first)}</td><td>${esc(r.second)}</td></tr>`).join('');
  }
  $('#replay-cta').hidden = v.variant !== 'A';
}

// ---------- commands --------------------------------------------------------------------------------------
const start = (variant, previous) => run(async () => {
  state.look = null;
  state.confirm = null;
  accept(await api('/api/runs', { variant, previous }));
});
const look = (tool, arg) => run(async () => { state.confirm = null; accept(await api(`/api/runs/${state.id}/look`, { tool, arg })); });
const act = (action, arg, text) => run(async () => { state.confirm = null; accept(await api(`/api/runs/${state.id}/act`, { action, arg, text })); });
const resolve = () => run(async () => { state.confirm = null; accept(await api(`/api/runs/${state.id}/resolve`, {})); });

document.addEventListener('click', (event) => {
  const t = event.target.closest('button');
  if (!t || t.disabled) return;
  if (t.dataset.look) look(t.dataset.look, t.dataset.arg || null);
  else if (t.dataset.release) look('release', t.dataset.release);
  else if (t.hasAttribute('data-open-flags')) look('flags', null);
  else if (t.dataset.flag) {
    const on = state.data.view.flags[t.dataset.flag];
    state.confirm = { action: 'flag', arg: t.dataset.flag, label: `Turn ${t.dataset.flag} ${on ? 'off' : 'on'}`, delay: 1 };
    renderActions();
    $('#confirm-yes').focus();
  } else if (t.dataset.action) {
    const a = state.data.view.actions[t.dataset.action];
    state.confirm = { action: t.dataset.action, arg: null, label: a.label, delay: a.delay };
    renderActions();
    $('#confirm-yes').focus();
  } else if (t.id === 'confirm-yes') {
    if (state.confirm.action === 'resolve') resolve();
    else act(state.confirm.action, state.confirm.arg, '');
  } else if (t.id === 'confirm-no') { state.confirm = null; renderActions(); }
  else if (t.dataset.ref) {
    const item = document.getElementById(`db-timeline-${t.dataset.ref}`);
    if (item) {
      item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      item.classList.add('flash');
      setTimeout(() => item.classList.remove('flash'), 1600);
    }
  }
});

$('#start').addEventListener('click', () => start('A'));
$('#post-status').addEventListener('click', () => act('status', null, $('#status-text').value));
$('#wait').addEventListener('click', () => run(async () => accept(await api(`/api/runs/${state.id}/wait`, {}))));
$('#resolve').addEventListener('click', () => {
  state.confirm = { action: 'resolve', arg: null, label: 'Declare the incident resolved and end your shift', delay: 0 };
  renderActions();
  $('#confirm-yes').focus();
});
$('#replay').addEventListener('click', () => start('B', state.id));
$('#restart').addEventListener('click', () => { state.data = null; state.id = null; state.look = null; remember(null); show('intro'); });

(async function init() {
  try {
    const info = await api('/api/scenario');
    const a = info.variants.A;
    $('#intro-briefing').innerHTML = `<strong>Page · 14:05</strong>${esc(a.briefing)}`;
  } catch (_) { /* briefing is optional */ }
  const saved = recall();
  if (saved) {
    try { accept(await api(`/api/runs/${saved}`)); return; } catch (_) { remember(null); }
  }
  show('intro');
})();
