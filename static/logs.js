// ── llama-server log viewer ─────────────────────────────────
var _logEs        = null;
var _logOpen      = false;
var _logPaused    = false;  // sticky pause (Pause/Resume button)
var _logMouseHold = false;  // transient hold while the mouse is down
var _logPinned    = true;   // stick to the bottom until the user scrolls up
var _logMatches   = [];     // <mark> elements for the current search
var _logMatchIdx  = -1;

function logIsPaused() { return _logPaused || _logMouseHold; }

function logToggle() {
  var btn = document.getElementById('log-toggle-btn');
  var dlg = document.getElementById('log-dialog');
  _logOpen = !_logOpen;
  if (_logOpen) {
    dlg.classList.add('open');
    btn.textContent = 'Hide logs';
    if (!_logEs) logStart();
  } else {
    dlg.classList.remove('open');
    btn.textContent = 'Show logs';
    logStop();
  }
  btn.setAttribute('aria-expanded', String(_logOpen));
}

function logStart() {
  if (_logEs || !_logOpen || document.getElementById('view-overview').hidden) return;
  document.getElementById('log-start-btn').disabled = true;
  logSetStatus('connecting', 'Connecting…');
  _logEs = new EventSource('/api/logs/llama-server?tail='
                           + document.getElementById('log-tail').value);

  var source = _logEs;
  _logEs.onopen = function () {
    if (_logEs !== source) return;
    logSetStatus('streaming', 'connected');
    document.getElementById('log-stop-btn').disabled = false;
    document.getElementById('log-clear-btn').disabled = false;
    document.getElementById('log-pause-btn').disabled = false;
    document.getElementById('log-tail').disabled = false;
    logSearchEnable(true);
  };

  _logEs.onmessage = function (evt) {
    if (_logEs !== source) return;
    var line = evt.data;
    if (!line) return;  // heartbeat
    if (line.indexOf('[error] ') === 0) {
      logAppendLine(line, 'log-err');
      logSetStatus('error', 'error');
    } else {
      logAppendLine(line, null);
    }
  };

  _logEs.onerror = function () {
    if (_logEs !== source) return;
    logSetStatus('error', 'disconnected');
    _logEs.close();
    _logEs = null;
    document.getElementById('log-stop-btn').disabled = true;
    document.getElementById('log-start-btn').disabled = false;
    document.getElementById('log-pause-btn').disabled = true;
    logSearchEnable(true);
  };
}

function logAppendLine(line, forcedClass) {
  var viewer = document.getElementById('log-viewer');
  var cls = forcedClass
      || (/(^|\s)(ERROR|CRIT|FATAL)([:\s]|$)/i.test(line) ? 'log-err'
      : /(\s|^)WARN(ING)?[:\s]/i.test(line) ? 'log-warn' : '');
  var div = document.createElement('div');
  div.className = 'log-line' + (cls ? ' ' + cls : '');
  div.textContent = line;
  div._raw = line;
  viewer.appendChild(div);
  while (viewer.childElementCount > 2000) viewer.removeChild(viewer.firstChild);
  if (document.getElementById('log-search').value.trim()) logApplySearch(true);
  if (_logPinned && !logIsPaused()) viewer.scrollTop = viewer.scrollHeight;
}

function logSearchEnable(on) {
  var inp = document.getElementById('log-search');
  inp.disabled = !on;
  document.getElementById('log-search-prev').disabled = !on;
  document.getElementById('log-search-next').disabled = !on;
  if (!on) {
    inp.value = '';
    document.getElementById('log-search-count').textContent = '';
  }
}

