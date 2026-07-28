/* ==========================================================================
   MoM-IGD shell script.

   Security notes:
   - The session token is NEVER present in this file, in any URL, in the DOM, or
     in localStorage/sessionStorage/cookies. Endpoints that need it are called
     through window.pywebview.api.api_get(), which is a Python method: the token
     is attached on the Python side and never crosses into JavaScript.
   - /health and /version are public and are fetched directly with same-origin
     relative URLs.
   - When the page is opened in a plain browser (manual verification) there is no
     pywebview bridge. The page then says so instead of degrading silently or
     inventing data.
   ========================================================================== */
'use strict';

(function () {
  const PYWEBVIEW_READY_TIMEOUT_MS = 4000;

  const el = {
    appName: document.getElementById('app-name'),
    appVersion: document.getElementById('app-version'),
    appPhase: document.getElementById('app-phase'),
    offlineBadge: document.getElementById('offline-badge'),
    refreshBtn: document.getElementById('refresh-btn'),
    statusUpdated: document.getElementById('status-updated'),
    diagBody: document.querySelector('#diag-details .diag-body'),
    footerBuild: document.getElementById('footer-build'),
    cards: {
      backend: document.getElementById('card-backend'),
      database: document.getElementById('card-database'),
      datadir: document.getElementById('card-datadir'),
      readiness: document.getElementById('card-readiness')
    }
  };

  // ---------------------------------------------------------------- helpers

  function setCard(card, state, detail, pairs) {
    const pill = card.querySelector('.status-pill');
    pill.dataset.state = state;
    pill.textContent = { ok: 'OK', warn: 'WARN', fail: 'FAIL', unknown: '—' }[state] || '—';

    card.querySelector('.status-detail').textContent = detail;

    const list = card.querySelector('.kv');
    list.textContent = '';
    (pairs || []).forEach(function (pair) {
      const dt = document.createElement('dt');
      dt.textContent = pair[0];
      const dd = document.createElement('dd');
      dd.textContent = pair[1];
      list.appendChild(dt);
      list.appendChild(dd);
    });
  }

  function yesNo(value) {
    if (value === true) return 'ya';
    if (value === false) return 'tidak';
    return 'tidak diketahui';
  }

  function waitForPywebview() {
    if (window.pywebview && window.pywebview.api) {
      return Promise.resolve(true);
    }
    return new Promise(function (resolve) {
      let settled = false;
      function done(value) {
        if (!settled) {
          settled = true;
          resolve(value);
        }
      }
      window.addEventListener('pywebviewready', function () { done(true); }, { once: true });
      setTimeout(function () {
        done(Boolean(window.pywebview && window.pywebview.api));
      }, PYWEBVIEW_READY_TIMEOUT_MS);
    });
  }

  async function getPublic(path) {
    // Relative URL: same origin as the loopback backend that served this page.
    const response = await fetch(path, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'omit'
    });
    if (!response.ok) {
      throw new Error('HTTP ' + response.status + ' for ' + path);
    }
    return response.json();
  }

  async function getProtected(path) {
    if (!(window.pywebview && window.pywebview.api && window.pywebview.api.api_get)) {
      return { ok: false, status: 0, error: 'bridge-unavailable' };
    }
    return window.pywebview.api.api_get(path);
  }

  // ------------------------------------------------------------- rendering

  function renderIdentity(version) {
    el.appName.textContent = version.app_name || 'MoM-IGD';
    el.appVersion.textContent = 'v' + (version.app_version || '?');
    el.appPhase.textContent = 'Phase ' + (version.phase || '—');
    el.footerBuild.textContent =
      'schema config v' + version.config_schema_version +
      ' · schema registry v' + version.registry_schema_version +
      ' · schema DB head v' + version.schema_version_head +
      ' · Python ' + (version.python || '?') +
      ' · ' + (version.platform || '?');
  }

  function renderBackend(health) {
    setCard(el.cards.backend, 'ok', 'Backend loopback merespons.', [
      ['status', health.status],
      ['mode', health.runtime_mode],
      ['uptime', health.uptime_seconds === null ? '—' : health.uptime_seconds + ' s'],
      ['offline', yesNo(health.offline)]
    ]);
    el.offlineBadge.dataset.confirmed = String(Boolean(health.offline));
  }

  function renderDatabase(health) {
    const db = health.database || {};
    let state = 'fail';
    let detail;
    if (!db.exists) {
      state = 'warn';
      detail = 'Database belum dibuat. Jalankan: python -m mom_igd db init';
    } else if (db.ready) {
      state = 'ok';
      detail = 'SQLite siap: WAL aktif, foreign key aktif, skema pada versi head.';
    } else {
      detail = 'Database ada tetapi belum siap (skema belum di versi head, atau pragma tidak terkonfirmasi).';
    }
    setCard(el.cards.database, state, detail, [
      ['ada', yesNo(db.exists)],
      ['WAL', yesNo(db.wal)],
      ['foreign keys', yesNo(db.foreign_keys)],
      ['versi skema', (db.schema_version === null ? '—' : db.schema_version) + ' / head ' + (db.head_version === null ? '—' : db.head_version)]
    ]);
  }

  function renderDataDir(health, readyPayload) {
    const dir = health.data_dir || {};
    let state = 'fail';
    let detail;
    if (dir.writable && dir.complete) {
      state = 'ok';
      detail = 'Direktori data runtime dapat ditulis dan lengkap.';
    } else if (dir.writable) {
      state = 'warn';
      detail = 'Lokasi dapat ditulis, tetapi sebagian subdirektori runtime belum dibuat.';
    } else {
      detail = 'Direktori data runtime tidak dapat ditulis. Aplikasi tidak dapat menyimpan rekaman atau database.';
    }

    // The absolute path comes from /internal/ready, which requires the token and
    // is therefore only available through the Python bridge.
    const pairs = [
      ['ada', yesNo(dir.exists)],
      ['dapat ditulis', yesNo(dir.writable)],
      ['lengkap', yesNo(dir.complete)]
    ];
    if (readyPayload && readyPayload.data_dir && readyPayload.data_dir.root) {
      pairs.push(['lokasi', readyPayload.data_dir.root]);
    } else {
      pairs.push(['lokasi', 'butuh token (lihat rincian diagnostik)']);
    }
    setCard(el.cards.datadir, state, detail, pairs);
  }

  function renderReadiness(doctorPayload, bridgeAvailable) {
    if (!bridgeAvailable) {
      setCard(
        el.cards.readiness,
        'warn',
        'Diagnostik lengkap butuh session token dan hanya tersedia di dalam shell desktop. ' +
          'Halaman ini sedang dibuka tanpa jembatan pywebview.',
        [['jalankan', 'python -m mom_igd doctor']]
      );
      return;
    }
    if (!doctorPayload || !doctorPayload.ok) {
      const message = doctorPayload && doctorPayload.error
        ? JSON.stringify(doctorPayload.error)
        : 'tidak diketahui';
      setCard(el.cards.readiness, 'fail', 'Gagal mengambil diagnostik: ' + message, []);
      return;
    }

    const report = doctorPayload.data;
    const counts = report.counts || {};
    const state = counts.FAIL > 0 ? 'fail' : (counts.WARN > 0 ? 'warn' : 'ok');
    const cpu = (report.results || []).find(function (r) { return r.key === 'cpu'; });
    const ram = (report.results || []).find(function (r) { return r.key === 'ram'; });
    const disk = (report.results || []).find(function (r) { return r.key === 'disk'; });

    const detail = counts.FAIL > 0
      ? counts.FAIL + ' pemeriksaan wajib GAGAL untuk phase saat ini.'
      : 'Semua pemeriksaan wajib phase saat ini terpenuhi. Peringatan berasal dari dependensi phase berikutnya.';

    setCard(el.cards.readiness, state, detail, [
      ['ringkasan', counts.PASS + ' PASS · ' + counts.WARN + ' WARN · ' + counts.FAIL + ' FAIL'],
      ['CPU', cpu ? cpu.detail : '—'],
      ['RAM', ram ? ram.detail : '—'],
      ['disk', disk ? disk.detail : '—']
    ]);

    renderDiagnostics(report);
  }

  function renderDiagnostics(report) {
    el.diagBody.textContent = '';
    const table = document.createElement('table');
    table.className = 'diag-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    ['Status', 'Pemeriksaan', 'Wajib di phase', 'Keterangan'].forEach(function (label) {
      const th = document.createElement('th');
      th.textContent = label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    (report.results || []).forEach(function (result) {
      const tr = document.createElement('tr');

      const tdStatus = document.createElement('td');
      const tag = document.createElement('span');
      tag.className = 'tag tag-' + String(result.status).toLowerCase();
      tag.textContent = result.status;
      tdStatus.appendChild(tag);

      const tdKey = document.createElement('td');
      tdKey.className = 'key';
      tdKey.textContent = result.key;

      const tdPhase = document.createElement('td');
      tdPhase.textContent = result.required_in_phase || 'informasi';

      const tdDetail = document.createElement('td');
      tdDetail.className = 'detail';
      tdDetail.textContent = result.detail;

      tr.appendChild(tdStatus);
      tr.appendChild(tdKey);
      tr.appendChild(tdPhase);
      tr.appendChild(tdDetail);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    el.diagBody.appendChild(table);
  }

  // -------------------------------------------------------------- refresh

  async function refresh() {
    el.refreshBtn.disabled = true;
    el.statusUpdated.textContent = 'Memeriksa…';

    const bridgeAvailable = await waitForPywebview();

    try {
      const version = await getPublic('/version');
      renderIdentity(version);
    } catch (error) {
      el.footerBuild.textContent = 'Tidak dapat membaca /version: ' + error.message;
    }

    let health = null;
    try {
      health = await getPublic('/health');
      renderBackend(health);
      renderDatabase(health);
    } catch (error) {
      setCard(el.cards.backend, 'fail', 'Backend tidak dapat dihubungi: ' + error.message, []);
      setCard(el.cards.database, 'unknown', 'Tidak dapat diperiksa tanpa backend.', []);
      setCard(el.cards.datadir, 'unknown', 'Tidak dapat diperiksa tanpa backend.', []);
      setCard(el.cards.readiness, 'unknown', 'Tidak dapat diperiksa tanpa backend.', []);
      el.statusUpdated.textContent = 'Gagal pada ' + new Date().toLocaleTimeString('id-ID');
      el.refreshBtn.disabled = false;
      return;
    }

    let readyPayload = null;
    const readyEnvelope = await getProtected('/internal/ready');
    if (readyEnvelope && readyEnvelope.ok) {
      readyPayload = readyEnvelope.data;
    }
    renderDataDir(health, readyPayload);

    const doctorEnvelope = await getProtected('/doctor');
    renderReadiness(doctorEnvelope, bridgeAvailable);

    el.statusUpdated.textContent = 'Diperiksa pada ' + new Date().toLocaleTimeString('id-ID');
    el.refreshBtn.disabled = false;
  }

  el.refreshBtn.addEventListener('click', function () { refresh(); });
  document.addEventListener('DOMContentLoaded', function () { refresh(); });
  if (document.readyState !== 'loading') {
    refresh();
  }
})();

/* =========================================================================
   Recording panel (Phase 2).

   Every authenticated call goes through window.pywebview.api, so the session
   token stays on the Python side and never enters this file, the DOM or any URL.
   No localStorage, no sessionStorage, no cookie: state lives in memory only.
   ========================================================================= */
(function () {
  'use strict';

  // Fallback only. The real rate comes from audio.status_poll_hz, which every
  // status payload carries -- a hardcoded constant here would silently ignore the
  // operator's configuration.
  var DEFAULT_POLL_MS = 350;       // ~3 Hz, the configured default
  var MIN_POLL_MS = 250;           // config caps status_poll_hz at 4.0
  var MAX_POLL_MS = 1000;          // ...and floors it at 1.0
  var pollMs = DEFAULT_POLL_MS;
  var pollTimer = null;
  var pollStopped = false;
  var startInFlight = false;
  var preflightOk = false;        // Start stays blocked until preflight says yes
  var devices = [];
  var currentUuid = null;

  function adoptPollRate(hz) {
    var value = Number(hz);
    if (!isFinite(value) || value <= 0) return;
    var next = Math.round(1000 / value);
    pollMs = Math.min(MAX_POLL_MS, Math.max(MIN_POLL_MS, next));
  }

  var el = {
    panel: document.getElementById('recording-panel'),
    open: document.getElementById('open-recording-btn'),
    select: document.getElementById('device-select'),
    evidence: document.getElementById('device-evidence'),
    refresh: document.getElementById('refresh-devices-btn'),
    use: document.getElementById('select-device-btn'),
    calibrate: document.getElementById('calibrate-btn'),
    verdict: document.getElementById('level-verdict'),
    rms: document.getElementById('meter-rms'),
    peak: document.getElementById('meter-peak'),
    levelDetail: document.getElementById('level-detail'),
    levelAdvice: document.getElementById('level-advice'),
    preflightPill: document.getElementById('preflight-pill'),
    preflightList: document.getElementById('preflight-list'),
    preflightBtn: document.getElementById('preflight-btn'),
    storage: document.getElementById('storage-estimate'),
    recPill: document.getElementById('rec-pill'),
    meetingTitle: document.getElementById('meeting-title'),
    start: document.getElementById('start-btn'),
    pause: document.getElementById('pause-btn'),
    resume: document.getElementById('resume-btn'),
    stop: document.getElementById('stop-btn'),
    elapsed: document.getElementById('elapsed'),
    recDetail: document.getElementById('rec-detail'),
    recWarning: document.getElementById('rec-warning'),
    integrity: document.getElementById('integrity-detail'),
    verify: document.getElementById('verify-btn'),
    recover: document.getElementById('recover-btn'),
    recovery: document.getElementById('recovery-detail')
  };

  function bridge() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  async function get(path, query) {
    var api = bridge();
    if (!api) return { ok: false, status: 0, error: 'bridge-unavailable' };
    return api.api_get(path, query || null);
  }

  async function post(path, payload) {
    var api = bridge();
    if (!api) return { ok: false, status: 0, error: 'bridge-unavailable' };
    return api.api_post(path, payload || null);
  }

  function setKv(node, pairs) {
    node.textContent = '';
    pairs.forEach(function (pair) {
      var dt = document.createElement('dt');
      dt.textContent = pair[0];
      var dd = document.createElement('dd');
      dd.textContent = pair[1];
      node.appendChild(dt);
      node.appendChild(dd);
    });
  }

  function errorText(envelope) {
    if (!envelope) return 'unknown error';
    if (envelope.error === 'bridge-unavailable') {
      return 'Panel ini hanya berfungsi di dalam shell desktop (butuh session token).';
    }
    if (typeof envelope.error === 'string') return envelope.error;
    return JSON.stringify(envelope.error);
  }

  function dbfsToPercent(dbfs) {
    if (typeof dbfs !== 'number') return 0;
    var clamped = Math.max(-60, Math.min(0, dbfs));
    return Math.round(((clamped + 60) / 60) * 100);
  }

  // ------------------------------------------------------------- devices

  async function loadDevices() {
    el.evidence.textContent = 'Memuat daftar perangkat…';
    var envelope = await get('/audio/devices', { refresh: true });
    if (!envelope.ok) {
      el.evidence.textContent = 'Gagal memuat perangkat: ' + errorText(envelope);
      return;
    }
    devices = envelope.data.devices || [];
    el.select.textContent = '';
    if (!devices.length) {
      var none = document.createElement('option');
      none.textContent = 'Tidak ada perangkat input';
      el.select.appendChild(none);
      el.select.disabled = true;
      el.evidence.textContent =
        (envelope.data.rejected || []).length +
        ' perangkat dikecualikan (output-only, loopback, virtual, atau nonaktif).';
      return;
    }
    el.select.disabled = false;
    devices.forEach(function (device) {
      var option = document.createElement('option');
      option.value = device.fingerprint;
      option.textContent = device.name + '  [' + device.host_api + ', ' +
        device.max_input_channels + ' ch]';
      if (device.fingerprint === envelope.data.selected_fingerprint) option.selected = true;
      el.select.appendChild(option);
    });
    describeSelected(envelope.data.verified_usb_available);
  }

  function describeSelected(usbAvailable) {
    var device = devices.filter(function (d) {
      return d.fingerprint === el.select.value;
    })[0];
    if (!device) { el.evidence.textContent = ''; return; }
    var lines = [];
    lines.push('Transport: ' + device.transport +
      (device.transport_verified ? ' (diverifikasi Windows)' : ' (BELUM diverifikasi)'));
    lines.push('Bukti: ' + device.transport_evidence);
    lines.push('Sample rate bawaan: ' + device.default_sample_rate + ' Hz');
    if (device.transport === 'INTERNAL') {
      lines.push(
        'Microphone internal: beamforming dan noise suppression menekan pembicara ' +
        'yang tidak menghadap laptop. Hanya untuk development.'
      );
    }
    if (!usbAvailable) {
      lines.push('Belum ada USB conference microphone terverifikasi.');
    }
    el.evidence.textContent = lines.join('  ·  ');
  }

  // ------------------------------------------------------- preflight

  async function runPreflight() {
    el.preflightBtn.disabled = true;
    var envelope = await get('/audio/preflight', { planned_minutes: 120 });
    el.preflightBtn.disabled = false;
    if (!envelope.ok) {
      el.preflightPill.dataset.state = 'fail';
      el.preflightPill.textContent = 'ERR';
      el.preflightList.textContent = '';
      el.storage.textContent = errorText(envelope);
      return false;
    }
    var report = envelope.data;
    el.preflightPill.dataset.state = report.can_start ? 'ok' : 'fail';
    el.preflightPill.textContent = report.can_start ? 'SIAP' : 'BLOK';
    el.preflightList.textContent = '';
    (report.items || []).forEach(function (item) {
      var li = document.createElement('li');
      var tag = document.createElement('span');
      tag.className = 'tag tag-' + String(item.status).toLowerCase();
      tag.textContent = item.status;
      var text = document.createElement('span');
      text.textContent = item.key + ' — ' + item.detail;
      li.appendChild(tag);
      li.appendChild(text);
      el.preflightList.appendChild(li);
    });
    if (report.estimate) {
      el.storage.textContent =
        'Estimasi ' + report.estimate.needed_gb + ' GB untuk ' +
        report.estimate.planned_minutes + ' menit; sisa disk ' +
        report.estimate.free_gb + ' GB (cukup untuk ±' +
        report.estimate.max_minutes_available + ' menit).';
    }
    preflightOk = report.can_start === true;
    el.start.disabled = !preflightOk;
    return preflightOk;
  }

  // ------------------------------------------------------ calibration

  async function calibrate() {
    el.calibrate.disabled = true;
    el.levelAdvice.textContent = 'Membuka microphone dan mengukur level…';
    var envelope = await post('/audio/calibrate', {});
    el.calibrate.disabled = false;
    if (!envelope.ok) {
      el.verdict.dataset.state = 'fail';
      el.verdict.textContent = 'ERR';
      el.levelAdvice.textContent = errorText(envelope);
      return;
    }
    renderLevels(envelope.data.levels, envelope.data.verdict, envelope.data.advice);
  }

  function renderLevels(levels, verdict, advice) {
    if (!levels) return;
    var state = verdict === 'GOOD' ? 'ok'
      : (verdict === 'CLIPPING' || verdict === 'NO_SIGNAL' ? 'fail' : 'warn');
    el.verdict.dataset.state = state;
    el.verdict.textContent = verdict;
    el.rms.dataset.state = state;
    el.rms.style.width = dbfsToPercent(levels.rms_dbfs) + '%';
    el.peak.style.left = dbfsToPercent(levels.peak_dbfs) + '%';
    var pairs = [
      ['rms', levels.rms_dbfs + ' dBFS'],
      ['peak', levels.peak_dbfs + ' dBFS'],
      ['clipping', levels.clipping_percent + ' %'],
      ['silence', levels.silence_percent + ' %'],
      ['noise floor', levels.noise_floor_dbfs + ' dBFS']
    ];
    (levels.channels || []).forEach(function (channel) {
      pairs.push(['ch ' + channel.channel,
        channel.rms_dbfs + ' dBFS ' + (channel.active ? '(aktif)' : '(TIDAK AKTIF)')]);
    });
    setKv(el.levelDetail, pairs);
    el.levelAdvice.textContent = advice || '';
  }

  // -------------------------------------------------------- transport

  function formatElapsed(seconds) {
    var total = Math.max(0, Math.floor(seconds || 0));
    var h = String(Math.floor(total / 3600)).padStart(2, '0');
    var m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
    var s = String(total % 60).padStart(2, '0');
    return h + ':' + m + ':' + s;
  }

  function renderStatus(status) {
    var lifecycle = status.lifecycle || 'IDLE';
    el.recPill.textContent = lifecycle;
    el.recPill.dataset.state =
      lifecycle === 'RECORDING' ? 'ok'
        : (lifecycle === 'PAUSED' ? 'warn'
          : (lifecycle === 'FAILED' || lifecycle === 'RECOVERABLE' ? 'fail' : 'unknown'));

    var active = status.recording_active === true;
    el.pause.disabled = lifecycle !== 'RECORDING';
    el.resume.disabled = lifecycle !== 'PAUSED';
    el.stop.disabled = !active;
    // Start needs BOTH: nothing recording, and a preflight that passed. Deciding
    // on `active` alone let the poll loop re-enable Start 350 ms after a preflight
    // had blocked it, offering a click that the service would only refuse.
    el.start.disabled = active || !preflightOk;
    el.verify.disabled = !currentUuid;
    if (el.meetingTitle) {
      // Locked while recording: the draft meeting was created at Start, so a
      // later edit would look applied without being applied.
      el.meetingTitle.disabled = active;
    }

    var session = status.session || {};
    el.elapsed.textContent = formatElapsed(session.elapsed_seconds);

    var profile = session.profile || {};
    var queue = session.queue || {};
    var pairs = [
      ['format', profile.sample_rate
        ? profile.sample_rate + ' Hz / ' + profile.channels + ' ch / ' + profile.sample_format
        : '—'],
      ['chunk aktif', session.current_chunk_seq === null || session.current_chunk_seq === undefined
        ? '—'
        : '#' + session.current_chunk_seq + ' (' +
          Math.round((session.current_chunk_progress || 0) * 100) + '%)'],
      ['chunk selesai', String(session.chunks_finalised || 0)],
      ['audio', (session.audio_seconds || 0) + ' s'],
      ['disk sisa', status.disk_free_gb + ' GB'],
      ['writer', session.writer_alive ? 'berjalan' : 'idle'],
      ['queue', (queue.high_water_percent || 0) + '% high-water']
    ];
    if (status.device) {
      pairs.push(['microphone', status.device.name + ' [' + status.device.transport + ']']);
    }
    setKv(el.recDetail, pairs);

    var warnings = [];
    if (queue.dropped_frames) {
      warnings.push('AUDIO HILANG: ' + queue.dropped_frames +
        ' frame terbuang karena writer tidak sanggup mengejar.');
    }
    if (session.stream && session.stream.xrun_callbacks) {
      warnings.push(session.stream.xrun_callbacks + ' xrun dilaporkan driver.');
    }
    if (status.degraded) warnings.push('Kualitas rekaman ditandai DEGRADED.');
    if (status.disk_low) warnings.push('Disk hampir penuh; perekaman akan dihentikan.');
    if (lifecycle === 'RECOVERABLE') {
      warnings.push('Perekaman terputus. Jalankan pemulihan untuk menyelamatkan audio.');
    }
    if (warnings.length) {
      el.recWarning.hidden = false;
      el.recWarning.textContent = warnings.join('  ');
    } else {
      el.recWarning.hidden = true;
    }

    setKv(el.integrity, [
      ['recording', status.recording_uuid || currentUuid || '—'],
      ['menunggu pemulihan', String(status.pending_recovery === undefined
        ? '—' : status.pending_recovery)]
    ]);
    if (status.recording_uuid) currentUuid = status.recording_uuid;
  }

  async function poll() {
    if (pollStopped) return;
    var envelope = await get('/audio/recordings/status');
    if (envelope.ok) {
      adoptPollRate(envelope.data.status_poll_hz);
      renderStatus(envelope.data);
      if (envelope.data.recording_active) {
        var quality = await get('/audio/quality');
        if (quality.ok && quality.data.available) {
          renderLevels(quality.data.rolling, quality.data.rolling.verdict,
            quality.data.rolling.advice);
        }
      }
    }
    if (pollStopped) return;
    pollTimer = setTimeout(poll, pollMs);
  }

  async function refreshButtons() {
    // One authoritative read, so the transport buttons never disagree with the
    // service. Cheaper than guessing from whichever request just finished.
    var envelope = await get('/audio/recordings/status');
    if (envelope.ok) renderStatus(envelope.data);
  }

  function stopPolling() {
    // The window is going away. Leaving a timer behind would keep calling into a
    // bridge whose Python side is shutting down, which logs noise on exit.
    pollStopped = true;
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  window.addEventListener('pagehide', stopPolling);
  window.addEventListener('beforeunload', stopPolling);

  // ----------------------------------------------------------- actions

  async function act(button, path, payload, label) {
    button.disabled = true;
    var envelope = await post(path, payload);
    button.disabled = false;
    if (!envelope.ok) {
      el.recWarning.hidden = false;
      el.recWarning.textContent = label + ' gagal: ' + errorText(envelope);
      return null;
    }
    renderStatus(envelope.data);
    return envelope.data;
  }

  el.open.addEventListener('click', async function () {
    el.panel.hidden = false;
    el.open.disabled = true;
    el.open.textContent = 'Panel terbuka';
    await loadDevices();
    await runPreflight();
    await refreshRecovery();
    if (pollTimer === null) poll();
    el.panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  el.refresh.addEventListener('click', loadDevices);
  el.select.addEventListener('change', function () { describeSelected(true); });

  el.use.addEventListener('click', async function () {
    if (!el.select.value) return;
    el.use.disabled = true;
    var envelope = await post('/audio/devices/select', { fingerprint: el.select.value });
    el.use.disabled = false;
    if (!envelope.ok) {
      el.evidence.textContent = 'Gagal memilih perangkat: ' + errorText(envelope);
      return;
    }
    await runPreflight();
  });

  el.calibrate.addEventListener('click', calibrate);
  el.preflightBtn.addEventListener('click', runPreflight);

  el.start.addEventListener('click', async function () {
    // Disable before the first await, not inside act(): preflight is an async
    // round trip, and a second click landing during it would post Start twice.
    // The service refuses the duplicate anyway, but the button should not
    // encourage it.
    if (el.start.disabled || startInFlight) return;
    startInFlight = true;
    el.start.disabled = true;
    try {
      var ready = await runPreflight();
      if (!ready) return;
      // No meeting id is sent: the backend creates a draft meeting for this
      // recording. Asking the operator for an internal database id would be
      // unanswerable on a fresh install, where no meeting row exists yet.
      var title = el.meetingTitle ? el.meetingTitle.value.trim() : '';
      await act(el.start, '/audio/recordings/start', { meeting_title: title }, 'Start');
    } finally {
      startInFlight = false;
      // renderStatus() is the authority on the buttons and has already run for a
      // successful start. Only revive Start when nothing is recording -- otherwise
      // this would re-enable it mid-capture.
      await refreshButtons();
    }
  });

  el.pause.addEventListener('click', function () {
    act(el.pause, '/audio/recordings/pause', {}, 'Pause');
  });
  el.resume.addEventListener('click', function () {
    act(el.resume, '/audio/recordings/resume', {}, 'Resume');
  });
  el.stop.addEventListener('click', function () {
    if (!window.confirm('Hentikan perekaman dan finalisasi seluruh chunk?')) return;
    act(el.stop, '/audio/recordings/stop', {}, 'Stop');
  });

  el.verify.addEventListener('click', async function () {
    if (!currentUuid) return;
    el.verify.disabled = true;
    var envelope = await get('/audio/recordings/' + currentUuid + '/verify');
    el.verify.disabled = false;
    if (!envelope.ok) {
      el.recovery.textContent = 'Verifikasi gagal: ' + errorText(envelope);
      return;
    }
    var report = envelope.data;
    el.recovery.textContent = (report.ok ? 'Manifest terverifikasi: ' : 'MASALAH: ') +
      report.verified_chunks + '/' + report.chunk_count + ' chunk, ' +
      report.total_frames + ' frame, chain ' +
      String(report.chain_sha256).slice(0, 12) +
      (report.problems && report.problems.length ? ' — ' + report.problems.join('; ') : '') +
      (report.database_mismatches && report.database_mismatches.length
        ? ' — DB: ' + report.database_mismatches.join('; ') : '');
  });

  async function refreshRecovery() {
    var envelope = await get('/audio/recovery/pending');
    if (!envelope.ok) return;
    var count = envelope.data.pending_count;
    el.recovery.textContent = count
      ? count + ' perekaman terputus menunggu pemulihan.'
      : 'Tidak ada perekaman terputus.';
  }

  el.recover.addEventListener('click', async function () {
    el.recover.disabled = true;
    var envelope = await post('/audio/recovery/run', {});
    el.recover.disabled = false;
    if (!envelope.ok) {
      el.recovery.textContent = 'Pemulihan gagal: ' + errorText(envelope);
      return;
    }
    var data = envelope.data;
    el.recovery.textContent = 'Dipindai ' + data.scanned + ' perekaman; ' +
      data.recovered_chunks + ' chunk dipulihkan, ' +
      data.quarantined_chunks + ' dikarantina sebagai bukti.';
  });
})();
