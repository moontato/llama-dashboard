// ── raw editor helpers (single-file: no build, no CDN) ─────
function miEsc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function miRawHighlight(text) {
  var out = [];
  text.split('\n').forEach(function (line) {
    if (/^\s*(#|;)/.test(line)) {
      var cls = /^\s*# ==/.test(line) ? 'mi-hl-marker' : 'mi-hl-com';
      out.push('<span class="' + cls + '">' + miEsc(line) + '</span>');
    } else if (/^\[[^\]]*\]\s*$/.test(line)) {
      out.push('<span class="mi-hl-sec">' + miEsc(line) + '</span>');
    } else {
      var m = line.match(/^(\s*)([^=\s][^=]*?)(\s*=\s*)(.*)$/);
      if (m) {
        out.push(miEsc(m[1])
               + '<span class="mi-hl-key">' + miEsc(m[2]) + '</span>'
               + '<span class="mi-hl-eq">' + miEsc(m[3]) + '</span>'
               + miEsc(m[4]));
      } else {
        out.push(miEsc(line));
      }
    }
  });
  return out.join('\n') + '\n';  // trailing newline: keep last line's height
}

function miRawSync() {
  var text = miEl('mi-raw-text').value;
  miEl('mi-raw-hl').innerHTML = miRawHighlight(text);
  var g = miEl('mi-raw-gutter');
  var n = text.split('\n').length;
  var rows = [];
  for (var i = 1; i <= n; i++) rows.push('<div>' + i + '</div>');
  g.innerHTML = rows.join('');
}

var miRawCheckTimer = null;
function miRawLiveCheck() {
  clearTimeout(miRawCheckTimer);
  miRawCheckTimer = setTimeout(function () {
    var text = miEl('mi-raw-text').value;
    if (miEditorState.raw.busy || miEditorState.raw.conflict) return;
    if (!text.trim()) { miEl('mi-raw-validation').textContent = 'Empty document'; return; }
    miPost('/api/models/raw/check', { text: text }).then(function (d) {
      if (miEditorState.raw.busy || miEditorState.raw.conflict || miEl('mi-raw-text').value !== text) return;
      miEl('mi-raw-validation').textContent = d.ok ? d.sections + ' sections · valid structure' : d.error;
    }).catch(function () { /* network: keep last status */ });
  }, 500);
}

// ── models.ini editor ────────────────────────────────────────────
var miSelected = null;
var miLoadedName = null;
var miOperationBusy = false;
var miLoadSequence = 0;

function miShowEditor(kind) {
  document.querySelector('.action-menu').open = false;
  miEl('mi-selection-empty').hidden = true;
  miEl('mi-editor').hidden = kind !== 'edit';
  miEl('mi-add-editor').hidden = kind !== 'add';
  miEl('models-card').classList.add('detail-open');
}
function miBackToList() {
  miEl('models-card').classList.remove('detail-open');
  miEl('mi-filter').focus({ preventScroll: true });
}
function miOpenAdd() {
  miShowEditor('add');
  miLoadFiles();
  miEl('mi-add-name').focus({ preventScroll: true });
}
async function miDiscardAdd() {
  if (!await miMayDiscard('add')) return;
  ['name', 'model', 'params'].forEach(function (key) { miEl('mi-add-' + key).value = ''; });
  miEl('mi-add-region').value = 'models';
  miAcceptDraft('add');
  miOpStatus('mi-add-status', 'Draft discarded', true);
}
function miMarkLoaded(name) {
  miLoadedName = name;
  document.querySelectorAll('.mi-row').forEach(function (row) {
    var badge = row.querySelector('.mi-badge-loaded');
    if (badge) badge.hidden = !(name && row.dataset.name === name && row.dataset.archived === 'false');
  });
}
function miSyncSelection() {
  document.querySelectorAll('.mi-row').forEach(function (row) {
    var selected = !!miSelected && row.dataset.name === miSelected.name && row.dataset.archived === String(miSelected.archived);
    row.classList.toggle('selected', selected);
    row.querySelector('.mi-select').setAttribute('aria-pressed', String(selected));
  });
  miMarkLoaded(miLoadedName);
  if (!miSelected || !miData) return;
  var section = miData.models.find(function (s) { return s.name === miSelected.name && s.archived === miSelected.archived; });
  miEl('mi-editor-title').textContent = miSelected.name;
  miEl('mi-editor-region').textContent = section ? REGION_LABELS[section.region] : 'Unavailable section';
  var aliases = section && section.model && miData.aliases[section.model];
  miEl('mi-selection-meta').textContent = section ?
    (section.model || 'Shared configuration') + (aliases && aliases.length > 1 ? ' · Aliases: ' + aliases.join(', ') : '') : '';
  var notice = miEl('mi-selection-notice');
  notice.replaceChildren();
  notice.hidden = !!section && miEditRevision === miData.revision;
  if (!notice.hidden) {
    notice.appendChild(document.createTextNode(section ? 'The file changed since this editor was opened. Your draft is preserved. ' : 'This section was deleted or renamed. Your draft is preserved. '));
    var reload = document.createElement('button');
    reload.className = 'mini-btn'; reload.textContent = 'Reload latest…'; reload.onclick = miReloadEdit;
    notice.appendChild(reload);
  }
  var globalSection = section && section.region === 'global';
  miEl('mi-archive-btn').hidden = !section || globalSection || section.archived;
  miEl('mi-restore-btn').hidden = !section || globalSection || !section.archived;
  miEl('mi-delete-btn').hidden = !section || globalSection;
  miEl('mi-newname').readOnly = !!globalSection;
}
function miSelectedAction(action) {
  if (miSelected) miAct(action, miSelected.name, miSelected.archived);
}
function miMoveSelected(direction) {
  document.querySelector('.action-menu').open = false;
  if (!miSelected) return;
  var index = miData.models.findIndex(function (s) { return s.name === miSelected.name && s.archived === miSelected.archived; });
  if (index >= 0) miStep(index, direction);
}
function miCopySection(selectId) {
  var value = miEl(selectId).value;
  return miData.models.find(function (s) { return JSON.stringify([s.name, s.archived]) === value; });
}

