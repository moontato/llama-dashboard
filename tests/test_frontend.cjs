// Lightweight DOM regressions without a frontend build or package install.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/models.js', 'utf8');
function loadFunction(name, context) {
  const start = source.search(new RegExp('^(?:async )?function ' + name + '\\(', 'm'));
  assert(start >= 0, 'missing function ' + name);
  const next = source.slice(start + 1).search(/\n(?:async )?function /);
  vm.runInContext(source.slice(start, next < 0 ? undefined : start + 1 + next), context);
}
function element(value = '') {
  return {
    value, children: [], attributes: {}, listeners: {},
    appendChild(child) { this.children.push(child); },
    setAttribute(k, v) { this.attributes[k] = v; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener(k, fn) { this.listeners[k] = fn; },
  };
}
const context = vm.createContext({
  document: { createElement: () => element() },
  miWireParamDrag() {}, miRefreshParamSteps() {}, miStepParam() {},
});
loadFunction('miDlForKey', context);
loadFunction('miParamRow', context);
const row = context.miParamRow('mmproj', '/model.gguf');
const key = row.children[1], value = row.children[2];
assert.equal(value.attributes.list, 'mi-dl-mmproj');
key.value = 'model-draft';
key.listeners.input();
assert.equal(value.attributes.list, 'mi-dl-mtp');
key.value = 'temperature';
key.listeners.input();
assert.equal(value.attributes.list, undefined);

let rows = [];
const fields = {
  'mi-name': element('a'), 'mi-newname': element('a'),
  'mi-model': element('/a.gguf'),
  'mi-params': { querySelectorAll: () => rows },
};
context.miEl = id => fields[id];
context.miOriginal = { model: '/a.gguf', x: '1', y: '2' };
context.miOriginalOrder = ['x', 'y'];
function param(k, v) {
  return { querySelector: selector => element(selector === '.mi-k' ? k : v) };
}
loadFunction('miEditSummary', context);
rows = [param('x', '1'), param('y', '2')];
assert.equal(context.miEditSummary().length, 0);
rows = [param('y', '2'), param('x', '1')];
assert.match(context.miEditSummary().join('\n'), /parameter order: y, x/);
let captured;
context.miData = { revision: 'current' };
context.fetch = (url, options) => {
  captured = options;
  return Promise.resolve({ status: 200, json: () => Promise.resolve({ ok: true }) });
};
context.uiRequest = async (url, options) => {
  const response = await context.fetch(url, options);
  return response.json();
};
loadFunction('miPost', context);
context.miPost('/api/models/undo', {});
assert.equal(captured.headers['If-Match'], '"current"');
context.miPost('/api/models/section/edit', {}, 'editor-snapshot');
assert.equal(captured.headers['If-Match'], '"editor-snapshot"');
console.log('Frontend regressions passed');
