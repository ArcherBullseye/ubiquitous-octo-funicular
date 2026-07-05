/* Solar Miner v2 — dashboard logic */

// ── Helpers ────────────────────────────────────────────────────
function el(id) { return document.getElementById(id); }

function fmt(w) {
  if (w === null || w === undefined || isNaN(w)) return '—';
  const abs = Math.abs(w);
  if (abs >= 1000) return (w / 1000).toFixed(2) + ' kW';
  return Math.round(w) + ' W';
}

function direction(w, posLabel, negLabel) {
  if (w > 50) return posLabel;
  if (w < -50) return negLabel;
  return 'idle';
}

function socColor(soc) {
  if (soc > 60) return 'var(--green)';
  if (soc > 15) return 'var(--amber)';
  return 'var(--red)';
}

function timeSince(isoStr) {
  if (!isoStr) return '—';
  const sec = Math.round((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (sec < 5) return 'just now';
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.round(sec / 60) + 'm ago';
  return Math.round(sec / 3600) + 'h ago';
}

function isFresh(isoStr) {
  if (!isoStr) return false;
  return (Date.now() - new Date(isoStr).getTime()) < 120000;
}

let dehumThresholdW = 500;

// ── Energy flow diagram ────────────────────────────────────────
// Paths are drawn node→hub for solar/battery/grid and hub→node for
// home/miners; `rev` flips the dash animation to match the real direction.
function setFlow(id, active, rev) {
  const p = el(id);
  if (!p) return;
  p.classList.toggle('on', !!active);
  p.classList.toggle('rev', !!rev);
}

function updateFlow(r, s, minersKw) {
  const MIN = 25; // W below which a leg reads as idle
  const pv = r.input_power_w || 0;
  const bw = r.battery_power_w || 0;   // + charging, − discharging
  const gw = r.grid_power_w || 0;      // + exporting, − importing
  const lw = r.load_power_w || 0;
  const mw = r.backup_power_w || 0;

  el('fv-solar').textContent = fmt(pv);
  const pvPeakW = (s.pv_peak_kw || 0) * 1000;
  el('fs-solar').textContent = pvPeakW > 0 ? Math.min(100, pv / pvPeakW * 100).toFixed(0) + '% of peak' : '';
  setFlow('fl-solar', pv > MIN, false);              // solar → hub

  el('fv-batt').textContent = fmt(Math.abs(bw));
  const bdir = direction(bw, 'charging', 'discharging');
  el('fs-batt').textContent = bdir;
  setFlow('fl-batt', Math.abs(bw) > MIN, bw > 0);    // fwd batt→hub (discharge); rev = charging

  el('fv-grid').textContent = fmt(Math.abs(gw));
  const gdir = direction(gw, 'exporting', 'importing');
  el('fs-grid').textContent = gdir;
  setFlow('fl-grid', Math.abs(gw) > MIN, gw > 0);    // fwd grid→hub (import); rev = exporting

  el('fv-home').textContent = fmt(lw);
  setFlow('fl-home', lw > MIN, false);               // hub → home

  el('fv-miner').textContent = fmt(mw);
  el('fs-miner').textContent = minersKw || '';
  setFlow('fl-miner', mw > MIN, false);              // hub → miners
}

// ── SOC ring (battery node in the flow diagram) ────────────────
function updateSocRing(soc) {
  const ring = el('soc-ring');
  const c = 2 * Math.PI * ring.r.baseVal.value;
  ring.setAttribute('stroke-dasharray', String(c));
  ring.style.strokeDashoffset = String(c * (1 - Math.max(0, Math.min(100, soc)) / 100));
  ring.style.stroke = socColor(soc);
  const numEl = el('soc-value');
  numEl.textContent = soc.toFixed(1) + '%';
  numEl.style.fill = socColor(soc);
}

// ── Charts ─────────────────────────────────────────────────────
let socChart = null, dailySatsChart = null, effChart = null;
let historyHours = 12;
const GRID_COLOR = 'rgba(148,163,184,.08)';
const TICK_COLOR = '#8b98ad';

function setHistoryWindow(hours) {
  historyHours = hours;
  document.querySelectorAll('.time-btn').forEach(b =>
    b.classList.toggle('active', Number(b.dataset.hours) === hours));
  el('soc-chart-title').textContent = `SOC & Power (${hours === 168 ? '7d' : hours + 'h'})`;
  if (socChart) { socChart.destroy(); socChart = null; }
  loadHistory();
}
document.querySelectorAll('.time-btn').forEach(b =>
  b.addEventListener('click', () => setHistoryWindow(Number(b.dataset.hours))));

async function loadHistory() {
  try {
    const resp = await fetch(`/api/history?hours=${historyHours}`);
    if (!resp.ok) return;
    const rows = await resp.json();

    const labels = rows.map(r => {
      const d = new Date(r.ts);
      if (historyHours > 24)
        return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    });
    const socVals = rows.map(r => r.soc);
    const pvVals = rows.map(r => (r.input_power_w || 0) / 1000);
    const gridVals = rows.map(r => (r.grid_power_w || 0) / 1000);
    const loadVals = rows.map(r => (r.load_power_w || 0) / 1000);
    const minerVals = rows.map(r => (r.backup_power_w || 0) / 1000);
    const bgColors = rows.map(r => r.miner_running ? 'rgba(52,211,153,0.8)' : 'rgba(251,191,36,0.6)');

    const line = (label, data, color) => ({
      label, data, borderColor: color, backgroundColor: 'transparent',
      borderWidth: 1.6, pointRadius: 0, tension: 0.4,
      cubicInterpolationMode: 'monotone', yAxisID: 'y1',
    });

    if (!socChart) {
      const ctx = el('socChart').getContext('2d');
      const grad = ctx.createLinearGradient(0, 0, 0, 260);
      grad.addColorStop(0, 'rgba(251,191,36,.22)');
      grad.addColorStop(1, 'rgba(251,191,36,0)');
      socChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: 'SOC %', data: socVals,
              borderColor: '#fbbf24', backgroundColor: grad,
              borderWidth: 2.2, pointRadius: 1.5,
              pointBackgroundColor: bgColors, pointBorderColor: bgColors,
              tension: 0.4, cubicInterpolationMode: 'monotone',
              fill: true, yAxisID: 'y',
            },
            line('PV kW', pvVals, '#2dd4bf'),
            line('Grid kW', gridVals, '#60a5fa'),
            line('Load kW', loadVals, '#fb7185'),
            line('Miner kW', minerVals, '#a78bfa'),
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          scales: {
            x: { ticks: { color: TICK_COLOR, maxTicksLimit: 8, font: { size: 10 } }, grid: { color: GRID_COLOR } },
            y: {
              type: 'linear', position: 'left', min: 0, max: 100,
              ticks: { color: '#fbbf24', callback: v => v + '%', font: { size: 10 } },
              grid: { color: GRID_COLOR },
            },
            y1: {
              type: 'linear', position: 'right',
              ticks: { color: TICK_COLOR, callback: v => v.toFixed(1) + ' kW', font: { size: 10 } },
              grid: { drawOnChartArea: false },
            },
          },
          plugins: {
            legend: { display: true, labels: { color: TICK_COLOR, boxWidth: 12, font: { size: 11 } } },
            tooltip: {
              callbacks: {
                label: c => {
                  if (c.dataset.yAxisID === 'y') {
                    const mining = rows[c.dataIndex]?.miner_running ? ' ⛏' : '';
                    return `SOC: ${c.parsed.y.toFixed(1)}%${mining}`;
                  }
                  return `${c.dataset.label}: ${c.parsed.y.toFixed(2)} kW`;
                }
              }
            }
          }
        }
      });
    } else {
      socChart.data.labels = labels;
      socChart.data.datasets[0].data = socVals;
      socChart.data.datasets[0].pointBackgroundColor = bgColors;
      socChart.data.datasets[0].pointBorderColor = bgColors;
      socChart.data.datasets[1].data = pvVals;
      socChart.data.datasets[2].data = gridVals;
      socChart.data.datasets[3].data = loadVals;
      socChart.data.datasets[4].data = minerVals;
      socChart.update('none');
    }
  } catch (e) { console.warn('loadHistory error:', e); }
}