var miData = null;          // last /api/models payload
var miOriginal = null;      // {key:value} snapshot of the row being edited
var miOriginalOrder = [];   // parameter order before editing
var miEditRevision = null;
var miRawRevision = null;
var miEditArchived = false; // archived-state of the row being edited
var miRegionOpen = { global: true, profiles: true, models: true };      // per-region open/closed; every region starts collapsed

var REGION_LABELS = {
  'global': 'Global',
  'profiles': 'Profiles',
  'archived_profiles': 'Archived profiles',
  'models': 'Models',
  'archived_models': 'Archived models',
};

function miPost(url, body, revision) {
  var headers = { 'Content-Type': 'application/json' };
  if (revision === undefined) revision = miData && miData.revision;
  if (revision) headers['If-Match'] = '"' + revision + '"';
  return uiRequest(url, {
    method: 'POST',
    headers: headers,
    body: JSON.stringify(body || {}),
  });
}

function miEl(id) { return document.getElementById(id); }

function miOpStatus(id, msg, ok) {
  var el = miEl(id);
  el.textContent = msg;
  el.className = 'restart-status ' + (ok ? 'ok' : 'err');
}

function miGiB(bytes) {
  var g = bytes / 1073741824;
  return (g >= 10 ? Math.round(g) : g.toFixed(1)) + ' GiB';
}

var miFiles = null;   // cached /api/models/files payload
var miFilter = '';    // active section filter query

function miDlForKey(key) {
  var kk = String(key || '').toLowerCase();
  if (kk === 'mmproj') return 'mi-dl-mmproj';
  if (kk === 'model-draft' || kk === 'draft') return 'mi-dl-mtp';
  return '';
}

function miLoadFiles() {
  fetch('/api/models/files')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d && d.ok) { miFiles = d; miFillDatalists(); }
    })
    .catch(function () {});
}

function miFillDatalists() {
  if (!miFiles) return;
  var cats = { 'mi-dl-gguf': 'gguf', 'mi-dl-mmproj': 'mmproj', 'mi-dl-mtp': 'mtp' };
  Object.keys(cats).forEach(function (id) {
    var dl = miEl(id);
    dl.innerHTML = '';
    miFiles[cats[id]].forEach(function (f) {
      var opt = document.createElement('option');
      opt.value = f.path;
      opt.label = f.rel + '  (' + f.size_gib.toFixed(1) + ' GiB)';
      dl.appendChild(opt);
    });
  });
}

function miApplyFilter() {
  var q = miFilter.trim().toLowerCase();
  var rows = document.querySelectorAll('#models-table .mi-row');
  var shown = 0;
  Array.prototype.forEach.call(rows, function (row) {
    var region = miEl('mi-region-filter').value;
    var hit = (!q || row.dataset.search.indexOf(q) !== -1) && (!region || row.dataset.region === region);
    row.classList.toggle('mi-hidden', !hit);
    if (hit) shown++;
  });
  Array.prototype.forEach.call(
    document.querySelectorAll('#models-table .mi-region-wrap'),
    function (wrap) {
      var any = wrap.querySelector('.mi-row:not(.mi-hidden)');
      wrap.classList.toggle('mi-all-hidden', !any);
      if ((q || miEl('mi-region-filter').value) && any) {            // force-open regions while filtering
        var rg = any.dataset.region;
        miRegionOpen[rg] = true;
        wrap.classList.remove('closed');
        var chev = wrap.querySelector('.mi-chev');
        if (chev) chev.textContent = '\u25be';
      }
    });
  miEl('mi-filter-count').textContent = shown + ' / ' + rows.length;
  miEl('mi-list-empty').hidden = shown > 0;
  miEl('mi-list-empty').textContent = rows.length ? 'No sections match your filters.' : 'No sections found. Check the configuration file or try Refresh.';
}

