/* Account credentials and local keys stay on the server. */
'use strict';
const discovery = {status:null, timer:null, signature:null, selected:new Set(), edits:{}, working:false};
function discoveryError(message) { $('discovery-error').textContent = message; }
function discoverySelection() {
  const count = discovery.selected.size;
  $('discovery-selection').textContent = count ? `${count} selected · required hubs are included` : 'Choose devices to add';
  document.querySelector('[data-action="discovery-import"]').disabled = !count || discovery.status?.busy || discovery.working;
}
function renderDiscovery(status) {
  discovery.status = status;
  $('discovery-account').textContent = status.connected ? (status.remembered ? 'Connected · remembered' : 'Connected · this session') : 'Not connected';
  $('discovery-login').hidden = status.connected;
  $('discovery-connected').hidden = !status.connected;
  $('discovery-qr').hidden = !status.qr_image;
  if (status.qr_image) $('discovery-qr-image').src = status.qr_image;
  else $('discovery-qr-image').removeAttribute('src');
  $('discovery-lan').textContent = status.lan.count ? `${status.lan.count} Tuya endpoints reachable: ${status.lan.reachable.join(', ')}. Zigbee children are found through the account's hub list.` : 'No Tuya endpoints found yet. Check the LAN range; Docker may not receive broadcasts.';
  const labels = {idle:'Ready to scan',scanning:'Checking network…',network_ready:'Network check complete',signing_in:'Creating QR code…',awaiting_signin:'Waiting for phone approval',loading_account:'Checking approval and loading devices…',loading_devices:'Loading account devices…',ready:'Ready to import',expired:'Discovery expired · load devices again'};
  $('discovery-progress').textContent = status.error ? 'Needs attention' : labels[status.phase] || '';
  if (status.error) discoveryError(status.error);
  $('discovery-warnings').textContent = status.warnings.join(' ');
  $('discovery-dialog').querySelectorAll('[data-action]').forEach(b => {
    if (b.dataset.action !== 'discovery-close' && b.dataset.action !== 'discovery-import') b.disabled = status.busy;
  });
  const signature = JSON.stringify([status.batch, status.candidates]);
  if (signature !== discovery.signature) {
    discovery.signature = signature;
    discovery.selected.clear(); discovery.edits = {};
    const hubs = [...home.document.hubs.map(h => [h.device_id,h.name]), ...status.candidates.filter(c => c.kind === 'hub' && !c.existing).map(c => [c.id,c.name])];
    $('discovery-candidates').innerHTML = status.candidates.length ? status.candidates.map(c => {
      const parent = hubs.find(h => h[0] === c.hub_device_id)?.[1];
      let details = c.existing ? 'Already configured · existing settings will be preserved' : !c.supported ? 'Local mapping unavailable · use manual device setup' : c.kind === 'hub' ? 'Tuya hub' : c.transport === 'zigbee' ? `Zigbee ${c.kind} · ${parent || 'Choose a hub'}` : `Wi-Fi ${c.kind}`;
      if (c.supported && !c.existing) {
        if (c.transport === 'zigbee') details += c.node_id ? ' · node ID found' : ' · missing node ID; imports disabled';
        else details += c.on_lan ? ' · reachable on LAN' : ' · LAN address needs confirmation';
        if (c.transport === 'wifi' && !c.has_key) details += ' · missing local key; imports disabled';
      }
      if (c.kind === 'sensor') details += ` · temperature DP ${c.temperature_dp} ÷ ${c.temperature_divisor} (${c.temperature_unit}), humidity DP ${c.humidity_dp} ÷ ${c.humidity_divisor}`;
      return `<div class="discovery-candidate" data-candidate="${esc(c.id)}"><label><input type="checkbox" data-import-id="${esc(c.id)}" ${c.existing || !c.supported ? 'disabled' : ''}><span><strong>${esc(c.name)}</strong><small class="${c.existing ? 'existing' : ''}">${esc(details)}</small></span></label>${!c.existing && c.supported && c.transport === 'wifi' ? `<label class="field">LAN IP address<input data-discovery-address="${esc(c.id)}" value="${esc(c.address)}" placeholder="192.168.88.…" list="discovery-addresses"></label>` : ''}${!c.existing && c.supported && c.transport === 'wifi' ? `<label class="field">Tuya protocol<select data-discovery-version="${esc(c.id)}">${options([['','Choose protocol'],[3.3,'3.3'],[3.4,'3.4'],[3.5,'3.5']],c.version_known ? c.version : '')}</select></label>` : ''}${!c.existing && c.supported && c.transport === 'zigbee' ? `<label class="field">Paired hub<select data-discovery-hub="${esc(c.id)}">${options([['','Choose a hub'],...hubs],c.hub_device_id)}</select></label>` : ''}</div>`;
    }).join('') + `<datalist id="discovery-addresses">${status.lan.reachable.map(ip => `<option value="${esc(ip)}"></option>`).join('')}</datalist><p class="hint">Your account may include other networks; confirm each hub’s LAN address. Missing connection details create disabled devices for you to finish in Configure. Importing preserves existing devices and automations. No plugs are switched during discovery or import.</p>` : '<p class="hint">No account devices loaded. Connect Tuya, then load your devices.</p>';
  }
  discoverySelection();
}
async function discoveryPoll() {
  if (!$('discovery-dialog').open) return;
  try { renderDiscovery(await api('/discovery')); } catch(error) { discoveryError(error.message); }
}
async function discoveryAction(button) {
  const action = button.dataset.action;
  if (action === 'discover') {
    if (home.dirty) { notice('Save the workspace before discovering devices.',true); return; }
    const address = home.document.hubs.find(h => h.address)?.address || location.hostname;
    $('discovery-network').value ||= /^\d+\.\d+\.\d+\.\d+$/.test(address) && !address.startsWith('127.') ? address.split('.').slice(0,3).join('.')+'.0/24' : '';
    discoveryError(''); $('discovery-dialog').showModal();
    await discoveryPoll();
    clearInterval(discovery.timer); discovery.timer = setInterval(discoveryPoll,1500);
    return;
  }
  if (action === 'discovery-close') { $('discovery-dialog').close(); clearInterval(discovery.timer); return; }
  if (discovery.working) return;
  discovery.working = true; button.disabled = true; discoveryError('');
  try {
    let status;
    if (action === 'discovery-scan') status = await post('/discovery/scan',{network:$('discovery-network').value.trim()});
    if (action === 'discovery-signin') status = await post('/discovery/signin',{user_code:$('discovery-code').value.trim(),remember:$('discovery-remember').checked});
    if (action === 'discovery-confirm') status = await post('/discovery/confirm',{});
    if (action === 'discovery-sync') status = await post('/discovery/sync',{});
    if (action === 'discovery-disconnect') status = await post('/discovery/disconnect',{});
    if (action === 'discovery-measurements') {
      if (home.dirty) throw new Error('Save workspace changes first.');
      const result=await post('/discovery/measurements',{batch:discovery.status.batch,revision:home.document.revision});
      home.document=result.document; renderDevices();
      notice(`Updated measurement mappings for ${result.updated} devices. Existing connection details and manual mappings were preserved.`);
      return;
    }
    if (action === 'discovery-import') {
      if (home.dirty) throw new Error('Save workspace changes before importing.');
      const ids = new Set(discovery.selected);
      for (const id of discovery.selected) {
        const child = discovery.status.candidates.find(c => c.id === id);
        const parent = discovery.edits[id]?.hub_device_id || child.hub_device_id;
        if (child.transport === 'zigbee' && discovery.status.candidates.some(c => c.id === parent && c.kind === 'hub' && !c.existing)) ids.add(parent);
      }
      const items = [...ids].map(id => ({id,...discovery.edits[id]}));
      const result = await post('/discovery/import',{items,batch:discovery.status.batch,revision:home.document.revision});
      home.document = result.document;
      renderDevices(); $('discovery-dialog').close(); clearInterval(discovery.timer);
      notice(`Imported ${result.imported} devices / hubs.${result.disabled ? ` ${result.disabled} need connection details in Configure before enabling.` : ' Local connections are updating.'}`);
      return;
    }
    if (status) renderDiscovery(status);
  } catch(error) { discoveryError(error.message); }
  finally { discovery.working = false; button.disabled = false; discoverySelection(); }
}
$('discovery-dialog').addEventListener('cancel',() => clearInterval(discovery.timer));
$('discovery-dialog').addEventListener('change',event => {
  const el = event.target;
  if (el.dataset.importId) {
    if (el.checked) discovery.selected.add(el.dataset.importId);
    else discovery.selected.delete(el.dataset.importId);
    discoverySelection();
  }
  if (el.dataset.discoveryVersion) {
    const id = el.dataset.discoveryVersion;
    discovery.edits[id] = {...discovery.edits[id]};
    if (el.value) discovery.edits[id].version = Number(el.value);
    else delete discovery.edits[id].version;
  }
  if (el.dataset.discoveryHub) {
    const id = el.dataset.discoveryHub;
    discovery.edits[id] = {...discovery.edits[id],hub_device_id:el.value};
  }
});
$('discovery-dialog').addEventListener('input',event => {
  const id = event.target.dataset.discoveryAddress;
  if (id) discovery.edits[id] = {...discovery.edits[id],address:event.target.value.trim()};
});