async function loadDailySats() {
  try {
    const resp = await fetch('/api/daily_sats?days=8&include_today=0');
    if (!resp.ok) return;
    const rows = await resp.json();
    if (!rows.length) return;

    const labels = rows.map(r => {
      const d = new Date(r.date + 'T12:00:00');
      return d.toLocaleDateString([], { weekday: 'short', month: 'numeric', day: 'numeric' });
    });
    const vals = rows.map(r => r.sats);

    if (!dailySatsChart) {
      const ctx = el('dailySatsChart').getContext('2d');
      const grad = ctx.createLinearGradient(0, 0, 0, 260);
      grad.addColorStop(0, 'rgba(251,191,36,.85)');
      grad.addColorStop(1, 'rgba(245,158,11,.35)');
      dailySatsChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: 'Sats', data: vals,
            backgroundColor: grad, borderColor: '#fbbf24',
            borderWidth: 1, borderRadius: 5,
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: {
            x: { ticks: { color: TICK_COLOR, font: { size: 10 } }, grid: { color: GRID_COLOR } },
            y: { min: 0, ticks: { color: TICK_COLOR, callback: v => v.toLocaleString(), font: { size: 10 } }, grid: { color: GRID_COLOR } },
          },
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: c => c.parsed.y.toLocaleString() + ' sats' } }
          }
        }
      });
    } else {
      dailySatsChart.data.labels = labels;
      dailySatsChart.data.datasets[0].data = vals;
      dailySatsChart.update('none');
    }
  } catch (e) { console.warn('loadDailySats error:', e); }
}