function miRenderRow(s, i, reorder, ro) {
  var row = document.createElement('div');
  row.className = 'mi-row' + (s.archived ? ' archived' : '');
  row.dataset.name = s.name;
  row.dataset.archived = String(s.archived);
  row.dataset.region = s.region;
  row.dataset.search = [s.name, s.region, s.model || '',
    s.keys.map(function (kv) { return kv.key + ' ' + kv.value; }).join(' ')].join(' ').toLowerCase();
  var button = document.createElement('button');
  button.className = 'mi-select';
  button.type = 'button';
  button.onclick = function () {
    var index = miData.models.findIndex(function (item) { return item.name === s.name && item.archived === s.archived; });
    miEdit(index);
  };
  var name = document.createElement('span');
  name.className = 'mi-name';
  name.textContent = s.name;
  if (s.archived || s.model_exists === false) {
    var badge = document.createElement('span');
    badge.className = 'mi-badge' + (s.model_exists === false ? ' mi-badge-missing' : '');
    badge.textContent = s.model_exists === false ? 'Missing file' : 'Archived';
    name.appendChild(badge);
  }
  var loaded = document.createElement('span');
  loaded.className = 'mi-badge mi-badge-loaded';
  loaded.textContent = 'Loaded'; loaded.hidden = true;
  name.appendChild(loaded);
  var model = document.createElement('span');
  model.className = 'mi-model';
  model.textContent = s.model ? s.model.split('/').pop() : 'Shared configuration';
  if (s.model_size_bytes != null) model.textContent += ' · ' + miGiB(s.model_size_bytes);
  model.title = s.model || '';
  button.append(name, model);
  row.appendChild(button);
  if (!ro && reorder) {
    // Drag from the row; the detail action menu provides keyboard alternatives.
    var grip = document.createElement('span');
    grip.className = 'mi-grip'; grip.textContent = '⠿'; grip.title = 'Drag to reorder';
    grip.setAttribute('aria-hidden', 'true'); row.appendChild(grip);
    miWireDrag(row, s);
  }
  return row;
}

var miDrag = null;

function miClearDrops() {
  var els = document.querySelectorAll(
    '.mi-row.drop-before, .mi-row.drop-after');
  Array.prototype.forEach.call(els, function (el) {
    el.classList.remove('drop-before', 'drop-after');
  });
}

function miWireDrag(row, s) {
  var grip = row.querySelector('.mi-grip');
  grip.addEventListener('mousedown', function () { row.draggable = true; });
  grip.addEventListener('mouseup',   function () { row.draggable = false; });
  row.addEventListener('dragstart', function (e) {
    if (row.draggable !== true) return;
    miDrag = { name: s.name, archived: s.archived,
               region: s.region, el: row };
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', s.name); } catch (err) {}
    row.classList.add('mi-dragging');
  });
  row.addEventListener('dragend', function () {
    row.draggable = false;
    row.classList.remove('mi-dragging');
    miClearDrops();
    miDrag = null;
  });
  row.addEventListener('dragover', function (e) {
    if (!miDrag || miDrag.el === row ||
        row.dataset.region !== miDrag.region) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    var r = row.getBoundingClientRect();
    var pos = (e.clientY - r.top) < r.height / 2 ? 'before' : 'after';
    if (row.classList.contains('drop-' + pos)) return;
    miClearDrops();
    row.classList.add('drop-' + pos);
  });
  row.addEventListener('dragleave', function (e) {
    if (e.relatedTarget && row.contains(e.relatedTarget)) return;
    row.classList.remove('drop-before', 'drop-after');
  });
  row.addEventListener('drop', function (e) {
    if (!miDrag || miDrag.el === row ||
        row.dataset.region !== miDrag.region) return;
    e.preventDefault();
    var r = row.getBoundingClientRect();
    var pos = (e.clientY - r.top) < r.height / 2 ? 'before' : 'after';
    var d = miDrag;
    miClearDrops();
    row.classList.remove('mi-dragging');
    miDrag = null;
    row.draggable = false;
    miMove(d.name, d.archived, row.dataset.name, pos);
  });
}

function miMove(name, archived, target, position) {
  miOpStatus('models-op-status', 'moving\u2026', true);
  miPost('/api/models/section/move', {
    name: name, target: target, position: position, archived: archived,
  }).then(function (d) {
    miOpStatus('models-op-status', d.ok ? '' : d.error, !!d.ok);
    if (d.ok) miLoad();
  }).catch(function () {
    miOpStatus('models-op-status', 'network error', false);
  });
}

function miStep(i, dir) {
  var ms = miData.models;
  var s = ms[i];
  var j = i + dir;
  if (j < 0 || j >= ms.length || ms[j].region !== s.region) return;
  miMove(s.name, s.archived, ms[j].name, dir < 0 ? 'before' : 'after');
}

