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