// ── Manual refresh (click the status dot) ──────────────────────
async function manualRefresh() {
  const dot = el('status-dot');
  if (dot.classList.contains('refreshing')) return;
  dot.classList.add('refreshing');
  let before = null;
  try {
    const r0 = await fetch('/api/status');
    if (r0.ok) before = (await r0.json()).last_updated || null;
  } catch (e) { /* ignore */ }
  try {
    await fetch('/api/refresh', { method: 'POST' });
    // Poll until the control loop finishes its live re-poll (cap ~8s).
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 500));
      try {
        const r = await fetch('/api/status');
        if (r.ok && (await r.json()).last_updated !== before) break;
      } catch (e) { /* keep waiting */ }
    }
    await refresh();
    loadHistory();
    loadDailySats();
  } catch (e) {
    console.warn('manual refresh error:', e);
  } finally {
    dot.classList.remove('refreshing');
  }
}

// ── Main refresh ───────────────────────────────────────────────
async function refresh() {
  try {
    const resp = await fetch('/api/status');
    if (!resp.ok) return;
    const s = await resp.json();

    el('status-dot').classList.toggle('fresh', isFresh(s.last_updated));
    el('last-updated').textContent = 'Updated ' + timeSince(s.last_updated);
    if (s.app_version) {
      el('app-version').textContent = 'v' + s.app_version;
      el('settings-version').textContent = 'v' + s.app_version;
    }

    const banner = el('error-banner');
    if (s.error) {
      banner.textContent = '⚠ ' + s.error;
      banner.classList.add('visible');
    } else {
      banner.classList.remove('visible');
    }

    const r = s.readings;
    if (r) {
      updateSocRing(r.soc);

      // Total live hashrate across miners for the flow diagram sub-label
      let ths = 0;
      for (const k in (s.miners || {})) {
        const m = s.miners[k];
        if (m && m.reachable !== false && m.hashrate_mhs) ths += m.hashrate_mhs / 1e6;
      }
      updateFlow(r, s, ths > 0 ? ths.toFixed(1) + ' TH/s' : '');
    }

    // Miners + ramp
    _ramp = s.ramp || null;
    loadRampLog();
    _renderRampChips(s);
    _renderMiners(s.miners, false);
    el('miner-threshold').textContent = 'SOC ON: ' + (s.effective_soc_on != null ? s.effective_soc_on.toFixed(1) + '%' : '—');

    // Smart start
    const smartBadge = el('smart-badge');
    const holdBtn = el('smart-hold-btn');
    if (s.smart_hold_active) {
      smartBadge.textContent = 'Held';
      smartBadge.className = 'badge badge-amber';
      holdBtn.textContent = 'Resume';
      holdBtn.dataset.held = '1';
      holdBtn.classList.add('held');
    } else {
      smartBadge.textContent = s.smart_start_active ? 'Active' : 'Idle';
      smartBadge.className = 'badge ' + (s.smart_start_active ? 'badge-green' : 'badge-gray');
      holdBtn.textContent = 'Hold';
      holdBtn.dataset.held = '0';
      holdBtn.classList.remove('held');
    }

    const wx = s.weather;
    const sunny = wx ? wx.remaining_sunny_hours : null;
    const sunnyThresh = s.sunny_hours_threshold || 3;
    el('smart-sunny').textContent = sunny != null ? String(sunny) : '—';
    el('smart-sunny-row').classList.toggle('ok', sunny != null && sunny >= sunnyThresh);

    el('smart-threshold').textContent = s.effective_soc_on != null ? s.effective_soc_on.toFixed(1) + '%' : '—';
    const curSoc = (s.readings && s.readings.soc != null) ? s.readings.soc : null;
    el('smart-threshold-row').classList.toggle('ok',
      curSoc != null && s.effective_soc_on != null && curSoc >= s.effective_soc_on);

    const curPv = (s.readings && s.readings.input_power_w != null) ? s.readings.input_power_w : null;
    const minPv = s.smart_min_pv_w || 1000;
    el('smart-pv-gate').textContent =
      (curPv != null ? (curPv / 1000).toFixed(1) : '—') + ' / ' + (minPv / 1000).toFixed(1) + ' kW';
    el('smart-pv-row').classList.toggle('ok', curPv != null && curPv >= minPv);

    const eodEl = el('eod-projection');
    if (s.eod_soc_target_enabled && s.eod_projected_with != null) {
      const target = s.eod_soc_target || 0;
      const color = s.eod_protecting ? 'var(--amber)' : (s.eod_projected_with >= target ? 'var(--green)' : 'var(--red)');
      const label = s.eod_protecting ? '⚠ EOD protect active' : 'EOD est';
      eodEl.style.display = 'block';
      eodEl.style.color = color;
      eodEl.innerHTML = `${label}: <b>${s.eod_projected_with.toFixed(0)}%</b> w/ miner`;
      if (s.eod_projected_without != null)
        eodEl.innerHTML += ` &nbsp;/&nbsp; <b>${s.eod_projected_without.toFixed(0)}%</b> w/o`;
    } else {
      eodEl.style.display = 'none';
    }

    // Dehumidifier
    const dehumBadge = el('dehum-badge');
    const dehumErrEl = el('dehum-error');
    const dehumAutoB = el('dehum-auto-badge');
    if (s.dehum_power !== null && s.dehum_power !== undefined) {
      dehumBadge.textContent = s.dehum_power ? 'Running' : 'Idle';
      dehumBadge.className = 'badge ' + (s.dehum_power ? 'badge-green' : 'badge-gray');
      el('dehum-humidity').textContent = s.dehum_humidity != null ? s.dehum_humidity + '%' : '—';
      el('dehum-tank').style.display = s.dehum_tank_full ? '' : 'none';
      dehumAutoB.style.display = s.dehum_auto_on ? '' : 'none';
      dehumErrEl.style.display = 'none';
      el('dehum-on-btn').className = s.dehum_power ? 'btn-amber' : 'btn-dim';
      el('dehum-off-btn').className = s.dehum_power ? 'btn-dim' : 'btn-amber';
    } else if (s.dehum_error) {
      dehumBadge.textContent = 'Error';
      dehumBadge.className = 'badge badge-red';
      dehumErrEl.textContent = s.dehum_error;
      dehumErrEl.style.display = '';
      dehumAutoB.style.display = 'none';
    } else {
      dehumBadge.textContent = 'N/C';
      dehumBadge.className = 'badge badge-gray';
      dehumAutoB.style.display = 'none';
    }
    const gridW = s.readings ? (s.readings.grid_power_w || 0) : 0;
    const pipColors = ['#f87171', '#fbbf24', '#34d399'];
    const pipActive = gridW >= dehumThresholdW ? 2 : gridW >= dehumThresholdW * 0.5 ? 1 : 0;
    for (let i = 0; i < 3; i++) {
      const pip = el('dehum-pip-' + i);
      pip.style.background = i === pipActive ? pipColors[i] : 'var(--muted)';
      pip.style.opacity = i === pipActive ? '1' : '.25';
    }

    // Weather
    if (wx) {
      el('wx-icon').textContent = wx.current_icon || '—';
      el('wx-desc').textContent = wx.current_desc || '—';
      el('wx-temp').textContent = wx.current_temp_f != null ? wx.current_temp_f.toFixed(1) + '°F' : '—';
      const pvKw = s.pv_peak_kw || 0;
      const effMap = s.pv_efficiency || {};
      function estSolar(rad_w, hour) {
        if (!pvKw) return '—';
        const theoretical = pvKw * rad_w / 1000;
        const eff = (hour != null && effMap[hour] != null && effMap[hour] > 0.05) ? effMap[hour] : null;
        const kw = eff !== null ? theoretical * eff : theoretical;
        return kw.toFixed(1) + ' kW' + (eff !== null ? '' : '*');
      }
      const curHour = new Date().getHours();
      const curEst = wx.current_radiation_w != null ? estSolar(wx.current_radiation_w, curHour) : '—';
      const curRadLabel = wx.current_radiation_w != null
        ? `<div style="font-size:10px;color:var(--muted);margin-top:1px;">${Math.round(wx.current_radiation_w)} W/m²</div>` : '';
      el('wx-radiation').innerHTML = curEst + curRadLabel;

      const row = el('hourly-row');
      if (wx.hourly && wx.hourly.length) {
        row.innerHTML = wx.hourly.map(h => {
          const hour = h.time ? parseInt(h.time.split(':')[0]) : null;
          return `<div class="hour-card">
            <div class="time">${h.time}</div>
            <div class="emoji">${h.icon}</div>
            <div class="temp">${h.temp_f.toFixed(0)}°F</div>
            <div class="rad">${estSolar(h.radiation_w, hour)}</div>
            <div class="rad">${h.radiation_w != null ? Math.round(h.radiation_w) + ' W/m²' : ''}</div>
          </div>`;
        }).join('');
      }
    } else {
      el('wx-icon').textContent = '—';
      el('wx-desc').textContent = 'No data';
      el('wx-temp').textContent = '';
      el('wx-radiation').textContent = 'Configure location in Settings';
    }

    // BTC + pool
    const btcPrice = s.btc_price_usd || 0;
    el('btc-price').textContent = btcPrice
      ? '$' + btcPrice.toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—';
    el('btc-price-sub').textContent = btcPrice ? 'per BTC' : 'mempool.space';

    function satsToUsd(sats) {
      if (!sats || !btcPrice) return btcPrice ? '$0.00' : '—';
      const usd = (sats / 1e8) * btcPrice;
      return usd < 0.01 ? '<$0.01' : '$' + usd.toFixed(2);
    }
    const todaySats = s.today_sats || 0;
    el('pool-sats-today').textContent = todaySats.toLocaleString() + ' sats';
    el('pool-today-usd').textContent = satsToUsd(todaySats);

    const pool = s.pool;
    if (pool) {
      const unpaid = pool.unpaid_sats || 0;
      const hr5m = pool.hashrate_ths || 0;
      const hr24h = pool.hashrate_24h_ths || 0;
      const hrDisplay = hr5m > 0 ? hr5m : hr24h;
      const hrLabel = hr5m > 0 ? 'TH/s live' : (hr24h > 0 ? 'TH/s 24h avg' : '');
      el('pool-sats-unpaid').textContent = unpaid.toLocaleString() + ' sats';
      el('pool-unpaid-usd').textContent = satsToUsd(unpaid);
      el('pool-hashrate').textContent = hrDisplay > 0 ? hrDisplay.toFixed(2) + ' ' + hrLabel : '';
      el('pool-shares').textContent = pool.uptime_pct != null ? pool.uptime_pct.toFixed(0) + '% uptime 24h' : '';
    } else {
      el('pool-sats-unpaid').textContent = '—';
      el('pool-unpaid-usd').textContent = '';
      el('pool-hashrate').textContent = '';
      el('pool-shares').textContent = '';
    }
  } catch (e) { console.warn('refresh error:', e); }
}