function miRender(d) {
  var prev = miData;
  miData = d;
  miBuildAddCopySelect();
  var line = miEl('models-git-line');
  if (d.git && d.git.ok) {
    var bits = [d.git.branch, d.git.dirty ? 'dirty' : 'clean'];
    if (d.git.ahead != null && d.git.ahead > 0)  bits.push(d.git.ahead + ' ahead');
    if (d.git.behind != null && d.git.behind > 0) bits.push(d.git.behind + ' behind');
    bits.push(d.git.last_commit);
    line.textContent = bits.join(' · ');
    line.style.color = '';
  } else {
    line.textContent = 'git: ' + (d.git && d.git.error ? d.git.error : 'unavailable');
    line.style.color = 'var(--crit)';
  }

  var ro = !d.writable;
  miEl('models-readonly').classList.toggle('visible', ro);
  if (ro) miEl('models-readonly-why').textContent = 'read-only — ' + d.write_reason;

  var library = document.querySelector('.model-library');
  var scrollTop = library ? library.scrollTop : 0;
  var table = miEl('models-table');
  table.innerHTML = '';
  var counts = {};
  d.models.forEach(function (s) {
    counts[s.region] = (counts[s.region] || 0) + 1;
  });

  // regions whose member list changed since the previous load auto-open
  var changed = {};
  var curNames = {}, prevNames = {};
  d.models.forEach(function (s) {
    (curNames[s.region] || (curNames[s.region] = [])).push(s.name);
  });
  if (prev) prev.models.forEach(function (s) {
    (prevNames[s.region] || (prevNames[s.region] = [])).push(s.name);
  });
  if (prev) Object.keys(curNames).forEach(function (rg) {
    var a = curNames[rg].slice().sort();
    var b = (prevNames[rg] || []).slice().sort();
    if (JSON.stringify(a) !== JSON.stringify(b)) changed[rg] = true;
  });

  var region = null, body = null;
  d.models.forEach(function (s, i) {
    var isFirst = s.region !== region;
    if (isFirst) {
      region = s.region;
      var rg = region;
      var open = !!miRegionOpen[rg] || !!changed[rg];
      if (open) miRegionOpen[rg] = true;
      var wrap = document.createElement('div');
      wrap.className = 'mi-region-wrap' + (open ? '' : ' closed');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'mi-region';
      var chev = document.createElement('span');
      chev.className = 'mi-chev';
      chev.textContent = open ? '▾' : '▸';
      var count = document.createElement('span');
      count.className = 'mi-region-count';
      count.textContent = counts[rg];
      btn.appendChild(chev);
      btn.appendChild(document.createTextNode(REGION_LABELS[rg] || rg));
      btn.appendChild(count);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      body = document.createElement('div');
      body.className = 'mi-region-body';
      btn.onclick = function () {
        var nowOpen = !miRegionOpen[rg];
        miRegionOpen[rg] = nowOpen;
        wrap.classList.toggle('closed', !nowOpen);
        chev.textContent = nowOpen ? '▾' : '▸';
        btn.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
      };
      wrap.appendChild(btn);
      wrap.appendChild(body);
      table.appendChild(wrap);
    }
    var nxt = d.models[i + 1];
    var isLast = !nxt || nxt.region !== s.region;
    var reorder = (counts[s.region] || 0) >= 2
      ? { up: !isFirst, down: !isLast } : null;
    body.appendChild(miRenderRow(s, i, reorder, ro));
  });
  Array.prototype.forEach.call(
    document.querySelectorAll('#view-models .mi-writable'),
    function (b) { b.disabled = ro; }
  );
  var raw = miEl('mi-raw-text');
  if (raw) raw.readOnly = ro;

  // undo is available while the file has uncommitted edits (#9)
  miEl('models-undo-btn').hidden = !d.undo_available;

  miApplyFilter();
  miUpdateEditorState();
  miSyncSelection();
  if (library) library.scrollTop = scrollTop;
}

async function miLoad() {
  var sequence = ++miLoadSequence;
  try {
    var data = await uiRequest('/api/models');
    if (sequence !== miLoadSequence) return;
    if (!data.ok || !data.models) throw new Error(data.error || 'Could not load models');
    miRender(data);
  } catch (error) {
    if (sequence !== miLoadSequence) return;
    miOpStatus('models-op-status', 'Could not refresh models. Your drafts are preserved. Try Refresh.', false);
    if (!miData) miEl('mi-list-empty').textContent = 'Models unavailable. Try Refresh.';
  }
}

async function miAct(action, name, isArchived) {
  if (miOperationBusy || !miData.writable) return;
  miOperationBusy = true;
  try {
    if (!await miMayDiscard('edit')) return;
    if (!await uiConfirm(action === 'delete' ? 'Delete ' + name + '?' : action + ' ' + name + '?', {
      description: 'This changes models.ini. The current section draft will be closed; model files are not deleted.',
      apply: action === 'delete' ? 'Delete section' : 'Confirm ' + action, danger: action === 'delete'
    })) return;
    var body = action === 'delete' ? { name: name, archived: !!isArchived } : { name: name };
    miOpStatus('models-op-status', 'Working…', true);
    var data = await miPost('/api/models/section/' + action, body);
    miOpStatus('models-op-status', data.ok ? 'Section updated' : data.error, !!data.ok);
    if (data.ok) {
      miSelected = null;
      miEditorState.edit.baseline = null;
      miEditorState.edit.conflict = false;
      miEl('mi-editor').hidden = true;
      miEl('mi-selection-empty').hidden = false;
      miBackToList();
      await miLoad();
    }
  } catch (_) { miOpStatus('models-op-status', 'Network error — draft preserved', false); }
  finally { miOperationBusy = false; }
}

function miCommit() {
  miGit('commit');
}

