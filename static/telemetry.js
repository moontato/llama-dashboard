(function () {
  'use strict';

  // ── Constants ───────────────────────────────────────────
  var STALE_MS     = 5000;
  var STATE_ICON   = { ok: '', warn: '⚠ ', critical: '🔴 ' };

  // ── State ───────────────────────────────────────────────
  var staleTimer   = null;
  var prevState    = 'ok';
  var notifSent    = false;
  var cpuReady     = false;
  var latestHistory = null;
  function connection(text, state) {
    var element = document.getElementById('connection-status');
    element.textContent = text; element.dataset.state = state;
  }
  function redrawHistory() {
    if (!latestHistory || document.getElementById('view-overview').hidden) return;
    ramSpark.draw(latestHistory.ram_pct);
    gpuSpark.draw(latestHistory.gpu_pct);
  }
  window.addEventListener('resize', function () { requestAnimationFrame(redrawHistory); });
  document.addEventListener('ui:view', function () { requestAnimationFrame(redrawHistory); });

  // ── Sparklines ──────────────────────────────────────────
  function Sparkline(canvasId, strokeColor) {
    this.el    = document.getElementById(canvasId);
    this.color = strokeColor;
  }
  Sparkline.prototype.draw = function (data) {
    var el  = this.el;
    var ctx = el.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    var w   = el.offsetWidth  * dpr;
    var h   = el.offsetHeight * dpr;
    if (w < 2 || h < 2) return;
    el.width  = w;
    el.height = h;
    ctx.clearRect(0, 0, w, h);
    if (!data || data.length < 2) return;

    ctx.beginPath();
    for (var i = 0; i < data.length; i++) {
      var x = (i / (data.length - 1)) * w;
      var y = h - (data[i] / 100) * h;
      if (i === 0) ctx.moveTo(x, y);
      else         ctx.lineTo(x, y);
    }
    ctx.strokeStyle = this.color;
    ctx.lineWidth   = 1.5 * dpr;
    ctx.lineJoin    = 'round';
    ctx.stroke();

    // fill under line
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = this.color + '22';
    ctx.fill();
  };

  var ramSpark = new Sparkline('ram-spark', '#22c55e');
  var gpuSpark = new Sparkline('gpu-spark', '#818cf8');

  // ── CPU bars ────────────────────────────────────────────
  function ensureCpuBars(count) {
    if (cpuReady) return;
    cpuReady = true;
    document.getElementById('cpu-label').textContent = 'CPU (' + count + ' cores)';
    var strip = document.getElementById('cpu-strip');
    strip.innerHTML = '';
    for (var i = 0; i < count; i++) {
      var col  = document.createElement('div');
      col.className = 'cpu-col';
      col.title     = 'CPU' + (i + 1);
      var fill = document.createElement('div');
      fill.className = 'cpu-fill';
      fill.id        = 'cpuf-' + i;
      fill.style.height = '0%';
      col.appendChild(fill);
      strip.appendChild(col);
    }
  }

  // ── State / color ────────────────────────────────────────
  function applyRamState(state, ramPct) {
    var card = document.getElementById('ram-card');
    card.className = 'card s-' + (state === 'critical' ? 'crit' : state);

    if (state !== prevState) {
      if (state === 'critical' && !notifSent) {
        notifSent = true;
        notify(ramPct);
      }
      if (state !== 'critical') notifSent = false;
      prevState = state;
    }
  }

  function swapClass(pct, th) {
    // thresholds come from the server (configurable); fall back to the
    // historical defaults when an old backend is streaming
    var warn = (th && th.swap_warn != null) ? th.swap_warn : 25;
    var crit = (th && th.swap_crit != null) ? th.swap_crit : 50;
    if (pct >= crit) return 'card sw-crit';
    if (pct >= warn) return 'card sw-warn';
    return 'card';
  }

  function updateTitle(state, ramPct) {
    document.title = (STATE_ICON[state] || '') + ramPct + '% — Orin';
  }

  // ── Web Notification (optional stretch) ─────────────────
  function notify(ramPct) {
    if (!('Notification' in window)) return;
    function send() {
      new Notification('⚠️ Orin RAM Critical', {
        body: 'RAM at ' + ramPct + '% — OOM risk',
        tag:  'orin-oom',
      });
    }
    if (Notification.permission === 'granted') {
      send();
    } else if (Notification.permission !== 'denied') {
      Notification.requestPermission().then(function (p) {
        if (p === 'granted') send();
      });
    }
  }

  // ── DOM update ───────────────────────────────────────────
  function setText(id, val) {
    var el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  function update(d) {
    // Board info arrives as a one-off event.
    if (d.board) {
      setText('hdr-model',   d.board.model   || 'Jetson AGX Orin');
      setText('hdr-jetpack', 'JetPack ' + (d.board.jetpack || '—'));
      return;
    }

    if (d.disconnected) {
      document.getElementById('disconnect-banner').classList.add('visible');
      connection('Telemetry offline', 'offline');
      miMarkLoaded(null);
      return;
    }

    document.getElementById('disconnect-banner').classList.remove('visible');
    connection('Telemetry live', 'live');

    var ram  = d.ram;
    var swap = d.swap;

    // RAM
    setText('ram-pct',    ram.pct + '%');
    setText('ram-detail', ram.used_gib + ' / ' + ram.total_gib + ' GiB');
    document.getElementById('ram-bar').style.width = Math.min(ram.pct, 100) + '%';
    applyRamState(d.state, ram.pct);
    updateTitle(d.state, ram.pct);

    // Swap
    setText('swap-pct',    swap.pct + '%');
    setText('swap-detail', swap.used_gib + ' / ' + swap.total_gib + ' GiB');
    document.getElementById('swap-bar').style.width = Math.min(swap.pct, 100) + '%';
    document.getElementById('swap-card').className  = swapClass(swap.pct, d.thresholds);

    // Shared
    setText('shared-val', ram.shared_gib);

    // Tiles
    setText('gpu-val',   d.gpu_pct == null ? '—' : d.gpu_pct);
    setText('temp-val',  d.temp_c == null ? '—' : d.temp_c);
    setText('power-val', d.power_w == null ? '—' : d.power_w);
    setText('fan-val',   d.fan_pct == null ? '—' : d.fan_pct);

    // nvp in header
    setText('hdr-nvp', d.nvp ? 'nvp ' + d.nvp : 'nvp —');

    // CPU
    if (d.cpu_pct && d.cpu_pct.length) {
      ensureCpuBars(d.cpu_pct.length);
      for (var i = 0; i < d.cpu_pct.length; i++) {
        var bar = document.getElementById('cpuf-' + i);
        if (bar) bar.style.height = d.cpu_pct[i] + '%';
      }
    }

    // Sparklines
    if (d.history) {
      latestHistory = d.history;
      redrawHistory();
    }

    // llama-server status + model disk (slow probes, ~15 s)
    updateLlm(d.llama || { state: 'unknown' });
    if (d.disk) updateDisk(d.disk);
    else {
      setText('disk-detail', 'Unavailable'); setText('disk-pct', '—%');
      setText('disk-label', 'Disk probe unavailable');
      document.getElementById('disk-bar').style.width = '0%';
    }

    // Timestamp
    var dt = new Date(d.ts * 1000);
    setText('footer-ts', 'Last update: ' + dt.toLocaleTimeString());
  }

  // ── llama-server card ────────────────────────────────────
  function fmtUptime(s) {
    s = Math.max(0, s | 0);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
    m %= 60; h %= 24;
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  function updateLlm(l) {
    document.getElementById('llm-card').style.display = '';
    var state = l.state || 'unknown';
    var dot = document.getElementById('llm-dot');
    dot.className = 'llm-dot ' +
      (state === 'active' ? 'active' : state === 'unknown' ? 'unknown' : 'inactive');
    var st = document.getElementById('llm-state');
    st.textContent = state === 'active' ? 'Running' : state === 'unknown' ? 'Unavailable' : state;
    miMarkLoaded(state === 'active' && l.model_status === 'loaded' ? l.model : null);
    st.style.color = state === 'active' ? 'var(--ok)' :
                      state === 'unknown' ? 'var(--muted)' : 'var(--crit)';
    var meta = [];
    if (l.uptime_s != null) meta.push('up ' + fmtUptime(l.uptime_s));
    document.getElementById('llm-meta').textContent = meta.join(' \u00b7 ') || 'Service uptime unavailable';
    var m = document.getElementById('llm-model');
    if (l.model) {
      if (l.model_status === 'unloaded') {
        // kept for robustness — the backend only sends a name together
        // with a loaded/loading entry
        m.textContent = 'no model loaded';
        m.className = 'llm-model loading';
      } else if (l.model_status === 'loading') {
        m.textContent = 'loading ' + l.model + '\u2026';
        m.className = 'llm-model loading';
      } else {
        m.textContent = l.model;
        m.className = 'llm-model';
      }
      m.style.display = '';
    } else if (l.model_status === 'unloaded') {
      // server reachable, but the slot is genuinely empty
      m.textContent = 'no model loaded';
      m.className = 'llm-model loading';
      m.style.display = '';
    } else if (l.model_configured && state === 'active') {
      m.textContent = 'Model information unavailable';
      m.className = 'llm-model loading';
      m.style.display = '';
    } else {
      m.textContent = state === 'active' ? 'Model probe not configured' : 'No active model information';
      m.style.display = '';
    }
  }

  // ── Disk card ────────────────────────────────────────────
  function updateDisk(k) {
    var card = document.getElementById('disk-card');
    card.style.display = '';
    card.className = 'card disk-card' +
      (k.pct >= 95 ? ' dsk-crit' : k.pct >= 90 ? ' dsk-warn' : '');
    document.getElementById('disk-bar').style.width = Math.min(k.pct, 100) + '%';
    setText('disk-detail', k.used_gib + ' / ' + k.total_gib + ' GiB');
    setText('disk-pct', k.pct + '%');
    setText('disk-label', 'Disk \u00b7 ' + k.path);
  }

  // ── Stale detection ──────────────────────────────────────
  function resetStale() {
    clearTimeout(staleTimer);
    document.getElementById('stale-banner').classList.remove('visible');
    staleTimer = setTimeout(function () {
      document.getElementById('stale-banner').classList.add('visible');
      connection('Telemetry stale', 'stale');
      miMarkLoaded(null);
    }, STALE_MS);
  }

  // ── EventSource ──────────────────────────────────────────
  var es = new EventSource('/stream');

  es.onmessage = function (evt) {
    try {
      var data = JSON.parse(evt.data);
      if (data.ram) resetStale();
      update(data);
    } catch (e) {
      console.warn('llama-dashboard parse error', e);
    }
  };

  es.onerror = function () {
    connection('Telemetry offline', 'offline');
    miMarkLoaded(null);
    document.getElementById('disconnect-banner').classList.add('visible');
  };

  resetStale();
}());