// ── Mining control ─────────────────────────────────────────────
let _fastPoll = null;
let _minerProfiles = null;    // /api/miner/profiles payload (per-miner ladder + current)
let _lastMinersStatus = null; // latest status so profile fetches can re-render
let _ramp = null;             // latest ramp plan from /api/status

function _renderRampChips(s) {
  const box = el('ramp-chips');
  if (!box) return;
  const p = s.ramp;
  if (!p) { box.innerHTML = ''; return; }
  const hw = p.headroom_w || 0;
  const chips = [
    `<span class="chip">${hw >= 0 ? 'surplus' : 'deficit'} <b>${(hw / 1000).toFixed(2)} kW</b></span>`,
    `<span class="chip">reserve <b>${((p.reserve_w || 0) / 1000).toFixed(2)} kW</b></span>`,
    `<span class="chip">now <b>${((p.current_total_w || 0) / 1000).toFixed(2)}</b> → target <b>${((p.target_total_w || 0) / 1000).toFixed(2)} kW</b></span>`,
  ];
  if (p.needs_battery_capacity)
    chips.push('<span class="chip warn">set battery capacity to arm</span>');
  box.innerHTML = chips.join('');
}

// Green (low power) → amber → red (high power) for a filled step i of n.
function _stepColor(i, n) {
  const hue = 140 - 140 * (n > 1 ? i / (n - 1) : 0);
  return `hsl(${hue.toFixed(0)}, 68%, 48%)`;
}