async function miUndo() {
  if (miOperationBusy) return;
  miOperationBusy = true;
  try {
    if (!await uiConfirm('Undo the last file edit?', { description: 'Restores the whole file. Open drafts are preserved but may need to be reconciled.', apply: 'Undo file edit' })) return;
    var data = await miPost('/api/models/undo', {});
    miOpStatus('models-git-status', data.ok ? 'Last file edit undone' : data.error, !!data.ok);
    if (data.ok) await miLoad();
  } catch (_) { miOpStatus('models-git-status', 'Network error', false); }
  finally { miOperationBusy = false; }
}

async function miGit(action) {
  if (miOperationBusy) return;
  miOperationBusy = true;
  var input = miEl('models-commit-msg');
  var message = input.value;
  try {
    var descriptions = { commit: 'Commits the saved file only. Unsaved drafts are not included.', pull: 'Updates the saved file from Git. Open drafts stay intact and may conflict.', push: 'Pushes committed changes to the remote repository. Unsaved drafts are not included.' };
    if (!await uiConfirm(action.charAt(0).toUpperCase() + action.slice(1) + ' configuration?', { description: descriptions[action], apply: 'Confirm ' + action })) return;
    miOpStatus('models-git-status', 'Working…', true);
    var data = await miPost('/api/models/git', { action: action, message: message });
    miOpStatus('models-git-status', data.ok ? (data.commit || data.output || 'Done') : data.error, !!data.ok);
    if (data.ok) {
      if (action === 'commit' && input.value === message) input.value = '';
      await miLoad();
    }
  } catch (_) { miOpStatus('models-git-status', 'Network error', false); }
  finally { miOperationBusy = false; }
}

function miParamRow(key, value) {
  var row = document.createElement('div');
  row.className = 'mi-param';

  var step = document.createElement('div');
  step.className = 'mi-param-step';
  var up = document.createElement('button');
  up.type = 'button';
  up.className = 'mi-step';
  up.textContent = '\u2191';
  up.title = 'Move parameter up';
  up.setAttribute('aria-label', 'Move parameter up');
  up.onclick = function () { miStepParam(row, -1); };
  var grip = document.createElement('div');
  grip.className = 'mi-grip';
  grip.textContent = '\u22EE\u22EE';
  grip.title = 'drag to reorder';
  var down = document.createElement('button');
  down.type = 'button';
  down.className = 'mi-step';
  down.textContent = '\u2193';
  down.title = 'Move parameter down';
  down.setAttribute('aria-label', 'Move parameter down');
  down.onclick = function () { miStepParam(row, 1); };
  step.appendChild(up); step.appendChild(grip); step.appendChild(down);

  var k = document.createElement('input'); k.className = 'mi-k mi-input'; k.value = key;
  k.setAttribute('aria-label', 'Parameter key');
  var v = document.createElement('input'); v.className = 'mi-v mi-input'; v.value = value;
  var help = document.createElement('span'); help.className = 'mi-param-help';
  var labels = { 'ctx-size': 'Context size · tokens', 'temp': 'Temperature · sampling randomness',
    'top-p': 'Top-p · nucleus sampling', 'top-k': 'Top-k · candidate limit',
    'n-gpu-layers': 'GPU layers · offload configuration', 'mmproj': 'Vision projector · GGUF file',
    'model-draft': 'Draft model · speculative decoding' };
  function syncList() {
    var id = miDlForKey(k.value);
    if (id) v.setAttribute('list', id);
    else v.removeAttribute('list');
    v.setAttribute('aria-label', (k.value || 'Parameter') + ' value');
    help.textContent = labels[k.value] || 'Custom parameter · value is passed unchanged';
  }
  syncList();
  k.addEventListener('input', syncList);
  var del = document.createElement('button');
  del.className = 'mini-btn mi-danger';
  del.textContent = '×';
  del.setAttribute('aria-label', 'Remove parameter');
  del.onclick = function () { row.remove(); miRefreshParamSteps(); };
  row.appendChild(step); row.appendChild(k); row.appendChild(v); row.appendChild(del); row.appendChild(help);
  miWireParamDrag(row);
  return row;
}

function miClearParamDrops() {
  var els = miEl('mi-params').querySelectorAll(
    '.mi-param.drop-before, .mi-param.drop-after');
  Array.prototype.forEach.call(els, function (el) {
    el.classList.remove('drop-before', 'drop-after');
  });
}

