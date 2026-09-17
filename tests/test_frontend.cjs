// Lightweight DOM regressions without a frontend build or package install.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/index.html', 'utf8');
function loadFunction(name, context) {
  const start = source.indexOf('function ' + name + '(');
  assert(start >= 0, 'missing function ' + name);
  const end = source.indexOf('\nfunction ', start + 1);
  vm.runInContext(source.slice(start, end), context);
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
  return Promise.resolve({ json: () => ({ ok: true }) });
};
loadFunction('miPost', context);
context.miPost('/api/models/undo', {});
assert.equal(captured.headers['If-Match'], '"current"');
context.miPost('/api/models/section/edit', {}, 'editor-snapshot');
assert.equal(captured.headers['If-Match'], '"editor-snapshot"');
console.log('Frontend regressions passed');
