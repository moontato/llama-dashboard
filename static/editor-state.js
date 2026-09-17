// Draft ownership is independent of model-list refreshes and navigation.
var miEditorState = {
  edit: { baseline: null, busy: false, conflict: false },
  raw: { baseline: null, busy: false, conflict: false },
  add: { baseline: null, busy: false, conflict: false }
};
function miEditDraft() {
  return {
    name: miEl('mi-newname').value.trim(), model: miEl('mi-model').value.trim(),
    params: Array.from(miEl('mi-params').querySelectorAll('.mi-param')).map(function (row) {
      return [row.querySelector('.mi-k').value.trim(), row.querySelector('.mi-v').value.trim()];
    })
  };
}
function miDraftValue(kind) {
  if (kind === 'raw') return miEl('mi-raw-text').value;
  if (kind === 'add') return JSON.stringify(['name', 'region', 'model', 'params'].map(function (key) {
    return miEl('mi-add-' + key).value;
  }));
  return JSON.stringify(miEditDraft());
}
function miIsDirty(kind) {
  var state = miEditorState[kind];
  return state.baseline !== null && state.baseline !== miDraftValue(kind);
}
function miUpdateEditorState() {
  ['edit', 'raw', 'add'].forEach(function (kind) {
    var state = miEditorState[kind];
    var dirty = miIsDirty(kind);
    var label = state.busy ? 'Working…' : state.conflict ? 'Conflict — draft preserved'
      : dirty ? 'Unsaved changes' : state.baseline === null ? 'Not loaded' : 'No unsaved changes';
    var badge = miEl('mi-' + kind + '-state');
    badge.textContent = label;
    badge.classList.toggle('mi-dirty', dirty || state.conflict);
    var writable = miData && miData.writable;
    miEl('mi-' + kind + '-save').disabled = state.busy || !dirty || state.conflict || !writable;
    miEl('mi-' + kind + '-fields').disabled = state.busy || !writable;
    var conflict = miEl('mi-' + kind + '-conflict');
    if (conflict) {
      conflict.hidden = !state.conflict;
      conflict.querySelectorAll('button').forEach(function (button) { button.disabled = state.busy; });
    }
  });
  miEl('nav-draft').hidden = !['edit', 'raw', 'add'].some(miIsDirty);
}
function miSetBusy(kind, busy) {
  miEditorState[kind].busy = busy;
  miUpdateEditorState();
}
function miAcceptDraft(kind) {
  miEditorState[kind].baseline = miDraftValue(kind);
  miEditorState[kind].conflict = false;
  miUpdateEditorState();
}
async function miMayDiscard(kind) {
  if (miEditorState[kind].busy) return false;
  if (!miIsDirty(kind)) return true;
  return uiConfirm('Discard unsaved changes?', {
    description: 'Your ' + (kind === 'raw' ? 'raw INI' : kind === 'add' ? 'new section' : 'section') + ' draft will be replaced. This cannot be undone.',
    apply: 'Discard draft', danger: true
  });
}
function miSaveConflict(kind, response) {
  if (response.httpStatus !== 409) return false;
  miEditorState[kind].conflict = true;
  miUpdateEditorState();
  return true;
}
function miConflictStatus(kind, message, ok) {
  if (kind === 'raw') miRawStatus(message, ok);
  else miOpStatus('mi-edit-status', message, ok);
}
function miCompareLatest(kind) {
  if (miEditorState[kind].busy) return;
  miSetBusy(kind, true);
  var work;
  if (kind === 'raw') {
    work = miPost('/api/models/raw/diff', { text: miDraftValue('raw') }).then(function (data) {
      if (!data.ok) throw new Error(data.error || 'Comparison failed');
      return miShowDiff(data.diff, data.sections, null);
    });
  } else {
    var draft = miEditDraft();
    work = uiRequest('/api/models').then(function (data) {
      if (!data.ok || !data.models) throw new Error(data.error || 'Load failed');
      var saved = data.models.find(function (section) {
        return section.name === miEl('mi-name').value && section.archived === miEditArchived;
      });
      var current = saved ? {
        name: saved.name, model: saved.model,
        params: saved.keys.filter(function (kv) { return kv.key !== 'model'; })
                          .map(function (kv) { return [kv.key, kv.value]; })
      } : 'Section was deleted or renamed';
      return miShowDiff(['Latest saved section:', JSON.stringify(current, null, 2), '',
                         'Your unsaved draft:', JSON.stringify(draft, null, 2)], null, null);
    });
  }
  return work.catch(function (error) {
    miConflictStatus(kind, error.message || 'Network error — draft preserved', false);
  }).finally(function () { miSetBusy(kind, false); });
}
async function miReloadEdit() {
  if (!await miMayDiscard('edit')) return;
  miSetBusy('edit', true);
  try {
    var data = await uiRequest('/api/models');
    if (!data.ok || !data.models) throw new Error(data.error || 'Load failed');
    var index = data.models.findIndex(function (section) {
      return section.name === miEl('mi-name').value && section.archived === miEditArchived;
    });
    if (index < 0) throw new Error('Section was deleted or renamed. Your draft is preserved; copy it before choosing another section.');
    miRender(data);
    await miEdit(index, true);
    miConflictStatus('edit', 'Reloaded latest section', true);
  } catch (error) {
    miConflictStatus('edit', error.message || 'Network error — draft preserved', false);
  } finally { miSetBusy('edit', false); }
}

document.addEventListener('DOMContentLoaded', function () {
  miEl('mi-editor').addEventListener('input', miUpdateEditorState);
  miEl('mi-raw-text').addEventListener('input', miUpdateEditorState);
  miEl('mi-add-editor').addEventListener('input', miUpdateEditorState);
  miAcceptDraft('add');
  miUpdateEditorState();
});
window.addEventListener('beforeunload', function (event) {
  if (['edit', 'raw', 'add'].some(function (kind) { return miIsDirty(kind) || miEditorState[kind].busy; })) {
    event.preventDefault(); event.returnValue = '';
  }
});