function miWireParamDrag(row) {
  var box = miEl('mi-params');
  var grip = row.querySelector('.mi-grip');
  row.addEventListener('dragstart', function (e) {
    if (miEditorState.edit.busy) { e.preventDefault(); e.stopImmediatePropagation(); }
  });
  grip.addEventListener('mousedown', function () { row.draggable = !miEditorState.edit.busy; });
  grip.addEventListener('mouseup',   function () { row.draggable = false; });
  row.addEventListener('dragstart', function (e) {
    if (row.draggable !== true) return;
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', 'param'); } catch (err) {}
    row.classList.add('mi-dragging');
  });
  row.addEventListener('dragend', function () {
    row.draggable = false;
    row.classList.remove('mi-dragging');
    miClearParamDrops();
  });
  row.addEventListener('dragover', function (e) {
    var dragging = box.querySelector('.mi-dragging');
    if (!dragging || dragging === row) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    var r = row.getBoundingClientRect();
    var pos = (e.clientY - r.top) < r.height / 2 ? 'before' : 'after';
    if (row.classList.contains('drop-' + pos)) return;
    miClearParamDrops();
    row.classList.add('drop-' + pos);
  });
  row.addEventListener('dragleave', function (e) {
    if (e.relatedTarget && row.contains(e.relatedTarget)) return;
    row.classList.remove('drop-before', 'drop-after');
  });
  row.addEventListener('drop', function (e) {
    if (miEditorState.edit.busy) return;
    var dragging = box.querySelector('.mi-dragging');
    if (!dragging || dragging === row) return;
    e.preventDefault();
    var r = row.getBoundingClientRect();
    var pos = (e.clientY - r.top) < r.height / 2 ? 'before' : 'after';
    if (pos === 'before') box.insertBefore(dragging, row);
    else if (row.nextSibling) box.insertBefore(dragging, row.nextSibling);
    else box.appendChild(dragging);
    dragging.classList.remove('mi-dragging');
    dragging.draggable = false;
    miClearParamDrops();
    miRefreshParamSteps();
  });
}

function miStepParam(row, dir) {
  var box = miEl('mi-params');
  var rows = Array.prototype.slice.call(box.querySelectorAll('.mi-param'));
  var i = rows.indexOf(row);
  var j = i + dir;
  if (j < 0 || j >= rows.length) return;
  var other = rows[j];
  if (dir < 0) box.insertBefore(row, other);
  else box.insertBefore(row, other.nextSibling);
  miRefreshParamSteps();
}

function miRefreshParamSteps() {
  miUpdateEditorState();
  var rows = Array.prototype.slice.call(
    miEl('mi-params').querySelectorAll('.mi-param'));
  rows.forEach(function (row, i) {
    var steps = row.querySelectorAll('.mi-param-step .mi-step');
    if (steps.length < 2) return;
    steps[0].disabled = (i === 0);
    steps[1].disabled = (i === rows.length - 1);
  });
}

function miAddParam(key, value) {
  miEl('mi-params').appendChild(miParamRow(key, value));
  miRefreshParamSteps();
}

function miFillCopyOptions(sel, excludeIndex) {
  var previous = sel.value;
  sel.innerHTML = '';
  var ph = document.createElement('option');
  ph.value = '';
  ph.textContent = '(pick a section to copy from\u2026)';
  ph.disabled = true;
  ph.selected = true;
  sel.appendChild(ph);
  miData.models.forEach(function (s, i) {
    if (i === excludeIndex) return;   // skip the excluded section (copy self)
    var opt = document.createElement('option');
    opt.value = JSON.stringify([s.name, s.archived]);
    opt.textContent = s.name + (s.archived ? '  (archived)' : '');
    sel.appendChild(opt);
  });
  if (Array.from(sel.options).some(function (option) { return option.value === previous; })) sel.value = previous;
}

function miBuildCopySelect(currentIndex) {
  miFillCopyOptions(miEl('mi-copy-from'), currentIndex);
}

function miBuildAddCopySelect() {
  miFillCopyOptions(miEl('mi-add-copy-from'));   // no exclusion: new section
}

async function miCopyFrom() {
  if (miEditorState.edit.busy) return;
  var s = miCopySection('mi-copy-from');
  if (!s) {
    miOpStatus('mi-edit-status', 'pick a section to copy from first', false);
    return;
  }
  if (miIsDirty('edit') && !await uiConfirm('Replace draft parameters?', {
    description: 'Copies the model file and parameters from ' + s.name + '. Your section name is kept.', apply: 'Replace parameters'
  })) return;
  miEl('mi-model').value = s.model || '';
  var box = miEl('mi-params');
  box.innerHTML = '';
  s.keys.forEach(function (kv) {
    if (kv.key === 'model') return;
    box.appendChild(miParamRow(kv.key, kv.value));
  });
  miRefreshParamSteps();
  miOpStatus('mi-edit-status', 'copied from [' + s.name +
    '] \u2014 review, then save', true);
}

async function miRevertEdit() {
  if (!miOriginal || !await miMayDiscard('edit')) return;
  miEl('mi-newname').value = miEl('mi-name').value.trim();
  miEl('mi-model').value = miOriginal.model || '';
  var box = miEl('mi-params');
  box.innerHTML = '';
  miOriginalOrder.forEach(function (k) {
    box.appendChild(miParamRow(k, miOriginal[k]));
  });
  miRefreshParamSteps();
  miOpStatus('mi-edit-status', 'changes discarded', true);
}

