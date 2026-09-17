const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function load(name, ctx) {
  const source = fs.readFileSync(name === 'uiRequest' ? 'static/ui.js' : 'static/models.js', 'utf8');
  const start = source.search(new RegExp('^(?:async )?function ' + name + '\\(', 'm'));
  assert(start >= 0, 'missing function ' + name);
  const next = source.slice(start + 1).search(/\n(?:async )?function /);
  vm.runInContext(source.slice(start, next < 0 ? undefined : start + 1 + next), ctx);
}
function element(value = '') {
  return { value, disabled: false, hidden: false, textContent: '', open: true,
    classList: { toggle() {}, add() {}, remove() {} },
    querySelectorAll: () => [], addEventListener() {} };
}
const elements = {};
function el(id) { return elements[id] || (elements[id] = element()); }
let params = [['x', '1'], ['y', '2']];
el('mi-params').querySelectorAll = () => params.map(([k, v]) => ({
  querySelector: selector => element(selector === '.mi-k' ? k : v)
}));
el('mi-name').value = el('mi-newname').value = 'a';
el('mi-model').value = '/a.gguf';
el('mi-raw-text').value = '[a]\nx = 1\n';
let confirmResult = true;
let comparison;
let unload;
let status;
const context = vm.createContext({
  document: { addEventListener() {} },
  window: { addEventListener(name, fn) { if (name === 'beforeunload') unload = fn; } },
  miEl: el, miData: { writable: true, revision: 'initial' },
  miEditArchived: false, miEditRevision: 'initial', miRawRevision: 'initial',
  miOriginal: { model: '/a.gguf', x: '1', y: '2' }, miOriginalOrder: ['x', 'y'],
  uiConfirm: () => Promise.resolve(confirmResult),
  uiDialog: () => Promise.resolve(confirmResult),
  miOpStatus: (id, message) => { status = message; },
  miRawStatus: message => { status = message; },
  miRawSync() {}, miRawLiveCheck() {}, miLoad() {},
  miShowDiff: (...args) => { comparison = args; },
});
vm.runInContext(fs.readFileSync('static/editor-state.js', 'utf8'), context);
['uiRequest', 'miEditSummary', 'miSaveEdit', 'miRawLoad', 'miRawSave', 'miPost'].forEach(name => load(name, context));
const flush = () => new Promise(resolve => setImmediate(resolve));

async function main() {
  context.miAcceptDraft('edit');
  context.miAcceptDraft('raw');
  assert.equal(context.miIsDirty('edit'), false);
  assert.equal(el('mi-edit-save').disabled, true);
  params.reverse();
  context.miUpdateEditorState();
  assert.equal(context.miIsDirty('edit'), true, 'reorder is a draft change');
  assert.equal(el('mi-edit-state').textContent, 'Unsaved changes');
  assert.equal(el('mi-edit-save').disabled, false);
  confirmResult = false;
  assert.equal(await context.miMayDiscard('edit'), false);
  assert.equal(context.miIsDirty('edit'), true, 'cancel keeps draft');
  confirmResult = true;
  let prevented = false;
  unload({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true, 'navigation warns about dirty drafts');

  let requests = 0;
  let resolveSave;
  context.fetch = () => {
    requests++;
    return new Promise(resolve => { resolveSave = resolve; });
  };
  context.miSaveEdit();
  context.miSaveEdit();
  await flush();
  assert.equal(requests, 1, 'duplicate saves blocked');
  assert.equal(el('mi-edit-fields').disabled, true);
  assert.equal(el('mi-edit-save').disabled, true);
  resolveSave({ status: 409, json: () => Promise.resolve({ ok: false, error: 'changed elsewhere' }) });
  await flush();
  assert.equal(context.miEditorState.edit.busy, false);
  assert.equal(context.miEditorState.edit.conflict, true);
  assert.equal(context.miIsDirty('edit'), true);
  assert.equal(el('mi-edit-conflict').hidden, false);
  assert.equal(context.miEditRevision, 'initial', 'conflict never silently rebases');

  context.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: true, models: [
    { name: 'a', archived: false, model: '/new.gguf', keys: [{ key: 'x', value: '9' }] }
  ] }) });
  const draft = context.miDraftValue('edit');
  await context.miCompareLatest('edit');
  assert.equal(context.miDraftValue('edit'), draft);
  assert.equal(comparison[2], null, 'comparison has no overwrite button');
  assert.match(comparison[0].join('\n'), /new.gguf/);
  assert.match(comparison[0].join('\n'), /Your unsaved draft/);

  context.fetch = () => Promise.reject(new Error('Offline'));
  await context.miReloadEdit();
  assert.equal(context.miDraftValue('edit'), draft, 'failed reload preserves draft');
  assert.equal(context.miEditorState.edit.conflict, true);
  assert.equal(context.miEditorState.edit.busy, false);

  // A later successful section save updates its revision and snapshot.
  context.miEditorState.edit.conflict = false;
  context.fetch = () => Promise.resolve({ status: 200,
    json: () => Promise.resolve({ ok: true, revision: 'saved-section' }) });
  context.miSaveEdit();
  await flush();
  assert.equal(context.miEditRevision, 'saved-section');
  assert.equal(context.miIsDirty('edit'), false);
  assert.equal(context.miEditSummary().length, 0);

  // Raw reload errors and cancellation must never clear a draft.
  el('mi-raw-text').value = '[a]\nx = 2\n';
  confirmResult = false;
  requests = 0;
  context.fetch = () => { requests++; return Promise.reject(new Error('offline')); };
  await context.miRawLoad();
  assert.equal(requests, 0);
  confirmResult = true;
  await context.miRawLoad();
  assert.equal(el('mi-raw-text').value, '[a]\nx = 2\n');
  assert.equal(context.miIsDirty('raw'), true);
  assert.equal(context.miEditorState.raw.busy, false);

  context.fetch = () => Promise.resolve({ status: 200, json: () => Promise.resolve({
    ok: true, changed: true, diff: ['-x = 1', '+x = 2'], sections: 1
  }) });
  context.miRawSave();
  await flush();
  assert.equal(context.miEditorState.raw.busy, true, 'preview freezes the reviewed draft');
  comparison[3](); // cancel/close callback
  assert.equal(context.miEditorState.raw.busy, false);
  assert.equal(context.miIsDirty('raw'), true);
  context.miRawSave();
  await flush();
  const apply = comparison[2];
  comparison[3](); // UI closes modal before applying
  context.fetch = () => Promise.resolve({ status: 409,
    json: () => Promise.resolve({ ok: false, error: 'changed elsewhere' }) });
  apply();
  await flush();
  assert.equal(context.miEditorState.raw.conflict, true);
  assert.equal(context.miIsDirty('raw'), true);
  assert.equal(el('mi-raw-text').value, '[a]\nx = 2\n');
  context.fetch = () => Promise.resolve({ json: () => Promise.resolve({
    ok: true, raw: '[a]\nx = 9\n', revision: 'latest'
  }) });
  await context.miRawLoad();
  assert.equal(context.miIsDirty('raw'), false);
  assert.equal(context.miEditorState.raw.conflict, false);
  assert.equal(context.miRawRevision, 'latest');
  assert.equal(el('mi-raw-text').value, '[a]\nx = 9\n');
  console.log('Editor state regressions passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