// Stepped power bar: one segment per profile rung, filled to the current
// level, with the ramp's target rung outlined. Fill reflects ACTUAL draw —
// an off/sleeping miner shows an empty bar even though it still reports a
// configured profile.
function _stepBar(prof, rampMiner, running) {
  const ladder = prof && prof.ladder;
  if (!ladder || !ladder.length) return '';
  const curName = prof.current_name;
  const curIdx = running ? ladder.findIndex(p => p.name === curName) : -1;
  const tgtName = rampMiner && rampMiner.target_profile;
  const tgtIdx = tgtName ? ladder.findIndex(p => p.name === tgtName) : -1;
  const boxes = ladder.map((p, i) => {
    let cls = 'step';
    let style = '';
    if (curIdx >= 0 && i <= curIdx) style = `background:${_stepColor(i, ladder.length)}`;
    if (i === tgtIdx) cls += ' target';
    const t = `${p.frequency} MHz · ${p.watts} W · ${p.hashrate_ths} TH/s`;
    return `<div class="${cls}" style="${style}" title="${t}"></div>`;
  }).join('');
  const cur = (curIdx >= 0) ? (ladder[curIdx].watts / 1000).toFixed(2) + ' kW' : 'off';
  let lbl = cur;
  if (tgtIdx >= 0 && tgtIdx !== curIdx) lbl = cur + ' → ' + (ladder[tgtIdx].watts / 1000).toFixed(2);
  return `<div class="miner-steps"><div class="step-bar">${boxes}</div>`
       + `<span class="m-power">${lbl}</span></div>`;
}

// Per-miner rows (X1, X2), each with its own Start/Stop buttons. Stop pauses
// just that miner until midnight.
function _renderMiners(miners, checking) {
  const list = el('miners-list');
  if (!list) return;
  if (!checking) _lastMinersStatus = miners;
  const present = (miners && Object.keys(miners).length) ? Object.keys(miners).sort() : [];
  if (!present.length && !checking) {
    list.innerHTML = '<div class="miner-row"><span class="m-state" style="color:var(--muted)">No miner configured</span></div>';
    return;
  }
  const labels = present.length ? present : ['X1'];
  list.innerHTML = labels.map(label => {
    const m = (miners && miners[label]) || {};
    const prof = _minerProfiles && _minerProfiles[label];
    let stateHtml;
    if (checking) {
      stateHtml = '<span style="color:var(--amber)">checking…</span>';
    } else if (m.reachable === false) {
      stateHtml = '<span style="color:var(--amber)" title="Can\'t reach this miner">unreachable'
                + (m.hold ? ' · held' : '') + '</span>';
    } else if (m.hold) {
      stateHtml = '<span style="color:var(--amber)" title="Paused until local midnight">held</span>';
    } else if (m.running === true) {
      stateHtml = '<span style="color:var(--green)">● ON</span>';
    } else if (m.running === false) {
      stateHtml = '<span style="color:var(--red)">● OFF</span>';
    } else {
      stateHtml = '<span style="color:var(--muted)">?</span>';
    }
    let hr = '';
    if (m.reachable !== false && m.hashrate_mhs) {
      hr = (m.hashrate_mhs / 1e6).toFixed(1) + ' TH/s';
    }
    const dis = checking ? 'disabled' : '';
    // ︎ forces text (monochrome, compact) rendering of the ▶ / ■ glyphs.
    const btns = `<span class="m-btns">`
      + `<button class="btn-green" ${dis} onclick="minerStart('${label}')" title="Start ${label}">▶︎</button>`
      + `<button class="btn-red" ${dis} onclick="minerStop('${label}')" title="Stop ${label} until midnight">■︎</button>`
      + `</span>`;
    const row = `<div class="miner-row"><span class="m-name">${label}</span>`
      + `<span class="m-state">${stateHtml}</span><span class="m-hr">${hr}</span>${btns}</div>`;
    const rampMiner = _ramp && _ramp.per_miner && _ramp.per_miner[label];
    // Only fill the bar when the miner is actually mining (not held/sleeping).
    const isRunning = (m.running === true) && !m.hold;
    const steps = (!checking && m.reachable !== false) ? _stepBar(prof, rampMiner, isRunning) : '';
    return row + steps;
  }).join('');
}