async function miEdit(i, reloading) {
  var target = miData && miData.models[i];
  if (!target) return;
  if (!reloading && miSelected && miSelected.name === target.name && miSelected.archived === target.archived) {
    miShowEditor('edit'); return;
  }
  if (!reloading && !await miMayDiscard('edit')) return;
  i = miData.models.findIndex(function (s) { return s.name === target.name && s.archived === target.archived; });
  if (i < 0) return;
  miLoadFiles();
  var s = miData.models[i];
  miSelected = { name: s.name, archived: s.archived };
  miEditArchived = !!s.archived;
  miEditRevision = miData.revision;
  miEl('mi-name').value = s.name;
  miEl('mi-newname').value = s.name;
  miEl('mi-model').value = s.model || '';
  var box = miEl('mi-params');
  box.innerHTML = '';
  miOriginal = Object.create(null);
  miOriginalOrder = s.keys.filter(function (kv) { return kv.key !== 'model'; })
                          .map(function (kv) { return kv.key; });
  s.keys.forEach(function (kv) {
    if (kv.key === 'model') { miOriginal.model = kv.value; return; }
    miOriginal[kv.key] = kv.value;
    box.appendChild(miParamRow(kv.key, kv.value));
  });
  miBuildCopySelect(i);
  miRefreshParamSteps();
  miAcceptDraft('edit');
  miOpStatus('mi-edit-status', '', true);
  miShowEditor('edit');
  miSyncSelection();
  miEl('mi-newname').focus({ preventScroll: true });
}

function miEditSummary() {
  var bits = [];
  var name = miEl('mi-name').value.trim();
  var nn = miEl('mi-newname').value.trim();
  if (nn && nn !== name) bits.push('rename: ' + name + ' \u2192 ' + nn);
  var model = miEl('mi-model').value.trim();
  if (model && miOriginal.model !== model)
    bits.push('model \u2192 ' + model.split('/').pop());
  if (!model && miOriginal.model) bits.push('- model');
  var present = {};
  var order = [];
  miEl('mi-params').querySelectorAll('.mi-param').forEach(function (row) {
    var k = row.querySelector('.mi-k').value.trim();
    var v = row.querySelector('.mi-v').value.trim();
    if (!k) return;
    present[k] = true;
    order.push(k);
    if (!(k in miOriginal)) bits.push('+ ' + k + ' = ' + v);
    else if (miOriginal[k] !== v) bits.push(k + ': ' + miOriginal[k] + ' \u2192 ' + v);
  });
  Object.keys(miOriginal).forEach(function (k) {
    if (k === 'model' || present[k]) return;
    bits.push('- ' + k);
  });
  if (JSON.stringify(order) !== JSON.stringify(miOriginalOrder))
    bits.push('parameter order: ' + order.join(', '));
  return bits;
}

async function miSaveEdit() {
  if (miEditorState.edit.busy || miEditorState.edit.conflict) return;
  var name = miEl('mi-name').value.trim();
  var set = Object.create(null);
  var key_order = [];
  var model = miEl('mi-model').value.trim();
  if (model && miOriginal.model !== model) set.model = model;
  miEl('mi-params').querySelectorAll('.mi-param').forEach(function (row) {
    var k = row.querySelector('.mi-k').value.trim();
    var v = row.querySelector('.mi-v').value.trim();
    if (!k) return;
    set[k] = v;
    key_order.push(k);
  });
  var remove = [];
  Object.keys(miOriginal).forEach(function (k) {
    if (k === 'model') { if (!model) remove.push(k); return; }
    if (!(k in set)) remove.push(k);
  });
  var body = { name: name, set: set, remove: remove, key_order: key_order,
               archived: miEditArchived };
  var nn = miEl('mi-newname').value.trim();
  if (nn && nn !== name) body.new_name = nn;
  // preview the exact change before it hits the file (#6)
  var bits = miEditSummary();
  if (!bits.length) {
    miOpStatus('mi-edit-status', 'no changes', true);
    return;
  }
  miSetBusy('edit', true);
  if (!await uiDialog({ title: 'Save ' + name + '?', lines: bits,
    description: 'Writes models.ini only. No Git commit or server restart.', apply: 'Save changes',
    returnFocus: miEl('mi-edit-save') })) {
    miSetBusy('edit', false); return;
  }
  miOpStatus('mi-edit-status', 'saving…', true);
  miPost('/api/models/section/edit', body, miEditRevision).then(function (d) {
    miOpStatus('mi-edit-status', d.ok ? 'saved' : d.error, !!d.ok);
    if (d.ok) {
      miEditRevision = d.revision;
      miEl('mi-name').value = miEl('mi-newname').value.trim() || name;
      miEl('mi-newname').value = miEl('mi-name').value;
      miOriginal = { model: miEl('mi-model').value.trim() };
      miOriginalOrder = [];
      miEditDraft().params.forEach(function (pair) {
        if (pair[0]) { miOriginal[pair[0]] = pair[1]; miOriginalOrder.push(pair[0]); }
      });
      miSelected = { name: miEl('mi-name').value, archived: miEditArchived };
      miAcceptDraft('edit'); miLoad();
    }
    else miSaveConflict('edit', d);
  }).catch(function () {
    miOpStatus('mi-edit-status', 'Network error — draft preserved', false);
  }).finally(function () { miSetBusy('edit', false); });
}

async function miAddCopyFrom() {
  if (miEditorState.add.busy) return;
  var s = miCopySection('mi-add-copy-from');
  if (!s) {
    miOpStatus('mi-add-status', 'pick a section to copy from first', false);
    return;
  }
  if ((miEl('mi-add-model').value || miEl('mi-add-params').value) && !await uiConfirm('Replace new section parameters?', {
    description: 'Copies the model file and parameters from ' + s.name + '. Your name and region are kept.', apply: 'Replace parameters'
  })) return;
  miEl('mi-add-model').value = s.model || '';
  var lines = [];
  s.keys.forEach(function (kv) {
    if (kv.key === 'model') return;
    lines.push(kv.key + ' = ' + kv.value);
  });
  miEl('mi-add-params').value = lines.join('\n');
  miUpdateEditorState();
  miOpStatus('mi-add-status', 'copied from [' + s.name + ']', true);
}