function logApplySearch(preserve) {
  var selected = preserve === true && _logMatches[_logMatchIdx];
  var selectedLine = selected && selected.parentElement;
  var occurrence = selectedLine ? Array.from(selectedLine.querySelectorAll('.log-mark')).indexOf(selected) : -1;
  var q = document.getElementById('log-search').value.trim();
  var viewer = document.getElementById('log-viewer');
  _logMatches = [];
  _logMatchIdx = -1;
  var count = 0;
  viewer.querySelectorAll('.log-line').forEach(function (el) {
    var raw = el._raw || '';
    if (!q) { el.textContent = raw; return; }
    var lower = raw.toLowerCase();
    var ql = q.toLowerCase();
    var html = '';
    var pos = 0;
    var idx;
    while ((idx = lower.indexOf(ql, pos)) !== -1) {
      html += miEsc(raw.slice(pos, idx))
           + '<mark class="log-mark">' + miEsc(raw.slice(idx, idx + q.length)) + '</mark>';
      pos = idx + q.length;
      count++;
    }
    html += miEsc(raw.slice(pos));
    el.innerHTML = html;
    el.querySelectorAll('.log-mark').forEach(function (m, i) {
      if (el === selectedLine && i === occurrence) _logMatchIdx = _logMatches.length;
      _logMatches.push(m);
    });
  });
  document.getElementById('log-search-count').textContent =
      q ? (count ? count + ' match' + (count === 1 ? '' : 'es') : 'no matches') : '';
  logMatchStyle();
}

function logMatchStyle() {
  _logMatches.forEach(function (m, i) {
    m.classList.toggle('log-cur', i === _logMatchIdx);
  });
}

function logSearchStep(dir) {
  if (!_logMatches.length) return;
  _logMatchIdx = _logMatchIdx < 0 ? (dir < 0 ? _logMatches.length - 1 : 0)
    : (_logMatchIdx + dir + _logMatches.length) % _logMatches.length;
  logMatchStyle();
  var cur = _logMatches[_logMatchIdx];
  if (cur) {
    _logPinned = false;
    cur.scrollIntoView({ block: 'center' });
  }
}

function logPauseToggle() {
  _logPaused = !_logPaused;
  var btn = document.getElementById('log-pause-btn');
  btn.textContent = _logPaused ? 'Resume scrolling' : 'Pause scrolling';
  btn.classList.toggle('active', _logPaused);
  if (!_logPaused) {
    _logPinned = true;
    var viewer = document.getElementById('log-viewer');
    viewer.scrollTop = viewer.scrollHeight;
  }
}

function logTailChange() {
  if (_logEs) logStop();
  if (_logOpen) logStart();
}

function logStop() {
  if (_logEs) {
    _logEs.close();
    _logEs = null;
  }
  _logPaused = false;
  _logMouseHold = false;
  _logPinned = true;
  var pbtn = document.getElementById('log-pause-btn');
  pbtn.textContent = 'Pause scrolling';
  pbtn.classList.remove('active');
  logSetStatus('stopped', 'stopped');
  document.getElementById('log-stop-btn').disabled = true;
  document.getElementById('log-start-btn').disabled = false;
  document.getElementById('log-pause-btn').disabled = true;
  logSearchEnable(true);
}

function logClear() {
  var viewer = document.getElementById('log-viewer');
  viewer.innerHTML = '';
  _logMatches = [];
  _logMatchIdx = -1;
  document.getElementById('log-search-count').textContent = '';
}

function logSetStatus(state, text) {
  var dot  = document.getElementById('log-dot');
  var txt  = document.getElementById('log-status-text');
  var btn  = document.getElementById('log-toggle-btn');
  dot.className = 'log-dot ' + state;
  txt.textContent = text;
  if (state === 'streaming') {
    btn.classList.add('active');
  } else {
    btn.classList.remove('active');
  }
}

document.addEventListener('DOMContentLoaded', function () {
  var viewer = document.getElementById('log-viewer');
  // mouse-down hold (transient) + sticky Pause button; unpin when scrolling up
  viewer.addEventListener('mousedown', function () { _logMouseHold = true; });
  window.addEventListener('mouseup', function () { _logMouseHold = false; });
  viewer.addEventListener('scroll', function () {
    _logPinned = viewer.scrollTop + viewer.clientHeight
               >= viewer.scrollHeight - 8;
  });
  var s = document.getElementById('log-search');
  s.addEventListener('input', logApplySearch);
  s.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      logSearchStep(e.shiftKey ? -1 : 1);
    } else if (e.key === 'Escape') {
      s.value = '';
      logApplySearch();
    }
  });

});