async function loadMinerProfiles() {
  try {
    const r = await fetch('/api/miner/profiles');
    if (!r.ok) return;
    const d = await r.json();
    _minerProfiles = d.miners || null;
    if (_lastMinersStatus) _renderMiners(_lastMinersStatus, false);
  } catch (e) { /* non-fatal */ }
}

// Ramp activity log — recent decisions (dry-run included).
async function loadRampLog() {
  const card = el('ramp-log-card');
  if (!card) return;
  try {
    const r = await fetch('/api/ramp_log?limit=60');
    if (!r.ok) return;
    const evs = (await r.json()).events || [];
    if (!_ramp && evs.length === 0) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    const modeEl = el('ramp-log-mode');
    let mode, cls;
    if (_ramp) {
      mode = _ramp.dry_run ? 'DRY-RUN' : 'ARMED';
      cls = _ramp.dry_run ? 'badge-amber' : 'badge-green';
    } else if (evs.length) {
      mode = evs[0].armed ? 'ARMED' : 'DRY-RUN';
      cls = evs[0].armed ? 'badge-green' : 'badge-amber';
    } else {
      mode = 'off'; cls = 'badge-gray';
    }
    modeEl.textContent = mode;
    modeEl.className = 'badge ' + cls;
    const box = el('ramp-log');
    if (!evs.length) {
      box.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:6px 4px;">No ramp activity yet — decisions appear here as the ramp adjusts.</div>';
      return;
    }
    box.innerHTML = evs.map(e => {
      const t = new Date(e.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      const hw = e.headroom_w || 0;
      const why = `SOC ${(e.soc || 0).toFixed(0)}% · `
        + (hw >= 0 ? `surplus +${(hw / 1000).toFixed(1)} kW` : `deficit ${(hw / 1000).toFixed(1)} kW`);
      const dry = e.armed ? '' : ' <span class="rl-dry">DRY</span>';
      return `<div class="ramp-ev"><span class="rl-time">${t}</span>`
           + `<span class="rl-what">${e.detail}${dry}</span>`
           + `<span class="rl-why">${why}</span></div>`;
    }).join('');
  } catch (e) { /* non-fatal */ }
}

// Poll the targeted miner (or the aggregate) until it reaches desiredState.
function _startFastPoll(desiredState, label) {
  if (_fastPoll) clearInterval(_fastPoll);
  let attempts = 0;
  const maxAttempts = 10;  // 2s × 10 = 20s max
  _renderMiners(null, true);
  _fastPoll = setInterval(async () => {
    attempts++;
    try {
      const resp = await fetch('/api/miner/quick');
      const data = await resp.json();
      if (resp.ok) {
        _renderMiners(data.miners, false);
        const observed = label
          ? (data.miners && data.miners[label] ? data.miners[label].running : undefined)
          : data.mining;
        if (observed === desiredState || attempts >= maxAttempts) {
          clearInterval(_fastPoll);
          _fastPoll = null;
          refresh();
          loadMinerProfiles();
        }
      }
    } catch (e) {
      if (attempts >= maxAttempts) { clearInterval(_fastPoll); _fastPoll = null; }
    }
  }, 2000);
}

async function toggleSmartHold() {
  const held = el('smart-hold-btn').dataset.held === '1';
  try {
    const resp = await fetch('/api/smart/hold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hold: !held }),
    });
    if (!resp.ok) { const d = await resp.json(); alert('Error: ' + (d.error || resp.status)); return; }
    await refresh();
  } catch (e) { alert('Error: ' + e); }
}

async function minerStart(label) {
  try {
    const resp = await fetch('/api/miner/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(label ? { miner: label } : {}),
    });
    const data = await resp.json();
    if (!resp.ok) { alert('Error: ' + (data.error || 'unknown')); return; }
    _startFastPoll(true, label);
  } catch (e) { alert('Error: ' + e); }
}

async function minerStop(label) {
  try {
    const resp = await fetch('/api/miner/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(label ? { miner: label } : {}),
    });
    const data = await resp.json();
    if (!resp.ok) { alert('Error: ' + (data.error || 'unknown')); return; }
    _startFastPoll(false, label);
  } catch (e) { alert('Error: ' + e); }
}

