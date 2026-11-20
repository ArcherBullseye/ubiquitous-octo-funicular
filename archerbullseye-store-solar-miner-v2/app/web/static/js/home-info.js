/* Read-only device diagnostics and optional measurement mappings. */
'use strict';
const deviceInfo = {id:null,timer:null,loading:false};
async function refreshDeviceInfo() {
  if (!$('device-info-dialog').open || deviceInfo.loading) return;
  deviceInfo.loading=true;
  const id=deviceInfo.id;
  try {
    const data=await api(`/devices/${encodeURIComponent(id)}/info`);
    if (deviceInfo.id !== id || !$('device-info-dialog').open) return;
    $('device-info-title').textContent=data.name;
    const row=data.readings;
    $('device-info-status').textContent=row.status === 'connected' ? 'Connected · '+ageLabel(row.last_report_at || Object.values(row.updated_at || {})[0]) : (row.availability_reason || 'Waiting / offline · last reports shown');
    const configured=home.document.devices.find(device=>device.id === id);
    const base=home.deviceMetrics[configured?.kind] || {};
    const entries=[...Object.entries(base).map(([key,item])=>({id:key,label:item.label,unit:''})),...data.telemetry];
    $('device-info-content').innerHTML=`<table class="telemetry-table"><thead><tr><th>Reading</th><th>Reported value</th><th>Last report</th></tr></thead><tbody>${entries.map(item=>`<tr><td>${esc(item.label)}</td><td>${esc(formatDeviceValue(row[item.id],item.unit))}</td><td>${esc(ageLabel(row.updated_at?.[item.id]))}</td></tr>`).join('')}</tbody></table>${!data.telemetry.length ? '<p class="dialog-hint">No extra measurement mappings are configured. A relay-only plug may not have electrical metering. Load device details in Discover devices to check its capabilities.</p>' : ''}<details><summary>Uninterpreted numeric / boolean reports (${data.raw.length})</summary><p class="dialog-hint">These are raw DP values, without assumed units. Unknown values are never treated as watts or amps.</p><table class="telemetry-table"><thead><tr><th>DP</th><th>Raw value</th><th>Last report</th></tr></thead><tbody>${data.raw.map(item=>`<tr><td>${esc(item.dp)}</td><td>${esc(formatDeviceValue(item.value))}</td><td>${esc(ageLabel(item.received_at))}</td></tr>`).join('')}</tbody></table></details>`;
  } catch(error) { $('device-info-error').textContent=error.message; }
  finally {deviceInfo.loading=false;}
}
async function deviceInfoAction(button) {
  const action=button.dataset.action;
  if (action === 'device-info-close') { $('device-info-dialog').close(); clearInterval(deviceInfo.timer); return; }
  if (action === 'device-info') {
    deviceInfo.id=button.dataset.id; $('device-info-error').textContent='';
    $('device-info-content').textContent='Loading device reports…';
    $('device-info-dialog').showModal(); await refreshDeviceInfo();
    clearInterval(deviceInfo.timer);deviceInfo.timer=setInterval(refreshDeviceInfo,3000);return;
  }
  if (action === 'device-info-refresh') {
    button.disabled=true;$('device-info-error').textContent='';
    try {
      await post(`/devices/${encodeURIComponent(deviceInfo.id)}/refresh`,{});
      $('device-info-status').textContent='Requested a report · waiting for the device…';
    } catch(error) {$('device-info-error').textContent=error.message;}
    finally {button.disabled=false;}
  }
}
$('device-info-dialog').addEventListener('cancel',()=>clearInterval(deviceInfo.timer));
