/* Home workspace: saved device configuration and a drag-and-drop rule builder. */
'use strict';
const home = {document: null, readings: {}, events: [], view: 'devices', selected: null, dirty: false, drag: null, dialog: null};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function uid(prefix) {
  // getRandomValues also works on the HTTP LAN address used by Umbrel.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return prefix + '-' + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}
const selectedRule = () => home.document.rules.find(r => r.id === home.selected);
const deviceName = id => home.document.devices.find(d => d.id === id)?.name || id;

async function api(path, options = {}) {
  const response = await fetch('/api/home' + path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The Home workspace could not be reached.');
  return result;
}
function post(path, body) {
  return api(path, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
}
let noticeTimer;
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $('notice').hidden = false;
  $('notice').className = error ? 'error' : '';
  $('notice').textContent = message;
  if (!error) noticeTimer = setTimeout(() => {$('notice').hidden = true;}, 5000);
}
function changed() {
  home.dirty = true;
  $('save-state').textContent = 'Unsaved changes';
  $('save-workspace').disabled = false;
  $('device-count').textContent = home.document.devices.length;
  $('rule-count').textContent = home.document.rules.length;
}
function ageLabel(iso) {
  const age = (Date.now() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(age)) return 'No report yet';
  if (age < 60) return 'Just received';
  if (age < 3600) return Math.floor(age / 60) + 'm ago';
  return Math.floor(age / 3600) + 'h ago';
}
function freshness(d, row) {
  const metrics = d.kind === 'plug' ? ['power'] : ['temperature_f','humidity_pct'];
  const dates = metrics.map(m => Date.parse(row.updated_at?.[m]));
  if (!dates.some(Number.isFinite)) return {style:'', text:'No report yet'};
  const oldest = Math.min(...dates);
  const fresh = dates.every(Number.isFinite) && Date.now() - oldest < 1800000 && row.status === 'connected';
  const stamp = dates.filter(Number.isFinite).sort((a,b) => a-b)[0];
  return {style:fresh ? 'fresh' : 'old', text: row.status === 'offline' ? 'Connection offline' : ageLabel(new Date(stamp).toISOString())};
}
function itemButtons(id, kind, index, length) {
  return `<div class="mini-buttons"><button data-action="move-${kind}" data-id="${esc(id)}" data-direction="-1" ${index === 0 ? 'disabled' : ''} aria-label="Move earlier">↑</button><button data-action="move-${kind}" data-id="${esc(id)}" data-direction="1" ${index === length-1 ? 'disabled' : ''} aria-label="Move later">↓</button><button data-action="edit-${kind}" data-id="${esc(id)}" aria-label="Edit ${kind}">⚙</button></div>`;
}
function renderDevices() {
  const devices = home.document.devices;
  $('device-count').textContent = devices.length;
  $('connected-count').textContent = `${devices.filter(d => d.enabled && home.readings[d.id]?.status === 'connected').length} reporting · ${devices.length} total`;
  $('devices-grid').innerHTML = devices.length ? devices.map((d, i) => {
    const row = home.readings[d.id] || {};
    const freshnessInfo = freshness(d, row);
    const hub = home.document.hubs.find(h => h.id === d.hub_id);
    const meta = d.transport === 'zigbee' ? `Zigbee · ${hub?.name || 'Choose a hub'}` : 'Wi-Fi · Local Tuya';
    const temp = row.temperature_f == null ? '—' : row.temperature_f.toFixed(1);
    const humidity = row.humidity_pct == null ? '—' : row.humidity_pct.toFixed(0);
    const pending = row.pending || home.dirty || !d.enabled;
    const held = row.hold_until * 1000 > Date.now();
    return `<article class="device-card" data-drag="device" data-id="${esc(d.id)}">
      <div class="card-top"><span class="device-symbol ${d.kind}" data-handle="true" title="Drag to arrange">${d.kind === 'plug' ? '⏻' : '♧'}</span>${itemButtons(d.id,'device',i,devices.length)}</div>
      <h4>${esc(d.name)}</h4><div class="device-meta">${esc(meta)}</div>
      <div class="device-readings">${d.kind === 'sensor' ? `<div><div class="reading-label">Temperature</div><div class="reading-value">${temp}<small>°F</small></div></div><div><div class="reading-label">Humidity</div><div class="reading-value">${humidity}<small>%</small></div></div>` : `<div><div class="reading-label">Power</div><div class="reading-value">${row.power == null ? '—' : row.power ? 'On' : 'Off'}</div></div><div class="power-controls"><button class="${row.power === true ? 'on' : ''}" data-action="power" data-id="${esc(d.id)}" data-on="true" ${pending ? 'disabled' : ''}>ON</button><button data-action="power" data-id="${esc(d.id)}" data-on="false" ${pending ? 'disabled' : ''}>OFF</button></div>`}</div>
      ${row.pending ? '<div class="held">Waiting for plug confirmation…</div>' : ''}
      ${held ? `<div class="held">Manual hold · ${Math.ceil((row.hold_until*1000-Date.now())/60000)}m <button data-action="resume" data-id="${esc(d.id)}">Resume rules</button></div>` : ''}
      ${row.command_error ? `<div class="device-error">${esc(row.command_error)}</div>` : ''}
      <div class="card-bottom"><span><i class="fresh-dot ${freshnessInfo.style}"></i>${d.enabled ? esc(freshnessInfo.text) : 'Disabled'}</span><span>${d.kind === 'plug' ? '15m hold on manual control' : 'Drag to arrange'}</span></div>
    </article>`;
  }).join('') : '<div class="empty-state"><h3>Your Home starts here.</h3>Add your hub, then sensors and smart plugs. Each device gets its own card.<br><button class="primary" data-action="new-device">＋ Add your first device</button></div>';
  $('hubs-list').innerHTML = home.document.hubs.map(h => `<div class="hub-row"><div><strong>${esc(h.name)}</strong><p>${esc(h.address || 'Connection details needed')} · ${h.enabled ? 'Enabled' : 'Disabled'} · ${devices.filter(d => d.hub_id === h.id).length} devices</p></div><button data-action="edit-hub" data-id="${esc(h.id)}">Configure</button></div>`).join('') || '<div class="empty-state">Using Zigbee? Add the G08Pro hub once, then select it for each paired device.</div>';
}
function showView(view) {
  home.view = view;
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  ['devices','automations','activity'].forEach(v => $('view-'+v).hidden = v !== view);
  if (view === 'automations') renderRules();
  if (view === 'devices') renderDevices();
  if (view === 'activity') renderActivity();
}
function field(key, label, value = '', type = 'text', extra = '') {
  return `<label class="field">${label}<input name="${key}" aria-label="${esc(label)}" type="${type}" value="${esc(value)}" ${extra}></label>`;
}
function options(items, value) {
  return items.map(([id,label]) => `<option value="${esc(id)}" ${String(id) === String(value) ? 'selected' : ''}>${esc(label)}</option>`).join('');
}
function selectField(key, label, items, value) {
  return `<label class="field">${label}<select name="${key}" aria-label="${esc(label)}">${options(items,value)}</select></label>`;
}
function openDevice(kind, id) {
  const collection = kind === 'hub' ? 'hubs' : 'devices';
  const current = home.document[collection].find(d => d.id === id);
  const defaults = {
    id:uid(kind), name:'', enabled:false, kind:'plug', transport:'zigbee', hub_id:home.document.hubs[0]?.id || '',
    address:'', device_id:'', local_key:'', version:3.4, node_id:'', temperature_dp:'103', humidity_dp:'101', power_dp:'1', temperature_divisor:1, humidity_divisor:1, temperature_unit:'C'
  };
  home.dialog = {collection, existing:!!current, value:structuredClone({...defaults, ...current})};
  renderDeviceForm();
  $('device-dialog').showModal();
}
function readDeviceForm() {
  const value = home.dialog.value;
  new FormData($('device-form')).forEach((entry,key) => {
    const input = $('device-form').elements.namedItem(key);
    value[key] = input.type === 'number' || key === 'version' ? Number(entry) : entry;
  });
  value.enabled = $('device-form').elements.namedItem('enabled').checked;
}
function renderDeviceForm() {
  const d = home.dialog.value;
  const hub = home.dialog.collection === 'hubs';
  $('device-dialog-title').textContent = (home.dialog.existing ? 'Configure ' : 'Add ') + (hub ? 'hub' : 'device');
  $('device-form-error').textContent = '';
  let fields = field('name','Name',d.name,'text','required maxlength="120"') + `<label class="toggle-label"><input name="enabled" type="checkbox" ${d.enabled ? 'checked' : ''}> Enable this ${hub ? 'hub' : 'device'}</label>`;
  if (!hub) fields += '<div class="fields-row">' + selectField('kind','Device type',[['plug','Smart plug'],['sensor','Temperature / humidity sensor']],d.kind) + selectField('transport','Connection',[['zigbee','Zigbee through hub'],['wifi','Wi-Fi Tuya device']],d.transport) + '</div>';
  if (hub || d.transport === 'wifi') {
    fields += field('address','LAN IP address',d.address,'text','placeholder="192.168.88.…"');
    fields += field('device_id','Tuya device ID',d.device_id);
    fields += field('local_key',d.local_key === '••••' ? 'Local key · saved key is preserved' : 'Local key',d.local_key,'password','autocomplete="new-password"');
    fields += selectField('version','Tuya protocol',[[3.3,'3.3'],[3.4,'3.4'],[3.5,'3.5']],d.version);
  } else {
    fields += selectField('hub_id','Hub',[['','Choose a hub'],...home.document.hubs.map(h => [h.id,h.name])],d.hub_id);
    fields += field('device_id','Sensor / plug Tuya device ID',d.device_id);
    fields += field('node_id','Zigbee node ID',d.node_id);
    fields += '<p class="dialog-hint">Pair the device to your hub in Smart Life first. Enter the IDs assigned to this device.</p>';
  }
  if (!hub) {
    fields += '<details><summary>Data point mapping</summary><p class="dialog-hint">Use the mapping for this model. Sensor defaults match the TH02 Z3-P3Z. Plug power must be a boolean switch data point.</p>';
    if (d.kind === 'plug') fields += field('power_dp','Power switch data point',d.power_dp);
    else fields += '<div class="fields-row">'+field('temperature_dp','Temperature DP',d.temperature_dp)+field('temperature_divisor','Temperature divisor',d.temperature_divisor,'number','step="any" min="0.001"')+'</div><div class="fields-row">'+field('humidity_dp','Humidity DP',d.humidity_dp)+field('humidity_divisor','Humidity divisor',d.humidity_divisor,'number','step="any" min="0.001"')+'</div>'+selectField('temperature_unit','Raw temperature unit',[['C','Celsius'],['F','Fahrenheit']],d.temperature_unit);
    fields += '</details>';
  }
  if (home.dialog.existing) fields += `<button type="button" class="danger" data-action="delete-dialog-item">Remove ${hub ? 'hub' : 'device'}</button>`;
  $('device-fields').innerHTML = fields;
}
function metricOptions(source) {
  if (source === 'solar') return [['soc','Battery SOC (%)'],['input_power_w','Solar production (W)'],['grid_power_w','Grid export (W)']];
  if (source === 'clock') return [['time','Local time window']];
  const device = home.document.devices.find(d => d.id === source);
  return device?.kind === 'plug' ? [['power','Power state']] : [['temperature_f','Temperature (°F)'],['humidity_pct','Humidity (%)']];
}
function newCondition(kind = 'temperature') {
  const sensor = home.document.devices.find(d => d.kind === 'sensor');
  const plug = home.document.devices.find(d => d.kind === 'plug');
  if (kind === 'power' && !plug) { notice('Add a smart plug first.',true); return null; }
  if ((kind === 'temperature' || kind === 'humidity') && !sensor) { notice('Add a sensor first.',true); return null; }
  const source = kind === 'time' ? 'clock' : kind === 'solar' ? 'solar' : kind === 'power' ? plug.id : sensor.id;
  const metric = {time:'time',solar:'soc',power:'power',temperature:'temperature_f',humidity:'humidity_pct'}[kind];
  return {id:uid('condition'),source,metric,operator:kind === 'time' ? 'between' : kind === 'power' ? 'is_on' : 'below',value:kind === 'time' ? '08:00' : kind === 'humidity' ? 60 : kind === 'solar' ? 85 : 40,end:'18:00'};
}
function newAction(kind) {
  if (kind === 'plug') {
    const plug = home.document.devices.find(d => d.kind === 'plug');
    if (!plug) { notice('Add a smart plug first.',true); return null; }
    return {id:uid('action'),type:'plug',device_id:plug.id,on:true};
  }
  return {id:uid('action'),type:'telegram',message:'Home automation conditions matched.'};
}
function blockControls(id, kind, index, length) {
  return `<div class="block-controls"><span class="grip" data-handle="true" title="Drag to reorder" aria-hidden="true">⠿</span><button data-action="move-block" data-id="${id}" data-kind="${kind}" data-direction="-1" ${index===0 ? 'disabled' : ''} aria-label="Move block earlier">↑</button><button data-action="move-block" data-id="${id}" data-kind="${kind}" data-direction="1" ${index===length-1 ? 'disabled' : ''} aria-label="Move block later">↓</button><button data-action="remove-block" data-id="${id}" data-kind="${kind}" aria-label="Remove block">×</button></div>`;
}
function conditionBlock(c, index, length) {
  const sources = [...home.document.devices.map(d => [d.id,d.name]),['solar','Solar system'],['clock','Clock']];
  const ops = c.metric === 'power' ? [['is_on','Is ON'],['is_off','Is OFF']] : c.metric === 'time' ? [['between','Between']] : [['below','Falls below'],['above','Rises above']];
  const attrs = `data-block="${esc(c.id)}" data-kind="conditions"`;
  return `<div class="rule-block" data-drag="conditions" data-id="${esc(c.id)}">${blockControls(c.id,'conditions',index,length)}<select aria-label="Condition device" ${attrs} data-key="source">${options(sources,c.source)}</select><select aria-label="Reading" ${attrs} data-key="metric">${options(metricOptions(c.source),c.metric)}</select><select aria-label="Comparison" ${attrs} data-key="operator">${options(ops,c.operator)}</select>${c.metric !== 'power' ? `<input aria-label="${c.metric === 'time' ? 'Start time' : 'Threshold'}" ${attrs} data-key="value" type="${c.metric === 'time' ? 'time' : 'number'}" step="any" value="${esc(c.value)}">` : ''}${c.metric === 'time' ? `<input aria-label="End time" ${attrs} data-key="end" type="time" value="${esc(c.end)}">` : ''}</div>`;
}
function actionBlock(a, index, length) {
  const attrs = `data-block="${esc(a.id)}" data-kind="actions"`;
  return `<div class="rule-block action" data-drag="actions" data-id="${esc(a.id)}">${blockControls(a.id,'actions',index,length)}<strong>${a.type === 'telegram' ? 'Telegram message' : 'Switch plug'}</strong>${a.type === 'telegram' ? `<textarea aria-label="Telegram message" ${attrs} data-key="message" maxlength="1000">${esc(a.message)}</textarea>` : `<select aria-label="Target plug" ${attrs} data-key="device_id">${options(home.document.devices.filter(d => d.kind === 'plug').map(d => [d.id,d.name]),a.device_id)}</select><select aria-label="Plug action" ${attrs} data-key="on">${options([['true','Turn ON'],['false','Turn OFF']],String(a.on))}</select>`}</div>`;
}
function renderRulesList() {
  $('rule-count').textContent = home.document.rules.length;
  $('rules-list').innerHTML = home.document.rules.map(r => `<button class="rule-option ${r.id === home.selected ? 'selected' : ''}" data-action="select-rule" data-id="${esc(r.id)}"><strong>${esc(r.name)}</strong><small>${r.enabled ? 'ENABLED' : 'PAUSED'} · ${r.conditions.length} conditions · ${r.actions.length} actions</small></button>`).join('') || '<p class="hint">Your saved automations will appear here.</p>';
}
function renderRules() {
  if (!selectedRule()) home.selected = home.document.rules[0]?.id;
  renderRulesList();
  const r = selectedRule();
  if (!r) { $('rule-editor').innerHTML = '<div class="empty-state"><h3>Build your first automation.</h3>For example, turn a plug on when a sensor gets cold, or send a Telegram message when humidity rises.<br><button class="primary" data-action="new-rule">＋ New automation</button></div>'; return; }
  $('rule-editor').innerHTML = `<div class="rule-title"><input aria-label="Automation name" data-rule-key="name" value="${esc(r.name)}" maxlength="120"><label class="toggle-label"><input type="checkbox" data-rule-key="enabled" ${r.enabled ? 'checked' : ''}>Enabled</label></div><p class="rule-intro">Drag blocks below, or click them to add. Rules run after saving. The first fresh reading sets the baseline; a later crossing runs the actions.</p>
    <div class="builder-palette"><span>CONDITIONS</span>${[['temperature','Temperature'],['humidity','Humidity'],['power','Plug state'],['solar','Solar & battery'],['time','Time window']].map(([id,label]) => `<button class="palette-block" data-handle="true" data-palette="${id}" data-kind="conditions" data-action="add-palette">＋ ${label}</button>`).join('')}<span>ACTIONS</span>${[['plug','Switch plug'],['telegram','Telegram']].map(([id,label]) => `<button class="palette-block" data-handle="true" data-palette="${id}" data-kind="actions" data-action="add-palette">＋ ${label}</button>`).join('')}</div>
    <div class="flow-section drop-zone" data-drop="conditions"><div class="flow-heading"><span class="step-title">WHEN</span><label class="toggle-label">Match <select aria-label="Match conditions" data-rule-key="match">${options([['all','all conditions'],['any','any condition']],r.match)}</select></label></div>${r.conditions.map((c,i) => conditionBlock(c,i,r.conditions.length)).join('')}<button class="add-block" data-action="add-condition">＋ Add condition</button></div>
    <div class="flow-arrow" aria-hidden="true">↓</div>
    <div class="flow-section drop-zone" data-drop="actions"><div class="flow-heading"><span class="step-title">THEN · ACTIONS</span></div>${r.actions.map((a,i) => actionBlock(a,i,r.actions.length)).join('')}<button class="add-block" data-action="add-action">＋ Add action</button></div>
    <div class="rule-options"><label>Conditions must hold for (seconds)<input type="number" data-rule-key="for_seconds" min="0" max="86400" value="${r.for_seconds}"></label><label>Minimum time between runs (seconds)<input type="number" data-rule-key="cooldown_seconds" min="0" max="86400" value="${r.cooldown_seconds}"></label></div>
    <label class="field">Workspace timezone<input data-workspace-key="timezone" value="${esc(home.document.timezone || 'America/Detroit')}" placeholder="America/Detroit"></label><p class="hint">Stale or unavailable readings pause a rule. Manual plug controls hold off automatic switching for 15 minutes. Time windows use this timezone, including daylight saving changes.</p><div class="rule-footer"><button data-action="preview-rule">Check saved conditions</button><button class="danger" data-action="delete-rule">Remove automation</button></div><div id="preview-result" class="preview-result" role="status"></div>`;
}
function renderActivity() {
  $('activity-list').innerHTML = home.events.length ? home.events.map(e => `<div class="activity-event"><span class="event-kind ${e.kind === 'error' ? 'error' : ''}">${esc(e.kind)}</span><div><strong>${esc(e.name)}</strong><p>${esc(e.detail)}</p><time>${esc(new Date(e.ts).toLocaleString())}</time></div></div>`).join('') : '<div class="empty-state">Activity will appear when a rule crosses a limit or a plug is switched.</div>';
}
function move(items, id, direction) {
  const from = items.findIndex(d => d.id === id);
  const to = from + Number(direction);
  if (from < 0 || to < 0 || to >= items.length) return;
  [items[from],items[to]] = [items[to],items[from]];
  changed();
}
function addBlock(kind, type) {
  const r = selectedRule();
  if (!r) return;
  const block = kind === 'conditions' ? newCondition(type) : newAction(type);
  if (!block) return;
  r[kind].push(block);
  changed(); renderRules();
}
async function refresh() {
  try {
    const payload = await api('');
    home.readings = payload.devices;
    home.events = payload.events;
    if (!home.dirty && !home.dialog) {
      const revisionChanged = !home.document || home.document.revision !== payload.document.revision;
      home.document = payload.document;
      if (revisionChanged && home.view === 'automations') renderRules();
      $('save-state').textContent = 'All changes saved';
    }
    $('rule-count').textContent = home.document.rules.length;
    if (!home.drag && home.view === 'devices') renderDevices();
    if (home.view === 'activity') renderActivity();
  } catch (error) { notice(error.message,true); }
}
async function handleAction(button) {
  const action = button.dataset.action;
  const id = button.dataset.id;
  if (action === 'new-device' || action === 'new-hub') return openDevice(action === 'new-hub' ? 'hub' : 'device');
  if (action === 'edit-device' || action === 'edit-hub') return openDevice(action === 'edit-hub' ? 'hub' : 'device',id);
  if (action === 'close-dialog') { $('device-dialog').close(); home.dialog = null; return; }
  if (action === 'delete-dialog-item') {
    if (!confirm('Remove this item? Remove its automation references before saving.')) return;
    home.document[home.dialog.collection] = home.document[home.dialog.collection].filter(d => d.id !== home.dialog.value.id);
    home.dialog = null; $('device-dialog').close(); changed(); renderDevices(); return;
  }
  if (action === 'move-device') { move(home.document.devices,id,button.dataset.direction); renderDevices(); return; }
  if (action === 'power') { await post(`/devices/${encodeURIComponent(id)}/power`,{on:button.dataset.on === 'true',hold_minutes:15}); notice('Command queued. Waiting for the plug to confirm.'); await refresh(); return; }
  if (action === 'resume') { await post(`/devices/${encodeURIComponent(id)}/resume`,{}); await refresh(); return; }
  if (action === 'new-rule') {
    const condition = newCondition(home.document.devices.some(d => d.kind === 'sensor') ? 'temperature' : 'solar');
    const rule = {id:uid('rule'),name:'New automation',enabled:false,match:'all',conditions:[condition],actions:[newAction('telegram')],for_seconds:0,cooldown_seconds:60};
    home.document.rules.push(rule); home.selected = rule.id; changed(); showView('automations'); return;
  }
  if (action === 'select-rule') { home.selected = id; renderRules(); return; }
  if (action === 'delete-rule') {
    if (!confirm('Remove this automation?')) return;
    home.document.rules = home.document.rules.filter(r => r.id !== home.selected); changed(); renderRules(); return;
  }
  if (action === 'add-palette') return addBlock(button.dataset.kind,button.dataset.palette);
  if (action === 'add-condition') return addBlock('conditions','solar');
  if (action === 'add-action') return addBlock('actions','telegram');
  if (action === 'remove-block') { const r=selectedRule(); r[button.dataset.kind]=r[button.dataset.kind].filter(b => b.id !== id); changed(); renderRules(); return; }
  if (action === 'move-block') { move(selectedRule()[button.dataset.kind],id,button.dataset.direction); renderRules(); return; }
  if (action === 'preview-rule') {
    if (home.dirty) { notice('Save the workspace before checking the conditions.',true); return; }
    const result = await api('/preview/'+encodeURIComponent(home.selected));
    $('preview-result').textContent = result.result === null ? 'Waiting for fresh readings from every required source.' : result.result ? 'The saved conditions currently match. This check runs no actions.' : 'The saved conditions currently do not match.';
  }
  if (action === 'refresh') await refresh();
}
document.addEventListener('click', event => {
  if (performance.now() < dragClickUntil && event.target.closest('[data-handle]')) {event.preventDefault(); return;}
  const tab=event.target.closest('[data-view]');
  if (tab) { showView(tab.dataset.view); return; }
  const button=event.target.closest('[data-action]');
  if (button && home.document) handleAction(button).catch(error => notice(error.message,true));
});
$('save-workspace').addEventListener('click',async () => {
  $('save-workspace').disabled=true;
  try {
    const payload = await post('',home.document);
    home.document=payload.document; home.dirty=false;
    $('save-state').textContent='All changes saved';
    notice('Workspace saved. Device connections and automations are updating.');
    renderDevices(); if(home.view === 'automations') renderRules();
  } catch(error) { notice(error.message,true); $('save-workspace').disabled=false; }
});
$('device-form').addEventListener('change',event => {
  if (['kind','transport'].includes(event.target.name)) { readDeviceForm(); renderDeviceForm(); }
});
$('device-form').addEventListener('submit',event => {
  event.preventDefault(); readDeviceForm();
  const {collection,value,existing}=home.dialog;
  if (existing) home.document[collection]=home.document[collection].map(d => d.id === value.id ? value : d);
  else home.document[collection].push(value);
  changed(); home.dialog=null; $('device-dialog').close(); renderDevices();
});
$('device-dialog').addEventListener('cancel',() => {home.dialog=null;});
function updateRuleInput(event) {
  const el=event.target;
  if (el.dataset.workspaceKey) {home.document[el.dataset.workspaceKey] = el.value; changed();}
  if (el.dataset.ruleKey) {
    const key=el.dataset.ruleKey;
    selectedRule()[key]=el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value;
    changed(); renderRulesList();
  }
  if (!el.dataset.block) return;
  const block=selectedRule()[el.dataset.kind].find(b => b.id === el.dataset.block);
  const key=el.dataset.key;
  block[key]=key === 'on' ? el.value === 'true' : el.type === 'number' ? Number(el.value) : el.value;
  if (key === 'source') { block.metric=metricOptions(block.source)[0][0]; }
  if (key === 'source' || key === 'metric') {
    block.operator=block.metric === 'power' ? 'is_on' : block.metric === 'time' ? 'between' : 'below';
    block.value=block.metric === 'time' ? '08:00' : 40;
    block.end='18:00';
  }
  changed();
  if (key === 'source' || key === 'metric') renderRules();

}
document.addEventListener('input', event => {
  if (event.target.tagName !== 'SELECT' && event.target.type !== 'checkbox') updateRuleInput(event);
});
document.addEventListener('change', event => {
  if (event.target.tagName === 'SELECT' || event.target.type === 'checkbox') updateRuleInput(event);
});
// Pointer-based dragging supports mouse and touch without native drag-event timing.
let pointerDrag = null;
let dragClickUntil = 0;
function dropDestination(target) {
  const zone = target.closest('[data-drop]');
  const item = target.closest('[data-drag]');
  if (home.drag?.palette && zone?.dataset.drop === home.drag.kind) return zone;
  return item?.dataset.drag === home.drag?.kind ? item : null;
}
function finishDrag(target) {
  const zone = target?.closest('[data-drop]');
  const item = target?.closest('[data-drag]');
  if (zone?.dataset.drop === home.drag.kind && home.drag.palette) addBlock(home.drag.kind, home.drag.palette);
  else if (item?.dataset.drag === home.drag.kind && item.dataset.id !== home.drag.id) {
    const items = home.drag.kind === 'device' ? home.document.devices : selectedRule()[home.drag.kind];
    const from = items.findIndex(d => d.id === home.drag.id);
    const to = items.findIndex(d => d.id === item.dataset.id);
    if (from >= 0 && to >= 0) { items.splice(to, 0, items.splice(from, 1)[0]); changed(); }
  }
}
function clearDrag() {
  pointerDrag = null;
  home.drag = null;
  document.querySelectorAll('.dragging,.drag-over').forEach(e => e.classList.remove('dragging','drag-over'));
}
document.addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  const handle = event.target.closest('[data-handle]');
  if (!handle) return;
  const palette = handle.closest('[data-palette]');
  const item = handle.closest('[data-drag]');
  const payload = palette ? {palette:palette.dataset.palette, kind:palette.dataset.kind} : {kind:item.dataset.drag, id:item.dataset.id};
  pointerDrag = {handle, payload, x:event.clientX, y:event.clientY, started:false};
  handle.setPointerCapture(event.pointerId);
});
document.addEventListener('pointermove', event => {
  if (!pointerDrag) return;
  if (!pointerDrag.started && Math.hypot(event.clientX-pointerDrag.x, event.clientY-pointerDrag.y) < 6) return;
  pointerDrag.started = true;
  home.drag = pointerDrag.payload;
  pointerDrag.handle.classList.add('dragging');
  const target = document.elementFromPoint(event.clientX, event.clientY);
  document.querySelectorAll('.drag-over').forEach(e => e.classList.remove('drag-over'));
  const destination = target && dropDestination(target);
  if (destination) destination.classList.add('drag-over');
  event.preventDefault();
});
document.addEventListener('pointerup', event => {
  if (!pointerDrag) return;
  const started = pointerDrag.started;
  if (started) {
    const target = document.elementFromPoint(event.clientX, event.clientY);
    if (target) finishDrag(target);
    // The browser may synthesize a click on the drag source after release.
    dragClickUntil = performance.now() + 500;
  }
  clearDrag();
  if (started) { if(home.view === 'devices') renderDevices(); else renderRules(); }
});
document.addEventListener('pointercancel', clearDrag);
document.addEventListener('keydown', event => {if(event.key === 'Escape') clearDrag();});
window.addEventListener('beforeunload',event => {if(home.dirty){event.preventDefault();event.returnValue='';}});
refresh();
setInterval(refresh,5000);