async function dehum(on) {
  try {
    const r = await fetch('/api/dehum/power', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on }),
    });
    if (!r.ok) { const d = await r.json(); alert('Error: ' + (d.error || r.status)); return; }
    await refresh();
  } catch (e) { alert('Request failed: ' + e); }
}

// ── Settings ───────────────────────────────────────────────────
// Fields are generic: every element with id "s-<setting_key>" is loaded from
// and saved to that settings key. Types are coerced server-side by the
// settings schema, so adding a setting = one row in config.py + one field here.
const SECRET_KEYS = ['solis_api_secret', 'lux_pool_api_key', 'telegram_bot_token', 'dehum_local_key'];

function _settingsFields() {
  return Array.from(document.querySelectorAll('[id^="s-"]'))
    .map(inp => ({ inp, key: inp.id.slice(2) }));
}

function showPanel(name, btn) {
  document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.settings-nav-item').forEach(b => b.classList.remove('active'));
  el('panel-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'system') loadEffChart();
}

async function loadSettings() {
  try {
    const resp = await fetch('/api/settings');
    if (!resp.ok) return;
    const s = await resp.json();
    for (const { inp, key } of _settingsFields()) {
      if (!(key in s)) continue;
      if (inp.type === 'checkbox') inp.checked = !!s[key];
      else if (inp.tagName === 'SELECT' && key === 'miner_max_watts') continue; // populated below
      else inp.value = s[key] != null ? s[key] : '';
    }
    el('s-eod_soc_target').disabled = !el('s-eod_soc_target_enabled').checked;
    _populateMaxProfile(s['miner_max_watts']);
    if (s['dehum_excess_threshold_w']) dehumThresholdW = Number(s['dehum_excess_threshold_w']);
  } catch (e) { console.warn('loadSettings error:', e); }
}

async function saveSettings() {
  const payload = {};
  for (const { inp, key } of _settingsFields()) {
    if (inp.type === 'checkbox') payload[key] = inp.checked;
    else if (inp.type === 'number') payload[key] = Number(inp.value);
    else payload[key] = inp.value;
  }
  // Empty secret field = keep the stored secret (don't clear it).
  for (const k of SECRET_KEYS) {
    if (!payload[k]) delete payload[k];
  }
  try {
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp.ok) { closeSettings(); refresh(); }
    else alert('Failed to save settings');
  } catch (e) { alert('Error saving settings: ' + e); }
}

async function searchLocation() {
  const raw = el('s-location_name').value.trim();
  const zip = raw.replace(/\D/g, '').slice(0, 5);
  if (!zip) { alert('Enter a ZIP code first'); return; }
  if (!/^\d{5}$/.test(zip)) { alert('Please enter a valid 5-digit US ZIP code'); return; }
  try {
    const resp = await fetch('/api/geocode?q=' + encodeURIComponent(zip));
    if (resp.ok) {
      const data = await resp.json();
      el('s-location_lat').value = data.lat;
      el('s-location_lon').value = data.lon;
      if (data.name) el('s-location_name').value = data.name;
    } else {
      alert('Location not found — check the ZIP code and try again');
    }
  } catch (e) { alert('Geocode error: ' + e); }
}

// Fill the "max profile" dropdown from a miner's live profile ladder.
async function _populateMaxProfile(savedWatts) {
  const sel = el('s-miner_max_watts');
  if (!sel) return;
  if (!_minerProfiles) await loadMinerProfiles();
  let ladder = null;
  if (_minerProfiles) {
    for (const k of Object.keys(_minerProfiles)) {
      const p = _minerProfiles[k];
      if (p && p.reachable && p.ladder && p.ladder.length) { ladder = p.ladder; break; }
    }
  }
  const saved = Number(savedWatts) || 2960;
  sel.innerHTML = '';
  if (ladder) {
    for (const p of ladder) {
      const o = document.createElement('option');
      o.value = String(p.watts);
      o.textContent = `${p.frequency} MHz · ${p.watts} W · ${p.hashrate_ths} TH/s`;
      sel.appendChild(o);
    }
    let best = ladder[0].watts, bestd = Infinity;
    for (const p of ladder) { const d = Math.abs(p.watts - saved); if (d < bestd) { bestd = d; best = p.watts; } }
    sel.value = String(best);
  } else {
    const o = document.createElement('option');
    o.value = String(saved);
    o.textContent = saved + " W (miner offline — can't list profiles)";
    sel.appendChild(o);
    sel.value = String(saved);
  }
}

function openSettings() {
  el('settings-modal').classList.add('open');
  loadSettings();
}
function closeSettings() {
  el('settings-modal').classList.remove('open');
}
el('settings-modal').addEventListener('click', function (e) {
  if (e.target === this) closeSettings();
});

function togglePwd(inputId, btn) {
  const inp = el(inputId);
  const show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  btn.textContent = show ? '🙈' : '👁';
}

el('btn-reset-eff').onclick = async () => {
  if (!confirm('Clear all learned efficiency data? It will relearn over the next few days.')) return;
  const r = await fetch('/api/reset_efficiency', { method: 'POST' });
  alert(r.ok ? 'Efficiency data cleared.' : 'Failed to reset — check logs.');
};

