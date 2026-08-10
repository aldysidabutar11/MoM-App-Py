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

  /* The bar has to move WHILE the operator is speaking.

     Before this it only updated once, when the ten-second test finished, so somebody
     saying "halo halo halo" watched a motionless bar and concluded the microphone was
     dead. It was not: the same test measured -45 dBFS with both channels active. A
     meter that cannot move is indistinguishable from a microphone that cannot hear,
     and the operator has no way to tell which they are looking at.

     `/audio/level` is a plain dict read, so polling it five times a second costs
     nothing and opens nothing. It reports `active: false` the moment the microphone
     closes, which is what stops the bar freezing on somebody's last word and
     pretending to still be live. */
  var meterTimer = null;

  function pollMeter() {
    meterTimer = window.setTimeout(function () {
      meterTimer = null;
      get('/audio/level').then(function (envelope) {
        var level = envelope && envelope.ok ? envelope.data : null;
        if (level && level.active && level.rms_dbfs !== null) {
          paintMeter(level.rms_dbfs, level.peak_dbfs);
          pollMeter();
        } else if (level && level.active) {
          pollMeter();
        }
      });
    }, 200);
  }

  function stopMeter() {
    if (meterTimer !== null) {
      window.clearTimeout(meterTimer);
      meterTimer = null;
    }
  }

  /* dBFS to a 0-100 width. -60 is the bottom of the scale rather than -96: the quiet
     end of a real room sits around -55, and a scale that starts at the noise floor
     spends most of its length on silence and barely twitches for speech. */
  function paintMeter(rms, peak) {
    var scale = function (db) {
      return Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    };
    el.rms.style.width = scale(rms) + '%';
    if (peak !== null && peak !== undefined) {
      el.peak.style.left = scale(peak) + '%';
    }
  }

  async function calibrate() {
    el.calibrate.disabled = true;
    el.levelAdvice.textContent = 'Membuka microphone. Bicaralah normal — batang di atas '
      + 'akan bergerak mengikuti suara Anda.';
    pollMeter();
    var envelope = await post('/audio/calibrate', {});
    stopMeter();
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
    await loadDevices();
    await runPreflight();
    await refreshRecovery();
    if (pollTimer === null) poll();
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


  /* ---------------------------------------------------------------- voice to text

     The verification an operator actually needs before a meeting. A level meter proves
     the microphone is delivering *sound*; it says nothing about whether that sound
     becomes the right words. Somebody responsible for a minute needs to see a sentence
     they just spoke appear correctly before they trust ninety minutes of it.

     Three things this panel must never do, each learned the hard way on this project:

     * Sit still while the microphone is open. A motionless bar is indistinguishable
       from a dead microphone, and that mistake cost a day.
     * Show invented text. Whisper answers noise with fluent sentences, so text that
       failed the filters is discarded -- and *counted*, because silently discarding it
       recreates the same "is this thing broken?" confusion from the other direction.
     * Let itself be read as the transcript. It is a preview from four-second windows
       with no second pass; the stored transcript comes from the master audio afterwards.
  */

  var voice = {
    card: document.getElementById('voice-card'),
    pill: document.getElementById('voice-pill'),
    start: document.getElementById('voice-start'),
    hint: document.getElementById('voice-hint'),
    rms: document.getElementById('voice-rms'),
    peak: document.getElementById('voice-peak'),
    output: document.getElementById('voice-output'),
    placeholder: document.getElementById('voice-placeholder'),
    final: document.getElementById('voice-final'),
    finalText: document.getElementById('voice-final-text'),
    stats: document.getElementById('voice-stats'),
    verdict: document.getElementById('voice-verdict'),
    note: document.getElementById('voice-note')
  };

  var voiceTimer = null;
  var voiceSeen = 0;

  /* Local, not borrowed. `show()` lives in a different module's closure, and calling it
     from here threw a ReferenceError on the first line of the handler -- so the button
     disabled itself and then nothing happened at all, which is indistinguishable from a
     dead button. Every helper this panel uses is defined in this scope. */
  function voiceShow(node, visible) {
    if (node) node.hidden = !visible;
  }

  function voiceScale(db) {
    if (db === null || db === undefined) return 0;
    return Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
  }

  function voiceAppend(segments) {
    /* Segments only ever grow, so redraw from where we stopped rather than rebuilding:
       the operator is reading this while it updates, and replacing the whole block
       every 400 ms makes it impossible to follow. */
    for (var i = voiceSeen; i < segments.length; i += 1) {
      var seg = segments[i];
      if (!seg.text) continue;
      var line = document.createElement('p');
      line.className = 'voice-line';
      var stamp = document.createElement('span');
      stamp.className = 'voice-stamp';
      var total = Math.floor((seg.started_ms || 0) / 1000);
      stamp.textContent =
        String(Math.floor(total / 60)).padStart(2, '0') + ':' +
        String(total % 60).padStart(2, '0');
      line.appendChild(stamp);
      line.appendChild(document.createTextNode(seg.text));
      voice.output.appendChild(line);
    }
    if (segments.length > voiceSeen) {
      voiceShow(voice.placeholder, false);
      voice.output.scrollTop = voice.output.scrollHeight;
    }
    /* Monotonic, and that is the whole point: this counts what is *on screen*, and a
       line already drawn cannot become undrawn. Assigning `segments.length` directly
       printed every preview line twice, and the path there is not obvious.

       `/audio/live` answers with an empty list once the transcriber has been cleared,
       which happens while the accurate pass is still running. The last poll in flight
       therefore came back with nothing, reset the counter to zero, and the final
       response -- carrying the full list -- then redrew all of it underneath the copy
       already there. Any shrinking answer would do the same. */
    voiceSeen = Math.max(voiceSeen, segments.length);
  }

  function voicePoll() {
    voiceTimer = window.setTimeout(function () {
      voiceTimer = null;
      Promise.all([get('/audio/level'), get('/audio/live')]).then(function (both) {
        var level = both[0] && both[0].ok ? both[0].data : null;
        var live = both[1] && both[1].ok ? both[1].data : null;
        if (level && level.active) {
          voice.rms.style.width = voiceScale(level.rms_dbfs) + '%';
          voice.peak.style.left = voiceScale(level.peak_dbfs) + '%';
        }
        if (live) voiceAppend(live.segments || []);
        if (level && level.active) {
          voicePoll();
        } else {
          /* The microphone has closed but the request has not come back: the accurate
             pass is running over the whole recording, and it takes several seconds. Say
             so. A panel that goes silent here reads as one that has hung, and the
             operator stops the test before the result they came for arrives. */
          voice.pill.dataset.state = 'live';
          voice.pill.textContent = 'MEMBACA ULANG';
          voice.hint.textContent = 'Microphone sudah ditutup. Model akurat sedang '
            + 'membaca ulang seluruh rekaman -- tunggu sebentar.';
          voice.rms.style.width = '0%';
        }
      });
    }, 400);
  }

  function voiceStopPolling() {
    if (voiceTimer !== null) {
      window.clearTimeout(voiceTimer);
      voiceTimer = null;
    }
  }

  async function runVoiceCheck() {
    voice.start.disabled = true;
    voiceSeen = 0;
    voice.output.textContent = '';
    voice.output.appendChild(voice.placeholder);
    voiceShow(voice.placeholder, true);
    voiceShow(voice.note, false);
    voice.stats.textContent = '';
    voice.verdict.textContent = '';
    voiceShow(voice.final, false);
    voice.pill.dataset.state = 'live';
    voice.pill.textContent = 'MENDENGAR';
    voice.hint.textContent = 'Microphone terbuka. Bicaralah normal.';
    voicePoll();

    var envelope;
    try {
      envelope = await post('/audio/voice-check', { seconds: 30 });
    } catch (err) {
      envelope = { ok: false, error: String(err) };
    } finally {
      /* The button is re-enabled here and nowhere else. Anything that throws between
         disabling it and this point leaves it dead for the rest of the session, and a
         dead button looks exactly like a feature that was never wired up -- which is
         the precise failure this panel already shipped with once. */
      voiceStopPolling();
      voice.start.disabled = false;
      voice.rms.style.width = '0%';
      voice.hint.textContent = 'Microphone sudah ditutup. Tidak ada yang disimpan.';
    }

    if (!envelope.ok) {
      voice.pill.dataset.state = 'fail';
      voice.pill.textContent = 'GAGAL';
      voice.verdict.textContent = errorText(envelope);
      return;
    }

    var data = envelope.data;
    var tr = data.transcript || {};
    voiceAppend(tr.segments || []);

    /* Why the accurate pass produced no text. Anything not listed is an exception type
       from Python and is shown as-is, which is still better than a blank box. */
    var VOICE_FINAL_REASON = {
      NO_AUDIO: 'Tidak ada audio yang sampai ke pemeriksa. Periksa pilihan mikrofon.',
      TOO_SHORT: 'Rekaman terlalu pendek untuk dibaca ulang. Coba lagi dan bicara '
        + 'sedikit lebih lama.',
      NO_SPEECH: 'Model membaca seluruh rekaman dan menilainya sebagai suara ruangan, '
        + 'bukan ucapan. Kalau Anda memang berbicara: dekatkan mikrofon, atau naikkan '
        + 'level input di Windows.',
      EMPTY_DECODE: 'Model berjalan tetapi tidak menghasilkan kata apa pun. Suaranya '
        + 'kemungkinan terlalu pelan atau tertutup derau.',
      NO_MODEL: 'Model pengenal suara belum terpasang di komputer ini, jadi '
        + 'hanya level suara yang bisa diperiksa.'
    };

    /* What the level measurement means, and what to do about it. Every one of these
       names the setting to change, because a warning that does not is a warning the
       operator learns to ignore. */
    var VOICE_VERDICT = {
      NO_SIGNAL: 'Tidak ada sinyal sama sekali. Pastikan mikrofon yang benar dipilih, '
        + 'tidak dalam keadaan mute, dan Windows sudah mengizinkan akses mikrofon.',
      TOO_QUIET: 'Suara terlalu pelan. Dekatkan mikrofon ke tengah meja, atau naikkan '
        + 'level input di Windows: Settings > System > Sound > Input.',
      GOOD: 'Level suara sudah pas.',
      TOO_LOUD: 'Suara hampir mentok dan kata-kata mulai pecah, yang membuat teksnya '
        + 'salah. Turunkan level input di Windows: Settings > System > Sound > Input.',
      CLIPPING: 'Suara pecah (clipping) dan audionya rusak permanen -- teks pasti '
        + 'banyak salah. Turunkan level input di Windows: Settings > System > Sound > '
        + 'Input, lalu tes lagi.'
    };

    /* The accurate pass is what the operator should judge, so it decides the verdict
       and gets its own block. The streaming lines above it are reassurance during the
       test, not the answer -- they come from the small model and are cut into
       six-second pieces. */
    var finalText = (data.final_text || '').trim();
    if (finalText) {
      voice.finalText.textContent = finalText;
      voiceShow(voice.final, true);
    } else if (data.final_error) {
      /* Never an empty box. Each reason is a different thing for the operator to do. */
      voice.finalText.textContent = VOICE_FINAL_REASON[data.final_error]
        || ('Lintasan akurat gagal (' + data.final_error + '). '
            + 'Teks cepat di atas tetap berlaku.');
      voiceShow(voice.final, true);
    }

    var previewLines = (tr.segments || []).length;
    var spoken = finalText ? 1 : previewLines;
    voice.pill.dataset.state = spoken ? 'ok' : 'warn';
    voice.pill.textContent = spoken ? 'ADA TEKS' : 'TIDAK ADA TEKS';

    setKv(voice.stats, [
      /* Not "sentences read": the accurate pass returns one block of text, so that
         label reported 1 next to three visible lines and read like a fault. These two
         count different things and are named for what they are. */
      ['Baris pratinjau', String(previewLines)],
      ['Bagian diproses', String(tr.decoded_windows || 0)],
      ['Dibuang (tidak jelas)', String(tr.filtered_windows || 0)],
      ['Blok audio hilang', String(tr.dropped_blocks || 0)],
      ['Level', data.levels.rms_dbfs + ' dBFS rata-rata, puncak ' + data.levels.peak_dbfs]
    ]);
    /* The service's advice is English -- it is written for the CLI and the logs. The
       operator of this panel is not the person who reads those, so the verdict is said
       in the language the rest of the interface is written in, and the English is kept
       as the fallback rather than dropped. */
    voice.verdict.textContent = VOICE_VERDICT[data.verdict] || data.advice || '';

    /* Saying nothing here is the failure mode this whole panel exists to prevent. If
       the operator spoke and no text appeared, they must be told which of the three
       reasons applies -- otherwise they conclude the microphone is broken, which is
       exactly the wrong conclusion and exactly the one already reached once. */
    if (!spoken) {
      if (!data.model_available) {
        voice.note.textContent = 'Model transkripsi belum terpasang, jadi teks tidak '
          + 'bisa dibuat. Level suara di atas tetap menunjukkan microphone berfungsi. '
          + 'Jalankan: python -m mom_igd asr provision all';
      } else if (tr.filtered_windows) {
        voice.note.textContent = 'Suara terdengar, tetapi ' + tr.filtered_windows
          + ' bagian dibuang karena model sendiri tidak yakin itu ucapan. Biasanya '
          + 'karena terlalu jauh dari microphone, terlalu pelan, atau ruangan berisik. '
          + 'Coba lagi lebih dekat dan lebih jelas.';
      } else {
        voice.note.textContent = 'Tidak ada suara yang terdengar sama sekali. Periksa '
          + 'batang level di atas: jika tidak bergerak saat Anda bicara, microphone '
          + 'belum menangkap suara.';
      }
      voiceShow(voice.note, true);
    } else {
      voice.note.textContent = 'Bandingkan "Hasil akhir" dengan kalimat yang Anda ucapkan. '
        + 'Jika artinya sudah tepat, microphone dan pengaturan suara sudah siap dipakai '
        + 'untuk rapat. Teks ini hanya pratinjau dan tidak disimpan.';
      voiceShow(voice.note, true);
    }
  }

  voice.start.addEventListener('click', function () { runVoiceCheck(); });

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

/* ==========================================================================
   PHASE 3 -- participants, biometric consent, voice enrollment.

   THE ARCHITECTURAL RULE THIS BLOCK EXISTS TO HONOUR: this page never touches
   audio. There is no getUserMedia, no AudioContext, no MediaRecorder and no PCM
   anywhere below. Voice capture runs inside the Python process
   (mom_igd/enrollment/capture.py); the page sends "record a sample" and receives
   levels, a duration and a quality verdict. A biometric sample never becomes a
   base64 blob in a request body and never enters browser memory. See ADR-0012.

   Every value that came from the backend is rendered with textContent or
   createTextNode. There is no innerHTML in this block: a participant's display
   name is operator-supplied text, and it is the obvious injection vector here.
   ========================================================================== */
(function () {
  'use strict';

  var READINESS_POLL_MS = 1500;   /* only while the wizard is open */
  var pollTimer = null;
  var pollStopped = false;
  var busy = false;               /* single in-flight guard for every action */
  var participants = [];
  var selected = null;            /* the participant object, never a DOM id */
  var consentBundle = null;
  var pendingConsentUuid = null;

  /* The revoke dialog owns its own state, deliberately not the global `busy`
     flag. `busy` is shared by every action button, so a dialog that read it
     could not tell "another action is running" from "my own submit is running",
     and its Batal button would go dead for reasons unrelated to the dialog. */
  var revokeTarget = null;    /* {uuid, name, role} or null */
  var revokeSubmitting = false;
  var revokeReturnFocus = null;

  var UUID_RE =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

  /* Meeting roster capacity. `null` until the backend has answered; the Save
     button stays disabled until then so a stale ceiling can never be sent. */
  var rosterMeetingUuid = null;
  var rosterState = null;     /* the last roster response, verbatim */
  var rosterFull = false;     /* derived from the server's own counts, never guessed */

  /* Directory search results are paged. Fifty participants is the safety ceiling for
     one roster, not for the directory, which has no limit -- so the candidate list
     must stay bounded and searchable rather than rendering everyone. */
  var CANDIDATE_PAGE = 25;

  var el = {
    open: document.getElementById('open-participants-btn'),
    panel: document.getElementById('participants-panel'),
    countPill: document.getElementById('participant-count-pill'),
    search: document.getElementById('participant-search'),
    refresh: document.getElementById('refresh-participants-btn'),
    newBtn: document.getElementById('new-participant-btn'),
    rows: document.getElementById('participant-rows'),
    empty: document.getElementById('participant-empty'),

    formCard: document.getElementById('participant-form-card'),
    formTitle: document.getElementById('participant-form-title'),
    name: document.getElementById('participant-name'),
    role: document.getElementById('participant-role'),
    save: document.getElementById('save-participant-btn'),
    cancelForm: document.getElementById('cancel-participant-btn'),
    formError: document.getElementById('participant-form-error'),

    enrollCard: document.getElementById('enrollment-card'),
    enrollPill: document.getElementById('enrollment-pill'),
    enrollWho: document.getElementById('enrollment-who'),
    readiness: document.getElementById('readiness-detail'),
    modelNotice: document.getElementById('model-unavailable-notice'),
    modelDetail: document.getElementById('model-unavailable-detail'),
    refreshReady: document.getElementById('refresh-readiness-btn'),
    startEnroll: document.getElementById('start-enrollment-btn'),
    captureSample: document.getElementById('capture-sample-btn'),
    finalize: document.getElementById('finalize-enrollment-btn'),
    cancelEnroll: document.getElementById('cancel-enrollment-btn'),
    progress: document.getElementById('enrollment-progress'),
    meter: document.getElementById('enrollment-meter'),
    meterRms: document.getElementById('enrollment-rms'),
    meterPeak: document.getElementById('enrollment-peak'),
    levels: document.getElementById('enrollment-levels'),
    sampleList: document.getElementById('sample-list'),
    enrollWarn: document.getElementById('enrollment-warning'),

    vpCard: document.getElementById('voiceprint-card'),
    vpDetail: document.getElementById('voiceprint-detail'),
    vpVerify: document.getElementById('verify-voiceprint-btn'),
    vpRevoke: document.getElementById('revoke-consent-btn'),
    vpResult: document.getElementById('voiceprint-result'),

    consentBackdrop: document.getElementById('consent-backdrop'),
    consentMeta: document.getElementById('consent-meta'),
    consentDraft: document.getElementById('consent-draft-warning'),
    consentText: document.getElementById('consent-text'),
    consentAgree: document.getElementById('consent-agree'),
    consentAgreeLabel: document.getElementById('consent-agree-label'),
    consentConfirm: document.getElementById('consent-confirm-btn'),
    consentCancel: document.getElementById('consent-cancel-btn'),
    consentError: document.getElementById('consent-error'),

    revokeBackdrop: document.getElementById('revoke-backdrop'),
    revokeWho: document.getElementById('revoke-who'),
    revokeWhoName: document.getElementById('revoke-who-name'),
    revokeWhoRole: document.getElementById('revoke-who-role'),
    revokeConfirm: document.getElementById('revoke-confirm-btn'),
    revokeCancel: document.getElementById('revoke-cancel-btn'),
    revokeError: document.getElementById('revoke-error'),

    rosterCard: document.getElementById('roster-card'),
    rosterSelect: document.getElementById('roster-meeting-select'),
    rosterPill: document.getElementById('roster-count-pill'),
    rosterSlots: document.getElementById('roster-slots'),
    rosterRows: document.getElementById('roster-rows'),
    rosterEmpty: document.getElementById('roster-empty'),
    rosterAddSearch: document.getElementById('roster-add-search'),
    rosterAddSearchBtn: document.getElementById('roster-add-search-btn'),
    rosterCandidates: document.getElementById('roster-candidates'),
    rosterCandidatesNote: document.getElementById('roster-candidates-note'),
    rosterMemberError: document.getElementById('roster-member-error'),
    rosterMemberResult: document.getElementById('roster-member-result'),
    rosterLabel: document.getElementById('roster-meeting-label'),
    rosterCapacity: document.getElementById('roster-capacity-input'),
    rosterSave: document.getElementById('roster-capacity-save-btn'),
    rosterRange: document.getElementById('roster-capacity-range'),
    rosterWarning: document.getElementById('roster-capacity-warning'),
    rosterError: document.getElementById('roster-capacity-error'),
    rosterResult: document.getElementById('roster-capacity-result')
  };

  if (!el.panel) return;   /* the Phase 3 markup is absent; nothing to wire */

  /* ------------------------------------------------------------- bridge -- */

  function api() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  function unavailable() {
    return {
      ok: false,
      status: 0,
      error: 'Panel ini hanya berfungsi di dalam shell desktop (butuh session token).'
    };
  }

  async function httpGet(path, query) {
    var a = api();
    return a ? a.api_get(path, query || null) : unavailable();
  }

  async function httpPost(path, payload) {
    var a = api();
    return a ? a.api_post(path, payload || null) : unavailable();
  }

  async function httpPatch(path, payload) {
    var a = api();
    return a ? a.api_patch(path, payload || null) : unavailable();
  }

  async function httpDelete(path) {
    var a = api();
    return a ? a.api_delete(path) : unavailable();
  }

  function failureText(envelope) {
    if (!envelope) return 'kesalahan tidak diketahui';
    var e = envelope.error;
    if (typeof e === 'string') return e;
    if (e && typeof e === 'object') {
      /* Enrollment errors arrive as {reason, message}. */
      if (e.message) return (e.reason ? '[' + e.reason + '] ' : '') + e.message;
      if (e.detail) return String(e.detail);
    }
    return 'HTTP ' + envelope.status;
  }

  /* --------------------------------------------------------- safe render -- */

  function textCell(value) {
    var td = document.createElement('td');
    td.textContent = (value === null || value === undefined || value === '')
      ? '—' : String(value);
    return td;
  }

  function stateBadge(label, state) {
    var span = document.createElement('span');
    span.className = 'state-badge';
    span.dataset.state = state;   /* never interpolated into a class name */
    span.textContent = label;
    return span;
  }

  function setKvSafe(node, pairs) {
    node.textContent = '';
    pairs.forEach(function (pair) {
      var dt = document.createElement('dt');
      dt.textContent = pair[0];
      var dd = document.createElement('dd');
      dd.textContent = (pair[1] === null || pair[1] === undefined)
        ? '—' : String(pair[1]);
      node.appendChild(dt);
      node.appendChild(dd);
    });
  }

  function show(node, visible) {
    if (node) node.hidden = !visible;
  }

  function makeButton(label, handler) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn';
    b.textContent = label;
    b.addEventListener('click', handler);
    return b;
  }

  /* ------------------------------------------------------------- guards -- */

  /* The two dialog confirm buttons are NOT in this list, and must never be added
     to it. Each dialog gates its own confirm button on its own precondition --
     consent on the checkbox, revoke on having a valid selected participant -- and
     a blanket `disabled = false` here would re-enable them regardless. That is
     how a "Ya, cabut persetujuan" button becomes clickable with nothing
     selected. Dialog state is re-applied by syncDialogButtons(). */
  function setActionsDisabled(disabled) {
    [
      el.refresh, el.newBtn, el.save, el.refreshReady, el.startEnroll,
      el.captureSample, el.finalize, el.cancelEnroll, el.vpVerify, el.vpRevoke,
      el.rosterSave
    ].forEach(function (b) {
      if (b) b.disabled = disabled;
    });
  }

  function syncDialogButtons() {
    if (el.consentConfirm) {
      el.consentConfirm.disabled =
        !pendingConsentUuid || !consentBundle || !el.consentAgree.checked;
    }
    if (el.revokeConfirm) {
      el.revokeConfirm.disabled = !revokeTarget || revokeSubmitting;
    }
    /* Batal is usable whenever a submit is not in flight -- including before any
       request has ever been made. */
    if (el.revokeCancel) el.revokeCancel.disabled = revokeSubmitting;
  }

  /* Runs `fn` with every action button disabled, so a double click cannot
     submit the same thing twice. */
  async function once(fn) {
    if (busy) return null;
    busy = true;
    setActionsDisabled(true);
    try {
      return await fn();
    } finally {
      busy = false;
      setActionsDisabled(false);
      syncDialogButtons();
      await refreshEnrollmentButtons();
    }
  }

  /* -------------------------------------------------------- participants -- */

  function consentBadge(entry) {
    var c = entry.consent || {};
    if (c.active) return stateBadge('AKTIF', 'ok');
    if (c.action === 'REVOKED') return stateBadge('DICABUT', 'fail');
    return stateBadge('BELUM ADA', 'unknown');
  }

  function voiceprintBadge(entry) {
    var v = entry.voiceprint;
    if (!v) return stateBadge('BELUM ADA', 'unknown');
    if (v.status === 'ACTIVE') return stateBadge('ACTIVE', 'ok');
    if (v.status === 'DEVELOPMENT_ONLY') return stateBadge('DEV ONLY', 'warn');
    if (v.status === 'RE_ENROLL_REQUIRED') return stateBadge('RE-ENROLL', 'warn');
    return stateBadge(String(v.status), 'fail');
  }

  function renderParticipants(listing) {
    participants = listing.participants || [];
    el.countPill.textContent = String(listing.total || 0);
    el.countPill.dataset.state = listing.total ? 'ok' : 'unknown';
    el.rows.textContent = '';
    show(el.empty, participants.length === 0);
    if (participants.length === 0) {
      el.empty.textContent = 'Belum ada peserta terdaftar.';
    }

    participants.forEach(function (entry) {
      var tr = document.createElement('tr');
      tr.appendChild(textCell(entry.display_name));
      tr.appendChild(textCell(entry.role));

      var statusTd = document.createElement('td');
      statusTd.appendChild(entry.is_active
        ? stateBadge('AKTIF', 'ok') : stateBadge('NONAKTIF', 'unknown'));
      tr.appendChild(statusTd);

      var consentTd = document.createElement('td');
      consentTd.appendChild(consentBadge(entry));
      tr.appendChild(consentTd);

      var vpTd = document.createElement('td');
      vpTd.appendChild(voiceprintBadge(entry));
      tr.appendChild(vpTd);

      var actions = document.createElement('td');
      actions.className = 'actions';
      actions.appendChild(makeButton('Enrollment', function () {
        openEnrollment(entry);
      }));
      actions.appendChild(makeButton('Edit', function () { openForm(entry); }));
      actions.appendChild(makeButton(
        entry.is_active ? 'Nonaktifkan' : 'Aktifkan',
        function () { once(function () { return toggleActive(entry); }); }
      ));
      tr.appendChild(actions);
      el.rows.appendChild(tr);
    });
  }

  async function loadParticipants() {
    var query = { limit: 100 };
    var term = el.search.value.trim();
    if (term) query.search = term;
    var envelope = await httpGet('/enrollment/participants', query);
    if (!envelope.ok) {
      show(el.empty, true);
      el.empty.textContent = 'Gagal memuat peserta: ' + failureText(envelope);
      return;
    }
    renderParticipants(envelope.data);
    /* Keep the selected participant's decorated copy fresh. */
    if (selected) {
      participants.forEach(function (entry) {
        if (entry.uuid === selected.uuid) selected = entry;
      });
    }
  }

  function openForm(entry) {
    selected = entry || null;
    el.formTitle.textContent = entry ? 'Edit peserta' : 'Tambah peserta';
    el.name.value = entry ? entry.display_name : '';
    el.role.value = (entry && entry.role) ? entry.role : '';
    show(el.formError, false);
    show(el.formCard, true);
    el.name.focus();
  }

  async function saveParticipant() {
    var name = el.name.value.trim();
    if (!name) {
      show(el.formError, true);
      el.formError.textContent = 'Nama tampilan wajib diisi.';
      return;
    }
    var body = { display_name: name, role: el.role.value.trim() || null };
    var envelope = selected
      ? await httpPatch('/enrollment/participants/' + selected.uuid, body)
      : await httpPost('/enrollment/participants', body);
    if (!envelope.ok) {
      show(el.formError, true);
      el.formError.textContent = 'Gagal menyimpan: ' + failureText(envelope);
      return;
    }
    show(el.formCard, false);
    selected = null;
    await loadParticipants();
  }

  async function toggleActive(entry) {
    var suffix = entry.is_active ? '/deactivate' : '/reactivate';
    var envelope = await httpPost(
      '/enrollment/participants/' + entry.uuid + suffix, {}
    );
    if (!envelope.ok) {
      show(el.empty, true);
      el.empty.textContent = 'Gagal mengubah status: ' + failureText(envelope);
      return;
    }
    await loadParticipants();
  }

  /* ------------------------------------------------------------- consent -- */

  async function openConsentDialog(entry) {
    var envelope = await httpGet('/enrollment/consent/text');
    if (!envelope.ok) {
      show(el.enrollWarn, true);
      el.enrollWarn.textContent =
        'Tidak dapat memuat teks persetujuan: ' + failureText(envelope);
      return;
    }
    consentBundle = envelope.data;
    pendingConsentUuid = entry.uuid;

    setKvSafe(el.consentMeta, [
      ['peserta', entry.display_name],
      ['versi', consentBundle.version],
      ['bahasa', consentBundle.language],
      ['tujuan', consentBundle.purpose],
      ['sha-256 teks', String(consentBundle.text_sha256).slice(0, 16) + '…']
    ]);
    el.consentDraft.textContent = consentBundle.review_pending
      ? consentBundle.review_note : '';
    show(el.consentDraft, Boolean(consentBundle.review_pending));
    el.consentText.textContent = consentBundle.text;
    el.consentAgreeLabel.textContent =
      'Peserta telah membaca dan memahami keterangan di atas, dan memberikan ' +
      'persetujuan untuk pembuatan serta penyimpanan templat biometrik suaranya ' +
      'untuk tujuan tersebut saja.';
    /* Never pre-checked: a prechecked box is not consent. */
    el.consentAgree.checked = false;
    el.consentConfirm.disabled = true;
    show(el.consentError, false);
    show(el.consentBackdrop, true);
  }

  function closeConsentDialog() {
    show(el.consentBackdrop, false);
    pendingConsentUuid = null;
    el.consentAgree.checked = false;
    el.consentConfirm.disabled = true;
  }

  async function confirmConsent() {
    if (!pendingConsentUuid || !consentBundle || !el.consentAgree.checked) return;
    var envelope = await httpPost(
      '/enrollment/participants/' + pendingConsentUuid + '/consent/grant',
      {
        acknowledged_text_sha256: consentBundle.text_sha256,
        confirmed_by_participant: true
      }
    );
    if (!envelope.ok) {
      show(el.consentError, true);
      el.consentError.textContent =
        'Gagal mencatat persetujuan: ' + failureText(envelope);
      return;
    }
    closeConsentDialog();
    await loadParticipants();
    if (selected) await refreshReadiness();
  }

  function revokeError(message) {
    show(el.revokeError, true);
    el.revokeError.textContent = message;
  }

  /* `trigger` is the element that opened the dialog, so focus can go back where
     the operator left it. Identity is rendered for the human; the UUID is what
     the request uses. A name is never sent, and never trusted as an identifier. */
  function openRevokeDialog(entry, trigger) {
    revokeReturnFocus = trigger || null;
    revokeSubmitting = false;
    show(el.revokeError, false);

    var uuid = entry && typeof entry.uuid === 'string' ? entry.uuid.toLowerCase() : '';
    if (!UUID_RE.test(uuid)) {
      /* Refuse rather than guess. Revoking the wrong person's consent destroys a
         voiceprint that cannot be restored, so an unidentified target is fatal. */
      revokeTarget = null;
      el.revokeWhoName.textContent = '(peserta tidak dikenali)';
      el.revokeWhoRole.textContent = '';
      revokeError(
        'Peserta yang dipilih tidak dapat dikenali, jadi tidak ada yang dicabut. ' +
        'Tutup dialog ini, muat ulang daftar peserta, lalu pilih peserta kembali.'
      );
    } else {
      revokeTarget = {
        uuid: uuid,
        name: String(entry.display_name || '(tanpa nama)'),
        role: entry.role ? String(entry.role) : ''
      };
      el.revokeWhoName.textContent = revokeTarget.name;
      el.revokeWhoRole.textContent = revokeTarget.role
        ? ' — ' + revokeTarget.role
        : '';
    }

    syncDialogButtons();
    show(el.revokeBackdrop, true);
    /* Focus lands inside the dialog. Batal first, not the destructive button:
       a stray Enter must not revoke anyone's consent. */
    if (el.revokeCancel) el.revokeCancel.focus();
  }

  function closeRevokeDialog() {
    if (revokeSubmitting) return;          /* never close mid-request */
    show(el.revokeBackdrop, false);
    show(el.revokeError, false);
    revokeTarget = null;
    el.revokeWhoName.textContent = '';
    el.revokeWhoRole.textContent = '';
    syncDialogButtons();
    var back = revokeReturnFocus;
    revokeReturnFocus = null;
    if (back && typeof back.focus === 'function' && !back.disabled) back.focus();
  }

  async function submitRevoke() {
    /* Three separate guards, because they fail for different reasons:
       no target  -> nothing to send, and the button should already be disabled;
       submitting -> a second click or a double-click; must not send twice. */
    if (revokeSubmitting) return;
    if (!revokeTarget) {
      revokeError('Tidak ada peserta yang dipilih, jadi tidak ada yang dicabut.');
      syncDialogButtons();
      return;
    }

    var target = revokeTarget;
    revokeSubmitting = true;
    syncDialogButtons();
    var label = el.revokeConfirm.textContent;
    el.revokeConfirm.textContent = 'Mencabut…';
    show(el.revokeError, false);

    try {
      var envelope = await httpPost(
        '/enrollment/participants/' + target.uuid + '/consent/revoke',
        { reason: 'dicabut melalui panel peserta' }
      );
      if (!envelope.ok) {
        /* Stay open so the operator can retry or cancel. failureText() carries a
           reason and a message, never a traceback, path, token or key. */
        revokeError('Gagal mencabut: ' + failureText(envelope));
        return;
      }

      var deletion = (envelope.data && envelope.data.deletion) || {};
      var deleted = (deletion.deleted || []).length;
      var pending = (deletion.delete_pending || []).length;
      var summary;
      if (pending > 0) {
        summary =
          'Persetujuan dicabut. Penghapusan berkas GAGAL untuk ' + pending +
          ' template; template sudah tidak dapat dipakai dan pembersihan akan diulang.';
      } else if (deleted > 0) {
        summary = 'Persetujuan dicabut dan template terenkripsi telah dihapus.';
      } else {
        /* No voiceprint existed. That is a success, not an error, and saying so
           plainly stops an operator hunting for a failure that did not happen. */
        summary =
          'Persetujuan dicabut. Peserta ini belum memiliki template suara, jadi ' +
          'tidak ada berkas yang perlu dihapus.';
      }

      revokeSubmitting = false;            /* allow the close below */
      closeRevokeDialog();
      el.vpResult.textContent = summary;
      await loadParticipants();
      if (selected) await refreshReadiness();
    } finally {
      revokeSubmitting = false;
      el.revokeConfirm.textContent = label;
      syncDialogButtons();
    }
  }

  /* ------------------------------------------------------- roster capacity -- */

  /* Nine is the backward-compatible default, not a limit of the product. The
     ceiling is configuration, and a bigger roster changes who the KNOWN speaker
     candidates are -- never whether audio is recorded. */
  var BASELINE_CAPACITY = 9;

  function renderRoster(data) {
    rosterState = data;
    var count = Number(data.active_count) || 0;
    var capacity = Number(data.capacity) || 0;
    el.rosterPill.textContent = count + ' / ' + capacity;
    el.rosterPill.dataset.state =
      count > capacity ? 'fail' : (count === capacity ? 'warn' : 'ok');

    /* The bounds come from the server, which knows whether this meeting is
       grandfathered above a since-lowered ceiling. Rendering `maximum_capacity`
       here would show a range the meeting does not actually have. */
    var lowest = Number(data.capacity_min_settable);
    var highest = Number(data.capacity_max_settable);
    el.rosterCapacity.min = String(lowest);
    el.rosterCapacity.max = String(highest);
    el.rosterCapacity.value = String(capacity);

    var range = 'Nilai yang diperbolehkan untuk rapat ini: ' + lowest + '–' + highest +
      '. Batas atas adalah pagar keamanan yang dapat dikonfigurasi (saat ini ' +
      data.maximum_capacity + '), bukan pernyataan bahwa ' + data.maximum_capacity +
      ' pembicara sudah terbukti dikenali dengan akurat.';
    if (!data.capacity_changeable) {
      range += ' Kapasitas tidak dapat diubah sekarang karena roster aktif (' +
        count + ') sudah melebihi nilai tertinggi yang boleh disetel.';
    }
    el.rosterRange.textContent = range;

    var notes = [];
    if (data.capacity_above_ceiling && data.capacity_notice) {
      /* Grandfathered: say so plainly rather than pretending the stored value is
         within the configured ceiling. */
      notes.push(data.capacity_notice);
    }
    if (capacity > BASELINE_CAPACITY) {
      notes.push('Kapasitas lebih besar membutuhkan conference microphone dan ' +
        'pengujian ruangan. Menambah roster tidak menjamin akurasi pengenalan suara.');
    }
    el.rosterWarning.textContent = notes.join(' ');
    show(el.rosterWarning, notes.length > 0);

    el.rosterSave.disabled = busy || !data.capacity_changeable;
    renderRosterMembers(data);
  }

  /* ------------------------------------------------- roster membership -- */

  function renderRosterMembers(data) {
    var members = (data.participants || []).filter(function (m) {
      return m.membership_active;
    });
    var capacity = Number(data.capacity) || 0;
    var remaining = Number(data.slots_remaining);
    rosterFull = members.length >= capacity;

    el.rosterSlots.textContent = members.length + ' anggota aktif, ' +
      (isFinite(remaining) ? remaining : 0) + ' kursi tersisa dari kapasitas ' +
      capacity + '.' + (rosterFull ? ' Roster penuh.' : '');

    el.rosterRows.textContent = '';
    show(el.rosterEmpty, members.length === 0);

    members.forEach(function (entry) {
      var tr = document.createElement('tr');
      tr.appendChild(textCell(entry.display_name));
      tr.appendChild(textCell(entry.role));

      var statusTd = document.createElement('td');
      statusTd.appendChild(entry.is_active
        ? stateBadge('AKTIF', 'ok') : stateBadge('NONAKTIF', 'unknown'));
      tr.appendChild(statusTd);

      var consentTd = document.createElement('td');
      consentTd.appendChild(consentBadge(entry));
      tr.appendChild(consentTd);

      var vpTd = document.createElement('td');
      vpTd.appendChild(voiceprintBadge(entry));
      tr.appendChild(vpTd);

      var actions = document.createElement('td');
      actions.className = 'actions';
      actions.appendChild(makeButton('Keluarkan dari roster', function () {
        once(function () { return removeFromRoster(entry); });
      }));
      tr.appendChild(actions);
      el.rosterRows.appendChild(tr);
    });
  }

  /* One request for the whole candidate page. The roster response already carries
     every member's consent and voiceprint state, so nothing here needs a call per
     participant, and nothing polls. */
  async function searchCandidates() {
    if (!rosterMeetingUuid) return;
    show(el.rosterMemberError, false);
    var query = { limit: CANDIDATE_PAGE, include_inactive: false };
    var text = el.rosterAddSearch.value.trim();
    if (text) query.search = text;
    var envelope = await httpGet('/enrollment/participants', query);
    if (!envelope.ok) {
      rosterMemberError('Direktori tidak dapat dimuat: ' + failureText(envelope));
      return;
    }
    var onRoster = {};
    ((rosterState && rosterState.participants) || []).forEach(function (m) {
      if (m.membership_active) onRoster[m.uuid] = true;
    });
    var candidates = (envelope.data.participants || []).filter(function (p) {
      return !onRoster[p.uuid];
    });

    el.rosterCandidates.textContent = '';
    candidates.forEach(function (entry) {
      var li = document.createElement('li');
      li.className = 'candidate-row';
      var name = document.createElement('span');
      name.className = 'candidate-name';
      name.textContent = entry.display_name;
      li.appendChild(name);
      if (entry.role) {
        var role = document.createElement('span');
        role.className = 'muted';
        role.textContent = entry.role;
        li.appendChild(role);
      }
      var add = makeButton('Tambahkan ke roster', function () {
        once(function () { return addToRoster(entry); });
      });
      /* A full roster disables Add up front; the server still answers 409 if the
         page's view is stale, and that path is handled below. */
      add.disabled = rosterFull;
      li.appendChild(add);
      el.rosterCandidates.appendChild(li);
    });

    var total = Number(envelope.data.total) || 0;
    var shown = candidates.length;
    el.rosterCandidatesNote.textContent = shown === 0
      ? (total === 0
        ? 'Tidak ada peserta aktif yang cocok. Tambahkan peserta baru di direktori.'
        : 'Semua peserta yang cocok sudah ada di roster ini.')
      : (shown + ' kandidat ditampilkan dari ' + total + ' peserta aktif yang cocok' +
         (total > CANDIDATE_PAGE
           ? '. Persempit pencarian untuk melihat sisanya.' : '.'));
  }

  function rosterMemberError(message) {
    show(el.rosterMemberError, true);
    el.rosterMemberError.textContent = message;
  }

  async function addToRoster(entry) {
    if (!rosterMeetingUuid) return;
    show(el.rosterMemberError, false);
    var envelope = await httpPost(
      '/enrollment/meetings/' + rosterMeetingUuid + '/participants',
      { participant_uuid: entry.uuid }
    );
    if (!envelope.ok) {
      rosterMemberError('Tidak dapat menambahkan: ' + failureText(envelope));
      /* Refresh anyway: a 409 usually means this page's counter is stale. */
      await loadRoster();
      await searchCandidates();
      return;
    }
    el.rosterMemberResult.textContent = 'Ditambahkan ke roster. Sekarang ' +
      envelope.data.active_count + ' / ' + envelope.data.capacity + '.';
    renderRoster(await refreshedRoster(envelope.data));
    await searchCandidates();
    await loadMeetings();
  }

  async function removeFromRoster(entry) {
    if (!rosterMeetingUuid) return;
    show(el.rosterMemberError, false);
    var envelope = await httpDelete(
      '/enrollment/meetings/' + rosterMeetingUuid + '/participants/' + entry.uuid
    );
    if (!envelope.ok) {
      rosterMemberError('Tidak dapat mengeluarkan: ' + failureText(envelope));
      await loadRoster();
      return;
    }
    el.rosterMemberResult.textContent = 'Dikeluarkan dari roster. Sekarang ' +
      envelope.data.active_count + ' / ' + envelope.data.capacity +
      '. Peserta tetap ada di direktori.';
    renderRoster(await refreshedRoster(envelope.data));
    await searchCandidates();
    await loadMeetings();
  }

  /* add/remove answer with a summary but no member list, so re-read the roster to
     get the rows. Falls back to the summary if that read fails. */
  async function refreshedRoster(summary) {
    var envelope = await httpGet(
      '/enrollment/meetings/' + rosterMeetingUuid + '/roster'
    );
    return envelope.ok ? envelope.data : summary;
  }

  /* One request for the whole list, not one per meeting: each option already
     carries its own count and capacity. */
  async function loadMeetings() {
    var envelope = await httpGet('/enrollment/meetings', { limit: 200 });
    if (!envelope.ok) {
      el.rosterLabel.textContent = 'Daftar rapat tidak dapat dimuat: ' +
        failureText(envelope);
      el.rosterSave.disabled = true;
      return;
    }
    var meetings = envelope.data.meetings || [];
    var previous = rosterMeetingUuid;
    el.rosterSelect.textContent = '';
    if (meetings.length === 0) {
      var none = document.createElement('option');
      none.value = '';
      none.textContent = '(belum ada rapat)';
      el.rosterSelect.appendChild(none);
      rosterMeetingUuid = null;
      await loadRoster();
      return;
    }
    meetings.forEach(function (m) {
      var option = document.createElement('option');
      option.value = m.meeting_uuid;
      /* textContent, never innerHTML: a meeting title is operator-supplied. */
      option.textContent =
        m.title + '  (' + m.active_count + ' / ' + m.capacity + ')';
      el.rosterSelect.appendChild(option);
    });
    var keep = meetings.some(function (m) { return m.meeting_uuid === previous; });
    rosterMeetingUuid = keep ? previous : meetings[0].meeting_uuid;
    el.rosterSelect.value = rosterMeetingUuid;
    await loadRoster();
  }

  async function loadRoster() {
    if (!rosterMeetingUuid) {
      el.rosterLabel.textContent =
        'Belum ada rapat. Mulai satu perekaman untuk membuat rapat, lalu atur ' +
        'roster dan kapasitasnya di sini.';
      el.rosterPill.textContent = '0 / 0';
      el.rosterPill.dataset.state = 'unknown';
      el.rosterSave.disabled = true;
      return;
    }
    var envelope = await httpGet(
      '/enrollment/meetings/' + rosterMeetingUuid + '/roster'
    );
    if (!envelope.ok) {
      el.rosterLabel.textContent = 'Roster tidak dapat dimuat: ' + failureText(envelope);
      el.rosterSave.disabled = true;
      return;
    }
    el.rosterLabel.textContent = 'Rapat: ' + String(envelope.data.meeting_title || '—');
    renderRoster(envelope.data);
  }

  async function saveCapacity() {
    if (!rosterMeetingUuid || !rosterState) return;
    show(el.rosterError, false);
    /* Reject client-side too, so an obvious mistake does not need a round trip.
       The server validates independently -- this is convenience, not the rule. */
    var raw = el.rosterCapacity.value.trim();
    if (!/^[0-9]+$/.test(raw)) {
      show(el.rosterError, true);
      el.rosterError.textContent =
        'Kapasitas harus berupa bilangan bulat, misalnya 15.';
      return;
    }
    var wanted = parseInt(raw, 10);
    var envelope = await httpPatch(
      '/enrollment/meetings/' + rosterMeetingUuid + '/capacity',
      { capacity: wanted }
    );
    if (!envelope.ok) {
      show(el.rosterError, true);
      el.rosterError.textContent = 'Kapasitas tidak diubah: ' + failureText(envelope);
      /* Put the stored value back, so the field never shows a rejected number as
         if it had been saved. */
      el.rosterCapacity.value = String(rosterState.capacity);
      return;
    }
    renderRoster(envelope.data);
    el.rosterResult.textContent =
      'Kapasitas roster disimpan: ' + envelope.data.capacity + '.';
    /* The selector labels carry "count / capacity", so they are now stale. */
    await loadMeetings();
  }

  /* ---------------------------------------------------------- enrollment -- */

  function openEnrollment(entry) {
    selected = entry;
    el.enrollWho.textContent = entry.display_name;
    show(el.enrollCard, true);
    show(el.vpCard, true);
    el.sampleList.textContent = '';
    el.vpResult.textContent = '';
    once(refreshReadiness);
    startPolling();
  }

  async function refreshReadiness() {
    if (!selected) return;
    var envelope = await httpGet(
      '/enrollment/participants/' + selected.uuid + '/readiness'
    );
    if (!envelope.ok) {
      el.progress.textContent =
        'Tidak dapat memeriksa kesiapan: ' + failureText(envelope);
      return;
    }
    var r = envelope.data;
    var device = r.device || {};
    var cal = r.calibration || {};
    var model = r.model || {};
    var lock = r.capture_lock || {};

    setKvSafe(el.readiness, [
      ['peserta aktif', r.participant_active ? 'ya' : 'tidak'],
      ['consent', (r.consent && r.consent.active) ? 'aktif' : 'belum/dicabut'],
      ['model', model.ready ? 'siap' : 'BELUM TERSEDIA'],
      ['perangkat', device.detail],
      ['transport', device.transport],
      ['layak produksi', device.production_eligible_device
        ? 'ya (USB terverifikasi)'
        : 'tidak (mikrofon internal akan menghasilkan DEVELOPMENT_ONLY)'],
      ['kalibrasi', cal.verdict
        ? (cal.verdict + ' (' + cal.age_days + ' hari)') : 'belum ada'],
      ['mikrofon dipakai proses lain', lock.held_by_other ? 'ya' : 'tidak'],
      ['penghalang', (r.blockers || []).join(', ') || 'tidak ada']
    ]);

    var noModel = !model.ready;
    show(el.modelNotice, noModel);
    if (noModel) {
      el.modelDetail.textContent =
        'Model speaker embedding lokal belum diprovision, sehingga enrollment ' +
        'suara belum dapat dijalankan dan mikrofon tidak akan dibuka. Ini bukan ' +
        'kerusakan aplikasi: pemilihan dan provisioning model adalah langkah ' +
        'terpisah yang harus disetujui lebih dulu. Rincian: ' +
        String(model.detail || '');
      el.progress.textContent =
        'Enrollment dinonaktifkan: MODEL_UNAVAILABLE. Mikrofon tidak akan dibuka.';
    }

    el.startEnroll.disabled = !r.can_start;
  }

  async function refreshEnrollmentButtons() {
    if (!el.enrollCard || el.enrollCard.hidden) return;
    var envelope = await httpGet('/enrollment/sessions/current');
    if (!envelope.ok) return;
    var s = envelope.data;
    var live = s.active === true;

    el.enrollPill.textContent = s.state || 'IDLE';
    el.enrollPill.dataset.state = live
      ? 'ok'
      : ((s.state === 'REJECTED' || s.state === 'FAILED') ? 'fail' : 'unknown');

    if (!busy) {
      el.captureSample.disabled = !live;
      el.cancelEnroll.disabled = !live;
      el.finalize.disabled = !live ||
        (s.samples_accepted || 0) < (s.samples_target || 5);
    }

    if (live) {
      el.progress.textContent = 'Sample ' + s.samples_accepted + ' dari ' +
        s.samples_target + ' diterima.' +
        (s.will_be_development_only
          ? ' Perangkat bukan USB terverifikasi, sehingga hasilnya DEVELOPMENT_ONLY.'
          : '');
      renderSamples(s.samples || []);
    }
    await loadVoiceprint();
  }

  function renderSamples(samples) {
    el.sampleList.textContent = '';
    samples.forEach(function (sample) {
      var li = document.createElement('li');
      li.className = 'sample-row';
      var tag = document.createElement('span');
      var kind = sample.status === 'PASS' ? 'pass'
        : (sample.status === 'WARN' ? 'warn' : 'fail');
      tag.className = 'tag tag-' + kind;
      tag.textContent = sample.status;
      var levels = sample.levels || {};
      var text = document.createElement('span');
      text.textContent = 'sample ' + (sample.index + 1) + ' · ' +
        sample.seconds + ' s · RMS ' + levels.rms_dbfs + ' dBFS · peak ' +
        levels.peak_dbfs + ' dBFS · clipping ' + levels.clipping_percent + '%';
      li.appendChild(tag);
      li.appendChild(text);
      el.sampleList.appendChild(li);
    });
    if (samples.length) {
      var last = samples[samples.length - 1].levels || {};
      show(el.meter, true);
      setMeter(last.rms_dbfs, last.peak_dbfs);
      el.levels.textContent = 'Terakhir: RMS ' + last.rms_dbfs +
        ' dBFS, peak ' + last.peak_dbfs + ' dBFS, senyap ' +
        last.silence_percent + '%.';
    }
  }

  function setMeter(rms, peak) {
    /* -60..0 dBFS mapped to 0..100%, the same scale as the recording panel. */
    function pct(v) {
      if (typeof v !== 'number' || !isFinite(v)) return 0;
      return Math.max(0, Math.min(100, ((v + 60) / 60) * 100));
    }
    el.meterRms.style.width = pct(rms) + '%';
    el.meterPeak.style.left = pct(peak) + '%';
  }

  async function loadVoiceprint() {
    if (!selected) return;
    var envelope = await httpGet(
      '/enrollment/participants/' + selected.uuid + '/voiceprint'
    );
    if (!envelope.ok) return;
    var v = envelope.data;
    var current = v.current;
    setKvSafe(el.vpDetail, [
      ['status', current ? current.status : 'belum ada'],
      ['dapat dipakai', v.has_usable_voiceprint ? 'ya' : 'tidak'],
      ['layak produksi', v.production_eligible ? 'ya' : 'tidak'],
      ['model', (current && current.model) ? current.model.name : '—'],
      ['kualitas', current ? current.quality_verdict : '—'],
      ['kemiripan minimum', current ? current.min_pair_cosine : '—'],
      ['diaktifkan', current ? current.activated_at : '—']
    ]);
    if (!busy) {
      el.vpVerify.disabled = !current;
      el.vpRevoke.disabled = !(selected.consent && selected.consent.active);
    }
  }

  /* ------------------------------------------------------------- polling -- */

  async function poll() {
    if (pollStopped) return;
    if (!busy) await refreshEnrollmentButtons();
    if (pollStopped) return;
    pollTimer = window.setTimeout(poll, READINESS_POLL_MS);
  }

  function startPolling() {
    pollStopped = false;
    if (pollTimer === null) poll();
  }

  function stopPolling() {
    pollStopped = true;
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  /* The panel polls only while it is open, and the timer is cleared when the
     window goes away -- otherwise it would keep calling into a bridge whose
     Python side is shutting down. */
  window.addEventListener('pagehide', stopPolling);
  window.addEventListener('beforeunload', stopPolling);

  /* ----------------------------------------------------------- listeners -- */

  if (el.open) {
    el.open.addEventListener('click', function () {
      once(async function () {
        show(el.panel, true);
        await loadParticipants();
        await loadMeetings();
        await searchCandidates();
      });
    });
  }

  el.refresh.addEventListener('click', function () { once(loadParticipants); });
  el.newBtn.addEventListener('click', function () { openForm(null); });
  el.save.addEventListener('click', function () { once(saveParticipant); });
  el.cancelForm.addEventListener('click', function () {
    show(el.formCard, false);
    selected = null;
  });
  el.search.addEventListener('change', function () { once(loadParticipants); });
  el.refreshReady.addEventListener('click', function () { once(refreshReadiness); });

  el.startEnroll.addEventListener('click', function () {
    once(async function () {
      if (!selected) return;
      /* Consent must be recorded first, and the dialog is the only way. */
      if (!(selected.consent && selected.consent.active)) {
        await openConsentDialog(selected);
        return;
      }
      var envelope = await httpPost('/enrollment/sessions',
        { participant_uuid: selected.uuid, samples_target: 5 });
      if (!envelope.ok) {
        show(el.enrollWarn, true);
        el.enrollWarn.textContent =
          'Tidak dapat memulai: ' + failureText(envelope);
        return;
      }
      show(el.enrollWarn, false);
      startPolling();
    });
  });

  el.captureSample.addEventListener('click', function () {
    once(async function () {
      el.progress.textContent = 'Merekam sample — silakan berbicara…';
      var envelope = await httpPost('/enrollment/sessions/current/samples',
        { seconds: 10 });
      if (!envelope.ok) {
        show(el.enrollWarn, true);
        el.enrollWarn.textContent = 'Sample gagal: ' + failureText(envelope);
        return;
      }
      var sample = envelope.data.last_sample;
      if (envelope.data.sample_accepted === false && sample) {
        show(el.enrollWarn, true);
        el.enrollWarn.textContent = 'Sample ditolak: ' + (sample.gates || [])
          .filter(function (g) { return g.status === 'REJECT'; })
          .map(function (g) { return g.detail; })
          .join(' ');
      } else {
        show(el.enrollWarn, false);
      }
    });
  });

  el.finalize.addEventListener('click', function () {
    once(async function () {
      var envelope = await httpPost('/enrollment/sessions/current/finalize', {});
      if (!envelope.ok) {
        show(el.enrollWarn, true);
        el.enrollWarn.textContent = 'Finalisasi gagal: ' + failureText(envelope);
        return;
      }
      var data = envelope.data;
      if (!data.voiceprint) {
        show(el.enrollWarn, true);
        el.enrollWarn.textContent = 'Enrollment ditolak kualitas: ' +
          (((data.quality || {}).gates) || [])
            .filter(function (g) { return g.status === 'REJECT'; })
            .map(function (g) { return g.detail; })
            .join(' ');
        return;
      }
      show(el.enrollWarn, false);
      el.progress.textContent = 'Selesai. Status voiceprint: ' +
        data.voiceprint.status +
        (data.voiceprint.production_eligible
          ? ' (layak produksi).' : ' (belum layak produksi).');
      stopPolling();
      await loadParticipants();
      await loadVoiceprint();
    });
  });

  el.cancelEnroll.addEventListener('click', function () {
    if (!window.confirm(
      'Batalkan enrollment? Audio yang sudah direkam akan dibuang.'
    )) return;
    once(async function () {
      await httpPost('/enrollment/sessions/current/cancel',
        { reason: 'dibatalkan operator' });
      stopPolling();
      el.progress.textContent =
        'Enrollment dibatalkan. Tidak ada audio yang disimpan.';
    });
  });

  el.vpVerify.addEventListener('click', function () {
    once(async function () {
      if (!selected) return;
      var status = await httpGet(
        '/enrollment/participants/' + selected.uuid + '/voiceprint'
      );
      if (!status.ok || !status.data.current) return;
      var uuid = status.data.current.voiceprint_uuid;
      var verified = await httpPost(
        '/enrollment/voiceprints/' + uuid + '/verify', {}
      );
      if (!verified.ok) {
        el.vpResult.textContent = 'Verifikasi gagal: ' + failureText(verified);
        return;
      }
      var report = verified.data;
      el.vpResult.textContent = report.ok
        ? ('Integritas terverifikasi (' + report.status + ').')
        : ('MASALAH INTEGRITAS: ' + (report.problems || []).join('; '));
    });
  });

  el.vpRevoke.addEventListener('click', function (event) {
    /* Always open the dialog, even with nothing selected: it then names the
       problem instead of the click appearing to do nothing at all. */
    openRevokeDialog(selected, event.currentTarget);
  });

  el.consentAgree.addEventListener('change', syncDialogButtons);
  el.consentConfirm.addEventListener('click', function () { once(confirmConsent); });
  el.consentCancel.addEventListener('click', closeConsentDialog);

  /* NOT wrapped in once(): the dialog has its own in-flight guard, and once()'s
     shared `busy` flag would let an unrelated action make Batal unresponsive. */
  el.revokeConfirm.addEventListener('click', submitRevoke);
  el.revokeCancel.addEventListener('click', closeRevokeDialog);

  /* A click on the dimmed area must not reach the page underneath, and must not
     close a dialog whose confirm button destroys data by accident either. */
  el.revokeBackdrop.addEventListener('click', function (event) {
    if (event.target === el.revokeBackdrop) event.stopPropagation();
  });

  /* Escape closes -- unless a revoke is in flight, when closing would leave the
     operator unsure whether it happened. */
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!el.revokeBackdrop.hidden) {
      if (!revokeSubmitting) closeRevokeDialog();
      return;
    }
    if (!el.consentBackdrop.hidden) closeConsentDialog();
  });

  el.rosterSelect.addEventListener('change', function () {
    rosterMeetingUuid = el.rosterSelect.value || null;
    el.rosterResult.textContent = '';
    el.rosterMemberResult.textContent = '';
    show(el.rosterError, false);
    show(el.rosterMemberError, false);
    once(async function () {
      await loadRoster();
      await searchCandidates();
    });
  });

  el.rosterAddSearchBtn.addEventListener('click', function () {
    once(searchCandidates);
  });
  el.rosterAddSearch.addEventListener('change', function () {
    once(searchCandidates);
  });
  el.rosterAddSearch.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') once(searchCandidates);
  });
  el.rosterSave.addEventListener('click', function () { once(saveCapacity); });
  el.rosterCapacity.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') once(saveCapacity);
  });
})();

