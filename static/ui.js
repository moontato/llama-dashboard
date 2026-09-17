// Shared UI primitives. Native dialogs provide focus containment and an inert backdrop.
var uiDialogActive = false;
function uiDialog(options) {
  if (uiDialogActive) return Promise.resolve(false);
  uiDialogActive = true;
  var dialog = document.getElementById('mi-diff-modal');
  var previous = options.returnFocus || document.activeElement;
  document.getElementById('mi-diff-title').textContent = options.title;
  document.getElementById('mi-diff-meta').textContent = options.description || '';
  var pre = document.getElementById('mi-diff-pre');
  pre.replaceChildren();
  pre.hidden = !options.lines || !options.lines.length;
  (options.lines || []).forEach(function (line) {
    var row = document.createElement('div');
    row.textContent = line || ' ';
    if (line.startsWith('+')) row.className = 'mi-diff-add';
    if (line.startsWith('-')) row.className = 'mi-diff-del';
    pre.appendChild(row);
  });
  var apply = document.getElementById('mi-diff-apply');
  var cancel = document.getElementById('mi-diff-cancel');
  apply.textContent = options.apply || 'Continue';
  apply.hidden = options.readonly || false;
  apply.className = 'mini-btn ' + (options.danger ? 'mi-danger' : 'mi-primary');
  cancel.textContent = options.readonly ? 'Close' : 'Cancel';
  return new Promise(function (resolve) {
    var accepted = false;
    apply.onclick = function () { accepted = true; dialog.close(); };
    cancel.onclick = function () { dialog.close(); };
    dialog.addEventListener('close', function done() {
      dialog.removeEventListener('close', done);
      uiDialogActive = false;
      resolve(accepted);
      // Callers release busy fieldsets after the promise settles.
      requestAnimationFrame(function () {
        if (previous && previous.isConnected && !previous.closest('[hidden]') && !previous.matches(':disabled')) previous.focus();
      });
    });
    dialog.showModal();
    cancel.focus();
  });
}
function uiConfirm(message, options) {
  return uiDialog(Object.assign({ title: message, apply: 'Continue' }, options || {}));
}
async function uiRequest(url, options) {
  var response = await fetch(url, options);
  var data;
  try { data = await response.json(); }
  catch (_) { throw new Error('The server returned an unreadable response. Your draft is preserved.'); }
  data.httpStatus = response.status;
  return data;
}
function miShowDiff(diff, sections, onApply, onClose) {
  return uiDialog({
    title: onApply ? 'Review changes' : 'Compare with latest',
    description: onApply ? 'Save to models.ini. This does not commit or restart the server.'
      : 'Read-only comparison. Your draft is preserved. Reload only if you want to discard it.',
    lines: diff, apply: 'Save changes', readonly: !onApply,
    returnFocus: onApply ? document.getElementById('mi-raw-save') : null
  }).then(function (accepted) {
    if (onClose) onClose();
    if (accepted && onApply) return onApply();
  });
}
async function restartLlama() {
  var btn = document.getElementById('restart-btn');
  if (btn.disabled) return;
  btn.disabled = true;
  if (!await uiConfirm('Restart llama-server?', {
    description: 'Active inference will be interrupted. Unsaved configuration is not applied.',
    apply: 'Restart server', danger: true, returnFocus: btn
  })) { btn.disabled = false; return; }
  var status = document.getElementById('restart-status');
  status.textContent = 'Restarting…';
  try {
    var data = await uiRequest('/api/restart-llama', { method: 'POST' });
    status.textContent = data.ok ? 'Restart requested. Status will update shortly.' : data.error;
    status.className = 'restart-status ' + (data.ok ? 'ok' : 'err');
    if (data.ok) { setTimeout(function () { btn.disabled = false; }, 30000); return; }
  } catch (_) { status.textContent = 'Network error. Check server status before retrying.'; }
  btn.disabled = false;
}