async function miAdd() {
  if (miEditorState.add.busy || !miData || !miData.writable) return;
  var name = miEl('mi-add-name').value.trim();
  var model = miEl('mi-add-model').value.trim();
  var region = miEl('mi-add-region').value;
  var params = Object.create(null);
  var invalid = !name || !model;
  miEl('mi-add-params').value.split('\n').forEach(function (line) {
    line = line.trim();
    if (!line) return;
    var index = line.indexOf('=');
    var key = line.slice(0, index).trim();
    if (index <= 0 || !key || key === 'model' || Object.hasOwn(params, key)) { invalid = true; return; }
    params[key] = line.slice(index + 1).trim();
  });
  if (invalid) {
    miOpStatus('mi-add-status', 'Enter a name and model file. Parameters must be unique key = value lines; use the Model file field for model.', false);
    return;
  }
  miSetBusy('add', true);
  miOpStatus('mi-add-status', 'Creating section…', true);
  try {
    var data = await miPost('/api/models/section/add', { name: name, model: model, region: region, params: params });
    miOpStatus('mi-add-status', data.ok ? 'Created ' + name + ' in models.ini' : data.error, !!data.ok);
    if (data.ok) {
      ['name', 'model', 'params'].forEach(function (key) { miEl('mi-add-' + key).value = ''; });
      miAcceptDraft('add');
      await miLoad();
    }
  } catch (_) { miOpStatus('mi-add-status', 'Network error — draft preserved', false); }
  finally { miSetBusy('add', false); }
}

async function miRawLoad() {
  if (!await miMayDiscard('raw')) return;
  miSetBusy('raw', true);
  return fetch('/api/models')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.ok && d.raw !== undefined) {
        miRawRevision = d.revision;
        miEl('mi-raw-text').value = d.raw;
        miAcceptDraft('raw');
        miRawStatus('Loaded latest file', true);
        miRawSync();
        miRawLiveCheck();
      } else {
        miRawStatus('Load failed — draft preserved', false);
      }
    })
    .catch(function () {
      miRawStatus('Network error — draft preserved', false);
    }).finally(function () { miSetBusy('raw', false); });
}



function miRawSave() {
  if (miEditorState.raw.busy || miEditorState.raw.conflict) return;
  miSetBusy('raw', true);
  var text = miEl('mi-raw-text').value;
  var revision = miRawRevision;
  // parse-check + preview before anything is written (#6)
  miRawStatus('checking\u2026', true);
  miPost('/api/models/raw/diff', { text: text }).then(function (d) {
    if (!d.ok) { miRawStatus(d.error || 'invalid', false); miSetBusy('raw', false); return; }
    if (!d.changed) {
      miRawRevision = d.revision;
      miAcceptDraft('raw');
      miRawStatus('Already matches the saved file', true);
      miSetBusy('raw', false);
      return;
    }
    miShowDiff(d.diff, d.sections, function () {
      miSetBusy('raw', true);
      miRawStatus('saving\u2026', true);
      miPost('/api/models/raw', { text: text }, revision).then(function (r) {
        if (r.ok) {
          miRawRevision = r.revision;
          miAcceptDraft('raw');
          miRawStatus('saved', true);
          miLoad();
        }
        else { miRawStatus(r.error || 'save failed', false); miSaveConflict('raw', r); }
      }).catch(function () {
        miRawStatus('Network error — draft preserved', false);
      }).finally(function () { miSetBusy('raw', false); });
    }, function () { miSetBusy('raw', false); });
  }).catch(function () {
    miRawStatus('Network error — draft preserved', false);
    miSetBusy('raw', false);
  });
}

function miRawStatus(msg, ok) {
  var el = miEl('mi-raw-status');
  el.textContent = msg;
  el.className = 'restart-status ' + (ok ? 'ok' : 'err');
}

// Auto-load raw editor content whenever opened
(function () {
  var rawEditor = miEl('mi-editor-raw');
  if (!rawEditor) return;
  rawEditor.addEventListener('toggle', function () {
    if (rawEditor.open && miEditorState.raw.baseline === null) {
      miRawLoad();
    }
  });
})();

  // section filter (#1)
  miEl('mi-filter').addEventListener('input', function () {
    miFilter = this.value;
    miApplyFilter();
  });

miEl('mi-region-filter').addEventListener('change', miApplyFilter);
var rawEditorInput = miEl('mi-raw-text');
rawEditorInput.addEventListener('input', function () { miRawSync(); miRawLiveCheck(); });
rawEditorInput.addEventListener('scroll', function () {
  miEl('mi-raw-hl').scrollTop = rawEditorInput.scrollTop;
  miEl('mi-raw-hl').scrollLeft = rawEditorInput.scrollLeft;
  miEl('mi-raw-gutter').scrollTop = rawEditorInput.scrollTop;
});