async function testTelegram() {
  const res = el('tg-test-result');
  res.textContent = 'Sending…';
  res.style.color = 'var(--muted)';
  try {
    const resp = await fetch('/api/telegram/test', { method: 'POST' });
    const data = await resp.json();
    if (resp.ok) {
      res.textContent = '✓ Sent! Check Telegram. Bot: ' + data.bot;
      res.style.color = 'var(--green)';
    } else {
      res.textContent = '✗ ' + (data.error || 'unknown error');
      res.style.color = 'var(--red)';
    }
  } catch (e) {
    res.textContent = '✗ ' + e;
    res.style.color = 'var(--red)';
  }
}

// ── PV efficiency chart (settings) ─────────────────────────────
let _effAllRows = null;
let _effCurrentMonth = 1;

function _effDataForMonth(month) {
  const byHour = {};
  for (const r of (_effAllRows || [])) {
    if (!byHour[r.hour]) byHour[r.hour] = {};
    byHour[r.hour][r.month] = { ratio: r.ratio, samples: r.samples };
  }
  const result = [];
  for (let h = 0; h < 24; h++) {
    const monthMap = byHour[h] || {};
    let found = null, fromMonth = null;
    for (let offset = 0; offset < 7; offset++) {
      const candidates = offset === 0 ? [month] : [
        ((month - 1 + offset) % 12) + 1,
        ((month - 1 - offset + 12) % 12) + 1,
      ];
      for (const m of candidates) {
        if (monthMap[m] && monthMap[m].samples >= 3) { found = monthMap[m]; fromMonth = m; break; }
      }
      if (found) break;
    }
    if (!found && monthMap[month]) { found = monthMap[month]; fromMonth = month; }
    if (found) result.push({ hour: h, ratio: found.ratio, samples: found.samples, fromMonth });
  }
  return result;
}

function _renderEffChart(month) {
  const emptyEl = el('eff-chart-empty');
  const canvasEl = el('eff-chart');
  const rows = _effDataForMonth(month);

  if (!rows.length) {
    canvasEl.style.display = 'none';
    emptyEl.style.display = '';
    return;
  }
  canvasEl.style.display = '';
  emptyEl.style.display = 'none';

  const MONTH_NAMES = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const labels = rows.map(r => r.hour + ':00');
  const ratios = rows.map(r => +(r.ratio * 100).toFixed(1));
  const colors = rows.map(r => {
    if (r.fromMonth !== month) return 'rgba(251,146,60,0.65)';
    return r.samples >= 3 ? 'rgba(52,211,153,0.75)' : 'rgba(234,179,8,0.6)';
  });
  const borders = rows.map(r => {
    if (r.fromMonth !== month) return 'rgba(251,146,60,1)';
    return r.samples >= 3 ? 'rgba(52,211,153,1)' : 'rgba(234,179,8,1)';
  });

  if (effChart) { effChart.destroy(); effChart = null; }
  effChart = new Chart(canvasEl.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Efficiency %', data: ratios,
          backgroundColor: colors, borderColor: borders,
          borderWidth: 1, borderRadius: 3, order: 2,
        },
        {
          label: '100% theoretical', data: labels.map(() => 100),
          type: 'line', borderColor: 'rgba(255,255,255,0.25)',
          borderWidth: 1, borderDash: [4, 4], pointRadius: 0, fill: false, order: 1,
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => item.datasetIndex === 0,
          callbacks: {
            label: (ctx) => {
              const r = rows[ctx.dataIndex];
              const borrowed = r.fromMonth !== month ? ` (from ${MONTH_NAMES[r.fromMonth]})` : '';
              return [
                `${ctx.parsed.y.toFixed(1)}% of theoretical`,
                `${r.samples} sample${r.samples !== 1 ? 's' : ''}${borrowed}${r.samples < 3 && !borrowed ? ' (still learning)' : ''}`,
              ];
            }
          }
        }
      },
      scales: {
        x: { ticks: { color: TICK_COLOR, font: { size: 10 } }, grid: { color: GRID_COLOR } },
        y: { min: 0, ticks: { color: TICK_COLOR, font: { size: 10 }, callback: v => v + '%' }, grid: { color: GRID_COLOR } },
      }
    }
  });
}

async function loadEffChart() {
  try {
    const resp = await fetch('/api/pv_efficiency');
    if (!resp.ok) return;
    const data = await resp.json();
    _effAllRows = data.rows || [];
    _effCurrentMonth = data.current_month || 1;
    const sel = el('eff-month-select');
    if (sel && !sel._userChanged) sel.value = String(_effCurrentMonth);
    _renderEffChart(sel ? parseInt(sel.value) : _effCurrentMonth);
  } catch (e) { console.warn('loadEffChart error:', e); }
}

el('eff-month-select').addEventListener('change', function () {
  this._userChanged = true;
  _renderEffChart(parseInt(this.value));
});

// ── Boot ───────────────────────────────────────────────────────
setInterval(() => { refresh(); loadHistory(); loadDailySats(); loadMinerProfiles(); }, 30000);
window.addEventListener('load', () => { refresh(); loadHistory(); loadDailySats(); loadMinerProfiles(); });