/* ==========================================================================
   Phase 4: transcription panel.

   The whole pipeline runs behind one POST that takes minutes, so this polls
   `/asr/status` for progress rather than holding a request open -- exactly as the
   recording panel does. Every call goes through the pywebview bridge, which means the
   session token never enters JavaScript and the page can only reach the anchored paths
   on the shell's allowlist.

   Nothing here can cause a model download. `/asr/models` reports readiness, `/asr/
   preflight` reports every precondition, and the Proses transkripsi button stays
   disabled until the server says the recording is eligible. Provisioning remains a
   deliberate command-line action.

   Eligibility is decided by the server, never here: `/asr/recordings` returns
   `eligible` and `ineligible_reason` per recording, so the button's enabled state and
   the explanation next to it cannot disagree.
   ========================================================================== */
(function () {
  'use strict';

  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  var POLL_MS = 1200;
  var ELAPSED_MS = 1000;
  /* A segment is called low-confidence when the decoder's own average token log
     probability is below the same floor pass-2 selection uses. Shown, never hidden:
     a reviewer needs to know which lines to check against the audio. */
  var LOW_CONFIDENCE_LOGPROB = -1.0;

  var el = {
    panel: document.getElementById('transcript-panel'),
    open: document.getElementById('open-transcript-btn'),
    modelKv: document.getElementById('asr-model-kv'),
    modelMissing: document.getElementById('asr-model-missing'),
    select: document.getElementById('asr-recording-select'),
    refresh: document.getElementById('asr-refresh-btn'),
    preflightBtn: document.getElementById('asr-preflight-btn'),
    selectedKv: document.getElementById('asr-selected-kv'),
    ineligible: document.getElementById('asr-ineligible'),
    emptyHint: document.getElementById('asr-empty-hint'),
    preflightList: document.getElementById('asr-preflight-list'),
    run: document.getElementById('asr-run-btn'),
    cancel: document.getElementById('asr-cancel-btn'),
    load: document.getElementById('asr-load-btn'),
    retryHint: document.getElementById('asr-retry-hint'),
    error: document.getElementById('asr-error'),
    pill: document.getElementById('asr-pill'),
    elapsed: document.getElementById('asr-elapsed'),
    costKv: document.getElementById('asr-cost-kv'),
    stages: document.getElementById('asr-stage-list'),
    pass2Kv: document.getElementById('asr-pass2-kv'),
    flagged: document.getElementById('asr-flagged-table'),
    transcriptKv: document.getElementById('asr-transcript-kv'),
    segments: document.getElementById('asr-segment-list'),
    transcriptEmpty: document.getElementById('asr-transcript-empty')
  };

  if (!el.panel) return;

  var busy = false;
  var pollTimer = null;
  var elapsedTimer = null;
  var startedAt = 0;
  var modelsReady = false;
  var preflightOk = false;
  var recordings = [];

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

  function show(node, visible) {
    if (node) node.hidden = !visible;
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

  function pad2(value) {
    return value < 10 ? '0' + value : String(value);
  }

  function stamp(ms) {
    var total = Math.max(0, Math.floor(Number(ms) / 1000));
    return (
      pad2(Math.floor(total / 3600)) +
      ':' +
      pad2(Math.floor((total % 3600) / 60)) +
      ':' +
      pad2(total % 60)
    );
  }

  function seconds(ms) {
    return (Number(ms || 0) / 1000).toFixed(1) + ' s';
  }

  function fail(message) {
    el.error.textContent = message;
    show(el.error, Boolean(message));
  }

  function selectedUuid() {
    var value = (el.select.value || '').trim();
    return UUID_RE.test(value) ? value : '';
  }

  function selectedEntry() {
    var uuid = selectedUuid();
    if (!uuid) return null;
    for (var index = 0; index < recordings.length; index += 1) {
      if (recordings[index].recording_uuid === uuid) return recordings[index];
    }
    return null;
  }

  /* -- readiness and the recording list --------------------------------- */

  var INELIGIBLE_TEXT = {
    MODEL_UNAVAILABLE:
      'Model transkripsi belum tersedia. Provisioning dilakukan sekali dari terminal.',
    RECORDING_IN_PROGRESS:
      'Ada perekaman yang sedang berjalan. Transkripsi tidak boleh bersaing dengan ' +
      'perekaman; hentikan rekaman terlebih dahulu.',
    NO_AUDIO: 'Rekaman ini tidak memiliki chunk audio, jadi tidak ada yang ditranskripsi.'
  };

  async function loadModels() {
    var response = await get('/asr/models');
    if (!response.ok) {
      setKv(el.modelKv, [['Status', 'tidak dapat dibaca']]);
      return;
    }
    var data = response.data || {};
    modelsReady = Boolean(data.pass1_ready);
    setKv(el.modelKv, [
      ['Pass 1', data.pass1_ready ? data.pass1_model || 'siap' : 'belum tersedia'],
      ['Pass 2', data.pass2_ready ? data.pass2_model || 'siap' : 'belum tersedia'],
      ['Registry', data.readable_index ? 'terbaca' : String(data.problem || 'rusak')]
    ]);
    show(el.modelMissing, !modelsReady);
    updateButtons();
  }

  function describeTranscript(entry) {
    if (!entry.transcript_revision) return 'belum ada';
    return (
      'revisi ' +
      entry.transcript_revision +
      ' (' +
      String(entry.transcript_status) +
      ', ' +
      entry.segment_count +
      ' segmen)'
    );
  }

  async function loadRecordings() {
    var response = await get('/asr/recordings', { limit: 100 });
    if (!response.ok) {
      fail(String((response.data && response.data.detail) || response.error || 'gagal'));
      return;
    }
    var previous = selectedUuid();
    recordings = (response.data || {}).recordings || [];
    el.select.textContent = '';
    show(el.emptyHint, recordings.length === 0);

    if (recordings.length === 0) {
      var none = document.createElement('option');
      none.value = '';
      none.textContent = 'Tidak ada rekaman selesai';
      el.select.appendChild(none);
    }
    recordings.forEach(function (entry) {
      var option = document.createElement('option');
      option.value = entry.recording_uuid;
      option.textContent =
        (entry.meeting_title || '(tanpa judul)') +
        ' — ' +
        stamp(entry.duration_ms) +
        ' — ' +
        entry.recording_uuid.slice(0, 8);
      el.select.appendChild(option);
    });
    if (previous) el.select.value = previous;
    renderSelected();
  }

  function renderSelected() {
    var entry = selectedEntry();
    preflightOk = false;
    el.preflightList.textContent = '';
    if (!entry) {
      setKv(el.selectedKv, [['Status', 'belum ada rekaman dipilih']]);
      show(el.ineligible, false);
      show(el.retryHint, false);
      updateButtons();
      return;
    }
    setKv(el.selectedKv, [
      ['Rapat', entry.meeting_title || '(tanpa judul)'],
      ['Durasi', stamp(entry.duration_ms)],
      ['Chunk', String(entry.chunk_count)],
      ['Integritas manifest', String(entry.manifest_status)],
      ['Kualitas', entry.degraded ? 'terdegradasi — periksa level' : 'normal'],
      ['Transkrip', describeTranscript(entry)],
      ['Jumlah revisi', String(entry.revision_count)]
    ]);
    var reason = entry.ineligible_reason;
    el.ineligible.textContent = reason
      ? INELIGIBLE_TEXT[reason] || String(reason)
      : '';
    show(el.ineligible, Boolean(reason));

    /* Re-running is the retry: it writes a new revision and leaves the old one as
       evidence. Said plainly, because "Proses transkripsi" on a recording that already
       has a transcript is otherwise an alarming button to press. */
    if (entry.revision_count > 0) {
      el.retryHint.textContent =
        'Rekaman ini sudah memiliki ' +
        entry.revision_count +
        ' revisi. Menjalankan ulang membuat revisi baru dan menonaktifkan yang lama — ' +
        'revisi sebelumnya tetap disimpan sebagai bukti, tidak ditimpa. Tahap yang ' +
        'sudah selesai dan masih valid (salinan kerja, VAD) akan dipakai ulang.';
      show(el.retryHint, true);
    } else {
      show(el.retryHint, false);
    }
    updateButtons();
  }

  function updateButtons() {
    var entry = selectedEntry();
    var eligible = Boolean(entry && entry.eligible);
    el.run.disabled = busy || !eligible || !modelsReady || !preflightOk;
    el.cancel.disabled = !busy;
    el.load.disabled = busy || !entry || !entry.transcript_revision;
    el.preflightBtn.disabled = busy || !entry;
    el.refresh.disabled = busy;
    el.select.disabled = busy;
    el.run.textContent =
      entry && entry.revision_count > 0 ? 'Proses transkripsi ulang' : 'Proses transkripsi';
  }

  /* -- preflight -------------------------------------------------------- */

  async function runPreflight() {
    fail('');
    var uuid = selectedUuid();
    if (!uuid) {
      fail('Pilih rekaman terlebih dahulu.');
      return;
    }
    var response = await get('/asr/preflight', { recording_uuid: uuid });
    if (!response.ok) {
      fail(String((response.data && response.data.detail) || response.error || 'gagal'));
      return;
    }
    var data = response.data || {};
    preflightOk = Boolean(data.ok);
    el.preflightList.textContent = '';
    (data.checks || []).forEach(function (check) {
      var li = document.createElement('li');
      li.className = check.ok ? 'stage-ok' : 'stage-fail';
      var name = document.createElement('strong');
      name.textContent = check.key + ': ';
      li.appendChild(name);
      li.appendChild(document.createTextNode(check.detail || ''));
      if (!check.ok && !check.blocking) {
        var note = document.createElement('em');
        note.textContent = ' (tidak memblokir)';
        li.appendChild(note);
      }
      el.preflightList.appendChild(li);
    });
    if (!preflightOk) {
      fail(
        data.blocking_count +
          ' pemeriksaan memblokir transkripsi. Perbaiki dahulu, lalu jalankan preflight lagi.'
      );
    }
    updateButtons();
  }

  /* -- progress --------------------------------------------------------- */

  function renderStages(stages) {
    el.stages.textContent = '';
    (stages || []).forEach(function (stage) {
      var li = document.createElement('li');
      li.className = stage.ok ? 'stage-ok' : 'stage-fail';
      var name = document.createElement('strong');
      name.textContent = stage.name + ': ';
      li.appendChild(name);
      li.appendChild(document.createTextNode(stage.detail || ''));
      el.stages.appendChild(li);
    });
  }

  function renderCost(result) {
    if (!result) {
      setKv(el.costKv, [['Status', 'belum ada hasil']]);
      return;
    }
    setKv(el.costKv, [
      ['Audio', seconds(result.audio_ms)],
      ['Wicara terdeteksi', seconds(result.speech_ms) + ' (' + result.region_count + ' bagian)'],
      ['Segmen / kata', result.segment_count + ' / ' + result.word_count],
      ['Waktu proses', seconds(result.wall_ms)],
      ['RTF', result.rtf === null ? 'N/A' : String(result.rtf)],
      ['Puncak memori worker', result.peak_rss_mib + ' MiB'],
      ['Istilah dinormalisasi', String(result.glossary_replacements)]
    ]);
  }

  function renderPass2(result) {
    if (!result) {
      setKv(el.pass2Kv, [['Status', 'belum dijalankan']]);
      return;
    }
    var rows = [
      ['Anggaran', seconds(result.pass2_budget_ms)],
      ['Terpakai', seconds(result.pass2_selected_ms)],
      ['Bagian diulang', String(result.pass2_region_count)]
    ];
    if (result.pass2_skipped_reason) {
      rows.push(['Dilewati', result.pass2_skipped_reason]);
    }
    if (result.pass2_budget_exhausted) {
      rows.push(['Anggaran habis', 'ya - sisa bagian memakai hasil pass 1']);
    }
    setKv(el.pass2Kv, rows);
  }

  function startElapsed() {
    startedAt = Date.now();
    if (elapsedTimer !== null) return;
    tickElapsed();
  }

  function tickElapsed() {
    el.elapsed.textContent = stamp(Date.now() - startedAt);
    elapsedTimer = window.setTimeout(function () {
      elapsedTimer = null;
      if (busy) tickElapsed();
    }, ELAPSED_MS);
  }

  function stopElapsed() {
    if (elapsedTimer !== null) {
      window.clearTimeout(elapsedTimer);
      elapsedTimer = null;
    }
  }

  async function poll() {
    var response = await get('/asr/status');
    if (!response.ok) {
      /* One failed status call is not the end of the run. Returning here used to stop
         the timer, so a single hiccup froze the display on a pipeline that was still
         working. */
      if (awaitingRun) schedulePoll();
      return;
    }
    var data = response.data || {};
    busy = Boolean(data.busy);
    if (busy) sawBusy = true;
    el.pill.textContent = busy
      ? 'Berjalan' + (data.cancel_requested ? ' (pembatalan diminta)' : '')
      : 'Idle';
    if (data.last_result) {
      renderCost(data.last_result);
      renderPass2(data.last_result);
      renderStages(data.last_result.stages);
    }
    updateButtons();
    if (busy) {
      schedulePoll();
      return;
    }
    if (awaitingRun && sawBusy) {
      /* The run this panel started has ended, and the status endpoint is what says so.
         `sawBusy` is required because the first poll can land before the backend has
         marked itself busy, and a stale `last_result` from a previous run would then be
         announced as this one's outcome. */
      finishRun(data.last_result || null, data.last_error || null);
      return;
    }
    stopPolling();
    stopElapsed();
  }

  /* Each poll schedules the next one itself, rather than running on a fixed repeating
     timer. A poll goes through the bridge, and a repeating timer would stack a second
     call on top of a round-trip that took longer than the interval. This cannot overlap
     with itself, and it stops as soon as the run finishes. */
  function schedulePoll() {
    if (pollTimer !== null) return;
    pollTimer = window.setTimeout(function () {
      pollTimer = null;
      poll();
    }, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer === null) return;
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  /* -- reads ------------------------------------------------------------ */

  function renderTranscript(payload) {
    var transcript = payload.transcript || {};
    setKv(el.transcriptKv, [
      ['Revisi', String(transcript.revision) + (transcript.is_active ? ' (aktif)' : '')],
      ['Status', String(transcript.status)],
      ['Pass 1', String(transcript.pass1_model_name || '-')],
      [
        'Pass 2',
        String(transcript.pass2_model_name || transcript.pass2_skipped_reason || '-')
      ],
      [
        'Glossary',
        transcript.glossary_version
          ? 'v' +
            transcript.glossary_version +
            ' - ' +
            transcript.glossary_replacements +
            ' koreksi'
          : 'nonaktif'
      ],
      ['Segmen / kata', transcript.segment_count + ' / ' + transcript.word_count],
      ['Pembicara', 'belum dipisahkan (Phase 5-6)']
    ]);

    el.segments.textContent = '';
    var segments = payload.segments || [];
    show(el.transcriptEmpty, segments.length === 0);
    segments.forEach(function (segment) {
      var low =
        typeof segment.avg_logprob === 'number' &&
        segment.avg_logprob < LOW_CONFIDENCE_LOGPROB;
      var li = document.createElement('li');
      li.className =
        'segment-row' +
        (segment.asr_pass === 2 ? ' segment-pass2' : '') +
        (low ? ' segment-low' : '');
      var time = document.createElement('span');
      time.className = 'segment-time';
      time.textContent = stamp(segment.start_ms);
      var who = document.createElement('span');
      who.className = 'segment-speaker';
      /* Phase 4 assigns no speaker. Rendered from the field rather than assumed, so a
         later phase that does assign one appears here without a change. */
      who.textContent = segment.speaker_status || 'UNASSIGNED';
      var text = document.createElement('span');
      text.className = 'segment-text';
      text.textContent = segment.text || '';
      li.appendChild(time);
      li.appendChild(who);
      li.appendChild(text);
      if (low) {
        var badge = document.createElement('span');
        badge.className = 'segment-lowconf';
        badge.textContent = 'rendah';
        badge.title =
          'Keyakinan decoder rendah (avg_logprob ' +
          segment.avg_logprob.toFixed(2) +
          '). Periksa terhadap audio.';
        li.appendChild(badge);
      }
      el.segments.appendChild(li);
    });
  }

  function renderFlagged(rows) {
    var body = el.flagged.querySelector('tbody');
    body.textContent = '';
    (rows || []).forEach(function (row) {
      var tr = document.createElement('tr');
      [
        String(row.region_seq === null ? '-' : row.region_seq),
        stamp(row.start_ms),
        String(row.asr_pass),
        row.selected_for_pass2 ? 'ya' : 'tidak',
        (row.reason_codes || []).join(', ')
      ].forEach(function (value) {
        var td = document.createElement('td');
        td.textContent = value;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  async function loadTranscript() {
    fail('');
    var uuid = selectedUuid();
    if (!uuid) {
      fail('Pilih rekaman terlebih dahulu.');
      return;
    }
    var response = await get('/asr/transcript/' + uuid);
    if (!response.ok) {
      fail(String((response.data && response.data.detail) || response.error || 'gagal'));
      show(el.transcriptEmpty, true);
      el.segments.textContent = '';
      return;
    }
    renderTranscript(response.data || {});
    var flagged = await get('/asr/flagged/' + uuid);
    if (flagged.ok) renderFlagged((flagged.data || {}).flagged);
  }

  /* -- actions ---------------------------------------------------------- */

  /* Whether a run started here is still unaccounted for, and whether the backend has
     been observed working on it. Both are needed to tell "the run finished" apart from
     "the first poll arrived before the backend marked itself busy". */
  var awaitingRun = false;
  var sawBusy = false;

  function finishRun(payload, errorText) {
    awaitingRun = false;
    sawBusy = false;
    busy = false;
    stopPolling();
    stopElapsed();
    if (payload) {
      renderStages(payload.stages);
      renderCost(payload);
      renderPass2(payload);
    }
    el.pill.textContent = errorText ? 'Gagal' : 'Selesai';
    if (errorText) fail(String(errorText));
    return loadRecordings()
      .then(function () {
        return errorText ? null : loadTranscript();
      })
      .then(updateButtons);
  }

  function run() {
    fail('');
    var uuid = selectedUuid();
    if (!uuid) {
      fail('Pilih rekaman terlebih dahulu.');
      return;
    }
    busy = true;
    awaitingRun = true;
    sawBusy = false;
    updateButtons();
    el.pill.textContent = 'Berjalan';
    startElapsed();
    schedulePoll();

    /* Deliberately NOT awaited, and this is the whole point of the note at the top of
       this panel. The pipeline runs for minutes; the bridge gives up after sixty
       seconds. Awaiting the POST meant the operator was shown a timeout for a run that
       was working and would finish -- which is exactly what happened once the pass-2
       budget was raised and a 135-second recording went from 56s to 98s. `/asr/status`
       is the source of truth for the outcome; this promise only carries the answers
       that arrive too fast for a poll to catch. */
    post('/asr/transcribe', { recording_uuid: uuid }).then(function (response) {
      if (!awaitingRun) return; // the poll has already accounted for this run
      if (response.ok) {
        finishRun(response.data || {}, null);
        return;
      }
      if (Number(response.status) === 0) {
        /* Transport gave up, not the pipeline: status 0 is the bridge's own timeout or
           a dropped connection, never an answer from the server. The run continues and
           the poll will see it end. Reporting this as a failure is the bug being fixed. */
        return;
      }
      var detail = (response.data && response.data.detail) || response.error || 'gagal';
      if (detail && typeof detail === 'object') {
        finishRun(detail, detail.error || detail.reason_code || 'transkripsi gagal');
      } else {
        finishRun(null, detail);
      }
    });
  }

  async function cancel() {
    var response = await post('/asr/cancel', null);
    if (!response.ok) {
      fail(String((response.data && response.data.detail) || 'tidak ada proses berjalan'));
      return;
    }
    el.pill.textContent = 'Pembatalan diminta';
  }

  /* -- wiring ----------------------------------------------------------- */

  var running = false;
  function once(action) {
    if (running) return;
    running = true;
    Promise.resolve()
      .then(action)
      .catch(function (error) {
        fail(String(error && error.message ? error.message : error));
      })
      .then(function () {
        running = false;
      });
  }

  if (el.open) {
    el.open.addEventListener('click', function () {
      show(el.panel, true);
      once(async function () {
        await loadModels();
        await loadRecordings();
        await poll();
      });
    });
  }

  el.select.addEventListener('change', function () {
    fail('');
    renderSelected();
  });
  el.refresh.addEventListener('click', function () {
    once(async function () {
      await loadModels();
      await loadRecordings();
    });
  });
  el.preflightBtn.addEventListener('click', function () {
    once(runPreflight);
  });
  el.run.addEventListener('click', function () {
    once(run);
  });
  el.cancel.addEventListener('click', function () {
    once(cancel);
  });
  el.load.addEventListener('click', function () {
    once(loadTranscript);
  });

  setKv(el.modelKv, [['Status', 'belum dimuat']]);
  setKv(el.selectedKv, [['Status', 'belum ada rekaman dipilih']]);
  renderCost(null);
  renderPass2(null);
  updateButtons();
})();

/* ==========================================================================
   Minutes panel.

   Generation runs behind one POST that takes ten to twenty minutes, so this polls
   `/mom/status` for liveness rather than holding a request open -- exactly as the
   recording and transcription panels do. Every call goes through the pywebview
   bridge, so the session token never enters JavaScript and the page can only reach
   the anchored paths on the shell's allowlist.

   Nothing here can cause a model download. `/mom/status` reports readiness and the
   Buat notulen button stays disabled until the server says the transcript is
   eligible. Provisioning remains a deliberate command-line action.

   Eligibility is decided by the server, never here: `/mom/transcripts` returns
   `eligible` and `reason` per row, so the button's enabled state and the explanation
   beside it cannot disagree.

   Two things this panel must always show, because they are the difference between a
   minute a reader can trust and one they cannot: the draft warning, and the
   verification state of every point. Neither is behind a toggle.
   ========================================================================== */
(function () {
  'use strict';

  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  var POLL_MS = 2000;
  var ELAPSED_MS = 1000;

  var KIND_LABELS = {
    DECISION: 'Keputusan',
    ACTION: 'Tindak Lanjut',
    DISCUSSION: 'Pembahasan',
    ISSUE: 'Isu / Pertanyaan Terbuka'
  };
  var KIND_ORDER = ['DECISION', 'ACTION', 'DISCUSSION', 'ISSUE'];
  var VERIFICATION_LABELS = {
    VERIFIED: 'terverifikasi',
    REBOUND: 'kutipan ditemukan di segmen lain',
    UNVERIFIED: 'BELUM TERVERIFIKASI'
  };
  /* Stored codes are stable identifiers; the operator reads sentences. Anything not
     listed here is a diagnostic that belongs in the database, not on screen. */
  var NOTE_LABELS = {
    OWNER_NOT_IN_TRANSCRIPT: 'nama PIC yang diusulkan model tidak terdengar di rekaman, jadi dihapus',
    OWNER_NOT_A_NAME: 'PIC yang diusulkan model bukan nama orang, jadi dihapus',
    DUE_NOT_IN_TRANSCRIPT: 'tenggat yang diusulkan model tidak terdengar di rekaman, jadi dihapus',
    QUOTE_NEAR_MATCH: 'kutipan tidak persis sama dengan transkrip',
    QUOTE_NOT_FOUND: 'kutipan tidak ditemukan di transkrip',
    CITATION_OUT_OF_RANGE: 'model merujuk segmen di luar bagian yang dibacanya'
  };

  var el = {
    panel: document.getElementById('mom-panel'),
    open: document.getElementById('open-mom-btn'),
    modelKv: document.getElementById('mom-model-kv'),
    modelMissing: document.getElementById('mom-model-missing'),
    select: document.getElementById('mom-transcript-select'),
    refresh: document.getElementById('mom-refresh-btn'),
    selectedKv: document.getElementById('mom-selected-kv'),
    ineligible: document.getElementById('mom-ineligible'),
    emptyHint: document.getElementById('mom-empty-hint'),
    format: document.getElementById('mom-format-select'),
    hideUnverified: document.getElementById('mom-hide-unverified'),
    run: document.getElementById('mom-run-btn'),
    cancel: document.getElementById('mom-cancel-btn'),
    load: document.getElementById('mom-load-btn'),
    error: document.getElementById('mom-error'),
    pill: document.getElementById('mom-pill'),
    elapsed: document.getElementById('mom-elapsed'),
    costKv: document.getElementById('mom-cost-kv'),
    stages: document.getElementById('mom-stage-list'),
    warnings: document.getElementById('mom-warning-list'),
    minuteKv: document.getElementById('mom-minute-kv'),
    minuteNote: document.getElementById('mom-minute-note'),
    exportRow: document.getElementById('mom-export-row'),
    exportBtn: document.getElementById('mom-export-btn'),
    exportNote: document.getElementById('mom-export-note'),
    summary: document.getElementById('mom-summary-list'),
    summaryBox: document.getElementById('mom-summary-box'),
    stats: document.getElementById('mom-stats'),
    statsWrap: document.getElementById('mom-stats-wrap'),
    progress: document.getElementById('mom-progress'),
    sections: document.getElementById('mom-sections'),
    minuteEmpty: document.getElementById('mom-minute-empty')
  };

  if (!el.panel) return;

  var busy = false;
  var running = false;
  var pollTimer = null;
  var elapsedTimer = null;
  var startedAt = 0;
  var modelReady = false;
  var transcripts = [];
  var loadedMinute = null;

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

  function show(node, visible) {
    if (node) node.hidden = !visible;
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

  function stamp(ms) {
    if (ms === null || ms === undefined) return '--:--:--';
    var total = Math.max(0, Math.floor(ms / 1000));
    var h = String(Math.floor(total / 3600)).padStart(2, '0');
    var m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
    var s = String(total % 60).padStart(2, '0');
    return h + ':' + m + ':' + s;
  }

  function minutes(ms) {
    return Math.round((ms || 0) / 60000) + ' menit';
  }

  function fail(message) {
    el.error.textContent = message || '';
    show(el.error, Boolean(message));
  }

  function detailOf(response) {
    if (!response) return 'tidak ada jawaban dari server.';
    /* `data`, because that is the key `ShellApi.api_get` returns. This module read
       `response.body` in all seven places it looked at a payload, so every read here
       silently produced `undefined`: the model showed as "belum tersedia", the feature
       as "dimatikan di konfigurasi" and the transcript list as empty, on an install
       where all three were fine. Every other module in this file already used `data`.
       A static test now checks the whole file against the envelope's real shape. */
    var body = response.data;
    if (body && typeof body === 'object' && body.detail) {
      return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    }
    return response.error || ('HTTP ' + response.status);
  }

  function selected() {
    var uuid = el.select.value;
    if (!UUID_RE.test(uuid)) return null;
    for (var i = 0; i < transcripts.length; i += 1) {
      if (transcripts[i].recording_uuid === uuid) return transcripts[i];
    }
    return null;
  }

  function updateButtons() {
    var row = selected();
    el.run.disabled = busy || running || !modelReady || !row || !row.eligible;
    el.cancel.disabled = !running;
    el.load.disabled = busy || !row || !row.minute_id;
    el.exportBtn.disabled = busy || running || !loadedMinute;
  }

  /* ------------------------------------------------------------------ model */

  async function loadModel() {
    var response = await get('/mom/status');
    if (!response.ok) {
      setKv(el.modelKv, [['Status', detailOf(response)]]);
      modelReady = false;
      show(el.modelMissing, true);
      updateButtons();
      return;
    }
    var status = response.data || {};
    modelReady = Boolean(status.model_ready);
    setKv(el.modelKv, [
      ['Model', status.model_name || 'belum tersedia'],
      ['Siap dipakai', modelReady ? 'ya' : 'tidak'],
      ['Fitur notulen', status.enabled ? 'aktif' : 'dimatikan di konfigurasi'],
      ['Perekaman berjalan', status.active_capture || 'tidak']
    ]);
    show(el.modelMissing, !modelReady);
    running = Boolean(status.running);
    if (running) sawRunning = true;
    if (awaitingRun && !running && sawRunning) {
      /* The run this panel started has ended, and the status endpoint is what says so.
         `sawRunning` is required because the first poll can land before the backend has
         marked itself running, and a stale `last_result` would then be announced as
         this run's outcome. `finishRun` clears `awaitingRun` before it calls back in
         here, so this cannot re-enter. */
      finishRun(status.last_result || null, status.last_error || null);
      return;
    }
    updateButtons();
  }

  /* ------------------------------------------------------------ transcripts */

  async function loadTranscripts() {
    var response = await get('/mom/transcripts', { limit: 50 });
    if (!response.ok) {
      fail('Tidak dapat memuat daftar transkrip: ' + detailOf(response));
      return;
    }
    transcripts = (response.data && response.data.transcripts) || [];
    var previous = el.select.value;
    el.select.textContent = '';
    if (!transcripts.length) {
      var none = document.createElement('option');
      none.value = '';
      none.textContent = '— belum ada transkrip selesai —';
      el.select.appendChild(none);
    } else {
      transcripts.forEach(function (row) {
        var option = document.createElement('option');
        option.value = row.recording_uuid;
        option.textContent =
          (row.meeting_title || '(tanpa judul)') +
          ' — ' + (row.started_at || '').slice(0, 16) +
          ' — ' + row.segment_count + ' segmen' +
          (row.minute_id ? ' — notulen rev ' + row.minute_revision : '');
        el.select.appendChild(option);
      });
      if (previous) el.select.value = previous;
      if (!el.select.value) el.select.selectedIndex = 0;
    }
    show(el.emptyHint, transcripts.length === 0);
    renderSelected();
  }

  function renderSelected() {
    var row = selected();
    if (!row) {
      setKv(el.selectedKv, [['Status', 'belum ada transkrip dipilih']]);
      show(el.ineligible, false);
      updateButtons();
      return;
    }
    setKv(el.selectedKv, [
      ['Rapat', row.meeting_title || '(tanpa judul)'],
      ['Transkrip', 'revisi ' + row.revision + ', ' + row.segment_count + ' segmen, ' +
        row.word_count + ' kata'],
      ['Durasi rekaman', minutes(row.duration_ms)],
      ['Notulen', row.minute_id
        ? 'revisi ' + row.minute_revision + ' (' + row.item_count + ' poin, ' +
          row.unverified_count + ' belum terverifikasi)'
        : 'belum ada']
    ]);
    if (row.eligible) {
      show(el.ineligible, false);
    } else {
      el.ineligible.textContent = 'Belum bisa dibuat: ' + (row.reason || 'alasan tidak diketahui');
      show(el.ineligible, true);
    }
    updateButtons();
  }

  /* --------------------------------------------------------------- progress */

  function renderStages(stages) {
    el.stages.textContent = '';
    (stages || []).forEach(function (stage) {
      var li = document.createElement('li');
      li.className = stage.ok ? 'stage-ok' : 'stage-fail';
      li.textContent = (stage.ok ? 'ok  ' : 'GAGAL ') + stage.name + ': ' + stage.detail;
      el.stages.appendChild(li);
    });
  }

  function renderWarnings(list) {
    el.warnings.textContent = '';
    (list || []).forEach(function (warning) {
      var li = document.createElement('li');
      li.className = 'stage-fail';
      li.textContent = warning;
      el.warnings.appendChild(li);
    });
  }

  function renderCost(result) {
    if (!result) {
      setKv(el.costKv, [['Status', 'belum ada proses']]);
      return;
    }
    var coverage = result.coverage_ratio === null || result.coverage_ratio === undefined
      ? 'tidak diketahui'
      : Math.round(result.coverage_ratio * 100) + '% dari isi transkrip';
    setKv(el.costKv, [
      ['Cakupan', coverage],
      ['Waktu', Math.round(result.total_seconds) + ' detik'],
      ['Memori puncak', result.peak_rss_mib + ' MiB'],
      ['Poin', result.item_count + ' (' + result.verified_count + ' terverifikasi, ' +
        result.unverified_count + ' belum)']
    ]);
  }

  function tickElapsed() {
    el.elapsed.textContent = stamp(Date.now() - startedAt);
    elapsedTimer = window.setTimeout(function () {
      elapsedTimer = null;
      if (running) tickElapsed();
    }, ELAPSED_MS);
  }

  function startElapsed() {
    if (elapsedTimer !== null) return;
    startedAt = Date.now();
    tickElapsed();
  }

  function stopElapsed() {
    if (elapsedTimer !== null) {
      window.clearTimeout(elapsedTimer);
      elapsedTimer = null;
    }
  }

  /* Re-armed only after the previous poll has resolved. A repeating timer would keep
     firing while a bridge call was still outstanding and queue them behind each other. */
  function schedulePoll() {
    if (pollTimer !== null) return;
    pollTimer = window.setTimeout(function () {
      pollTimer = null;
      loadModel().then(function () {
        if (running) schedulePoll();
      });
    }, POLL_MS);
  }

  function stopPoll() {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function startTimers() {
    el.pill.textContent = 'Membuat notulen';
    el.pill.className = 'pill pill-live';
    show(el.progress, true);
    startElapsed();
    schedulePoll();
  }

  function stopTimers(label) {
    stopElapsed();
    stopPoll();
    show(el.progress, false);
    el.pill.textContent = label;
    el.pill.className = 'pill';
  }

  /* ----------------------------------------------------------- the minute */

  function noteText(code) {
    var known = NOTE_LABELS[code];
    if (known) return known;
    var reversed = /^POSSIBLY_SUPERSEDED:(.*)$/.exec(code);
    if (reversed) {
      return 'PERHATIAN: keputusan ini tampaknya dibatalkan atau diubah pada ' +
        reversed[1] + ' — periksa keputusan yang berlaku';
    }
    var conflict = /^(OWNER|DUE)_CONFLICT:(.*)\|(.*)$/.exec(code);
    if (conflict) {
      var field = conflict[1] === 'OWNER' ? 'PIC' : 'tenggat';
      return 'dua bagian rekaman menyebut ' + field + ' berbeda: “' +
        conflict[2] + '” dan “' + conflict[3] + '”';
    }
    return null;
  }

  function statTile(value, label, tone) {
    var tile = document.createElement('div');
    tile.className = 'mom-stat' + (tone ? ' mom-stat-' + tone : '');
    var number = document.createElement('div');
    number.className = 'mom-stat-value';
    number.textContent = String(value);
    var caption = document.createElement('div');
    caption.className = 'mom-stat-label';
    caption.textContent = label;
    tile.appendChild(number);
    tile.appendChild(caption);
    return tile;
  }

  function renderStats(minute) {
    el.stats.textContent = '';
    var coverage = minute.transcript_ms
      ? Math.round(100 * minute.covered_ms / minute.transcript_ms)
      : null;
    el.stats.appendChild(statTile(minute.item_count, 'poin dicatat', null));
    el.stats.appendChild(
      statTile(minute.verified_count, 'cocok dengan rekaman', 'ok')
    );
    el.stats.appendChild(
      statTile(minute.unverified_count, 'belum terverifikasi',
        minute.unverified_count ? 'fail' : null)
    );
    el.stats.appendChild(
      statTile(coverage === null ? '—' : coverage + '%', 'transkrip terproses',
        coverage !== null && coverage < 100 ? 'warn' : null)
    );
    show(el.statsWrap, true);
  }

  function badge(text, tone) {
    var span = document.createElement('span');
    span.className = 'mom-badge mom-badge-' + tone;
    span.textContent = text;
    return span;
  }

  function renderItem(item) {
    var li = document.createElement('li');
    li.className = 'mom-item mom-item-' + item.kind.toLowerCase() +
      (item.verification === 'UNVERIFIED' ? ' mom-item-unverified' : '');

    var text = document.createElement('div');
    text.className = 'mom-item-text';
    var time = document.createElement('span');
    time.className = 'mom-item-time';
    time.textContent = stamp(item.start_ms);
    text.appendChild(time);
    text.appendChild(document.createTextNode(item.text));
    li.appendChild(text);

    var badges = document.createElement('div');
    badges.className = 'mom-badges';
    if (item.verification === 'VERIFIED') {
      badges.appendChild(badge('cocok dengan rekaman', 'ok'));
    } else if (item.verification === 'REBOUND') {
      badges.appendChild(badge('kutipan di segmen lain', 'warn'));
    } else {
      badges.appendChild(badge('BELUM TERVERIFIKASI', 'fail'));
    }
    if (item.owner) badges.appendChild(badge('PIC: ' + item.owner, 'info'));
    if (item.due_text) badges.appendChild(badge('tenggat: ' + item.due_text, 'info'));
    if (item.merged_count > 1) {
      badges.appendChild(badge('disebut ' + item.merged_count + 'x', 'plain'));
    }
    li.appendChild(badges);

    var quote = document.createElement('div');
    quote.className = 'mom-quote';
    quote.textContent = '“' + item.quote + '”';
    li.appendChild(quote);

    (item.verification_notes || []).forEach(function (code) {
      var readable = noteText(code);
      if (!readable) return;
      var note = document.createElement('div');
      note.className = 'mom-note' +
        (code.indexOf('POSSIBLY_SUPERSEDED') === 0 || code.indexOf('QUOTE_NOT_FOUND') === 0
          ? ' mom-note-fail' : '');
      note.textContent = readable;
      li.appendChild(note);
    });
    return li;
  }

  /* Actions also get a table. It is the part somebody has to act on, and a table
     is the shape that survives being pasted into an email. */
  function renderActionTable(items) {
    var table = document.createElement('table');
    table.className = 'mom-action-table';
    var head = document.createElement('tr');
    ['No', 'Tindak lanjut', 'PIC', 'Tenggat', 'Waktu'].forEach(function (label) {
      var th = document.createElement('th');
      th.textContent = label;
      head.appendChild(th);
    });
    var thead = document.createElement('thead');
    thead.appendChild(head);
    table.appendChild(thead);

    var body = document.createElement('tbody');
    items.forEach(function (item, index) {
      var row = document.createElement('tr');
      [String(index + 1), item.text, item.owner, item.due_text, stamp(item.start_ms)]
        .forEach(function (value, column) {
          var td = document.createElement('td');
          if ((column === 2 || column === 3) && !value) {
            td.className = 'mom-unstated';
            td.textContent = 'tidak disebutkan';
          } else {
            td.textContent = value;
          }
          row.appendChild(td);
        });
      body.appendChild(row);
    });
    table.appendChild(body);
    return table;
  }

  function renderMinute(minute) {
    loadedMinute = minute;
    el.summary.textContent = '';
    el.sections.textContent = '';
    el.stats.textContent = '';
    if (!minute) {
      setKv(el.minuteKv, [['Status', 'belum ada notulen dimuat']]);
      show(el.minuteEmpty, true);
      show(el.minuteNote, false);
      show(el.exportRow, false);
      show(el.statsWrap, false);
      show(el.summaryBox, false);
      updateButtons();
      return;
    }
    show(el.minuteEmpty, false);
    show(el.minuteNote, true);
    show(el.exportRow, true);
    renderStats(minute);

    setKv(el.minuteKv, [
      ['Judul', minute.title || '(tanpa judul)'],
      ['Nomor', minute.document_number || 'tidak dinomori'],
      ['Revisi', String(minute.revision) + ' (' + minute.status + ')'],
      ['Model', (minute.model_name || 'tidak tercatat') +
        (minute.quantisation ? ' (' + minute.quantisation + ')' : '')],
      ['PIC dihapus', String(minute.owners_dropped || 0) + ' (nama tidak terdengar di rekaman)']
    ]);

    (minute.summary || []).forEach(function (line) {
      var li = document.createElement('li');
      li.textContent = line;
      el.summary.appendChild(li);
    });
    if ((minute.summary_unsupported_numbers || []).length) {
      var alarm = document.createElement('li');
      alarm.className = 'mom-note-fail';
      alarm.textContent = 'Ringkasan memuat angka yang tidak ada di poin manapun: ' +
        minute.summary_unsupported_numbers.join(', ') + '. Periksa terhadap rekaman.';
      el.summary.appendChild(alarm);
    }
    show(el.summaryBox, (minute.summary || []).length > 0);

    KIND_ORDER.forEach(function (kind) {
      var items = (minute.items || []).filter(function (item) { return item.kind === kind; });
      if (!items.length) return;

      var head = document.createElement('div');
      head.className = 'mom-section-head';
      var heading = document.createElement('h4');
      heading.textContent = KIND_LABELS[kind];
      var count = document.createElement('span');
      count.className = 'mom-section-count';
      count.textContent = items.length + ' poin';
      head.appendChild(heading);
      head.appendChild(count);
      el.sections.appendChild(head);

      if (kind === 'ACTION') el.sections.appendChild(renderActionTable(items));

      var list = document.createElement('ul');
      list.className = 'mom-items';
      items.forEach(function (item) { list.appendChild(renderItem(item)); });
      el.sections.appendChild(list);
    });

    var exported = (minute.exports || [])[0];
    el.exportNote.textContent = exported
      ? 'Terakhir ditulis: ' + exported.format + ' — ' + exported.relative_path
      : 'Belum ada dokumen yang ditulis dari revisi ini.';
    updateButtons();
  }

  /* ------------------------------------------------------------------ acts */

  /* Whether a run started here is still unaccounted for, and whether the backend has
     been observed working on it. Both are needed to tell "it finished" apart from "the
     first poll arrived before the backend marked itself running". */
  var awaitingRun = false;
  var sawRunning = false;

  function finishRun(result, errorText) {
    awaitingRun = false;
    sawRunning = false;
    running = false;
    stopTimers(errorText ? 'Gagal' : 'Selesai');
    if (result) {
      renderStages(result.stages);
      renderWarnings(result.warnings);
      renderCost(result);
    }
    if (errorText) fail('Pembuatan notulen gagal: ' + errorText);
    updateButtons();
    return loadModel()
      .then(function () {
        return errorText ? null : loadTranscripts();
      })
      .then(function () {
        return errorText ? null : loadMinute();
      });
  }

  function run() {
    var row = selected();
    if (!row) return;
    fail('');
    renderStages([]);
    renderWarnings([]);
    renderCost(null);
    running = true;
    awaitingRun = true;
    sawRunning = false;
    startTimers();
    updateButtons();
    schedulePoll();

    /* Deliberately NOT awaited, for the reason the transcription panel gives at length:
       the model runs for minutes and the bridge gives up after sixty seconds, so
       awaiting the POST reports a timeout for a run that is working. `/mom/status` owns
       the outcome; this promise only carries answers that arrive too fast to poll for,
       and the refusals -- a capture in progress, a run already going -- which come back
       immediately. */
    post('/mom/generate', {
      recording_uuid: row.recording_uuid,
      export_formats: [el.format.value],
      include_unverified: !el.hideUnverified.checked
    }).then(function (response) {
      if (!awaitingRun) return; // the poll already accounted for this run
      if (response.ok) {
        finishRun(response.data || {}, null);
        return;
      }
      if (Number(response.status) === 0) {
        /* Transport gave up, not the model: status 0 is the bridge's own timeout or a
           dropped connection, never an answer from the server. */
        return;
      }
      finishRun(null, detailOf(response));
    });
  }

  async function cancel() {
    var response = await post('/mom/cancel', {});
    if (!response.ok) fail('Tidak dapat membatalkan: ' + detailOf(response));
  }

  async function loadMinute() {
    var row = selected();
    if (!row) return;
    var response = await get('/mom/minute/' + row.recording_uuid);
    if (!response.ok) {
      fail('Tidak dapat memuat notulen: ' + detailOf(response));
      renderMinute(null);
      return;
    }
    renderMinute(response.data || null);
  }

  async function exportAgain() {
    var row = selected();
    if (!row) return;
    fail('');
    var response = await post('/mom/export', {
      recording_uuid: row.recording_uuid,
      export_format: el.format.value,
      include_unverified: !el.hideUnverified.checked
    });
    if (!response.ok) {
      fail('Ekspor gagal: ' + detailOf(response));
      return;
    }
    var record = response.data || {};
    el.exportNote.textContent = 'Ditulis: ' + record.format + ' — ' + record.relative_path +
      (record.included_unverified ? ' (memuat poin belum terverifikasi)' : '');
    await loadMinute();
  }

  function once(action) {
    if (busy) return;
    busy = true;
    updateButtons();
    Promise.resolve()
      .then(action)
      .catch(function (error) { fail(String(error)); })
      .then(function () {
        busy = false;
        updateButtons();
      });
  }

  if (el.open) {
    el.open.addEventListener('click', function () {
      show(el.panel, true);
      once(async function () {
        await loadModel();
        await loadTranscripts();
      });
    });
  }

  el.select.addEventListener('change', function () {
    fail('');
    renderMinute(null);
    renderSelected();
  });
  el.refresh.addEventListener('click', function () {
    once(async function () {
      await loadModel();
      await loadTranscripts();
    });
  });
  el.run.addEventListener('click', function () { once(run); });
  el.cancel.addEventListener('click', function () { once(cancel); });
  el.load.addEventListener('click', function () { once(loadMinute); });
  el.exportBtn.addEventListener('click', function () { once(exportAgain); });

  setKv(el.modelKv, [['Status', 'belum dimuat']]);
  setKv(el.selectedKv, [['Status', 'belum ada transkrip dipilih']]);
  renderCost(null);
  renderMinute(null);
  updateButtons();
})();

/* ==========================================================================
   NAVIGATION

   One view is visible; the rest carry `hidden`. The rail owns that decision and
   nothing else does -- the four workflow modules below never touch each other's
   visibility, which is what keeps them independent IIFEs.

   Each module still owns its own first load: fetch the device list, run the
   preflight, start polling. All of that already hangs off the module's "open"
   button from the previous layout, so rather than reimplement four loaders here,
   the first visit to a view clicks the button. Whether a view has been visited is
   tracked here and not inferred from the button's `disabled` flag: two of the four
   modules never set it, so inferring would have refetched on every single click
   for transcription and minutes, and not at all for the other two.
   ========================================================================== */
(function () {
  var items = Array.prototype.slice.call(document.querySelectorAll('.nav-item'));
  if (!items.length) return;

  var LOADER_OF_VIEW = {
    'view-peserta': 'open-participants-btn',
    'view-rekam': 'open-recording-btn',
    'view-teks': 'open-transcript-btn',
    'view-notulen': 'open-mom-btn'
  };
  var visited = {};

  function showView(viewId) {
    items.forEach(function (item) {
      var target = document.getElementById(item.dataset.view);
      var current = item.dataset.view === viewId;
      if (target) target.hidden = !current;
      if (current) {
        item.setAttribute('aria-current', 'page');
      } else {
        item.removeAttribute('aria-current');
      }
    });

    if (LOADER_OF_VIEW[viewId] && !visited[viewId]) {
      visited[viewId] = true;
      var loader = document.getElementById(LOADER_OF_VIEW[viewId]);
      if (loader) loader.click();
    }

    /* The rail is sticky but the page is not: without this, opening a long view
       left the reader half-way down the previous one. */
    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  items.forEach(function (item) {
    item.addEventListener('click', function () { showView(item.dataset.view); });
  });

  /* The home screen repeats the same destinations as large cards, because somebody
     opening this for the first time reads the middle of the screen, not a rail.
     They drive the rail rather than duplicating what it does. */
  Array.prototype.slice.call(document.querySelectorAll('[data-goto]')).forEach(
    function (button) {
      button.addEventListener('click', function () {
        var item = document.getElementById(button.dataset.goto);
        if (item) item.click();
      });
    }
  );

  /* The same buttons still sit on the capability list under Sistem, where they now
     read as "go there". Marking the view visited *before* navigating is what stops
     this from recursing: `showView` would otherwise click the very button whose
     handler called it. */
  Object.keys(LOADER_OF_VIEW).forEach(function (viewId) {
    var loader = document.getElementById(LOADER_OF_VIEW[viewId]);
    if (!loader) return;
    loader.addEventListener('click', function () {
      visited[viewId] = true;
      showView(viewId);
    });
  });

  showView('view-beranda');
})();
