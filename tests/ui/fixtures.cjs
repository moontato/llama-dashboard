const clone = value => JSON.parse(JSON.stringify(value));
function model(name, region, path, archived = false) {
  return { name, region, archived, model: path, model_exists: path ? true : null,
    model_size_bytes: path ? 8589934592 : null,
    keys: [...(path ? [{key: 'model', value: path}] : []), {key: 'ctx-size', value: '32768'}, {key: 'temp', value: '0.7'}] };
}
function createState() {
  return { revision: 1, undo: null, failNext: null, requests: [], writable: true, downloads: [],
    raw: '[*]\nctx-size = 32768\n\n[Qwen3-8B]\nmodel = /models/Qwen3-8B-Q4_K_M.gguf\ntemp = 0.7\n',
    models: [model('*', 'global', ''), model('Coding', 'profiles', '/models/Qwen3-8B-Q4_K_M.gguf'),
      model('Qwen3-8B', 'models', '/models/Qwen3-8B-Q4_K_M.gguf'),
      {...model('Llama-3.3-70B', 'models', '/models/Llama-3.3-70B-Q4_K_M.gguf'), model_exists: false},
      model('Old preset', 'archived_models', '/models/old.gguf', true)] };
}
async function installFixtures(context, state) {
  await context.addInitScript(() => {
    window.__streams = [];
    window.__tick = {
      ts: 1770000000, ram: {pct: 38.4, used_gib: 24.6, total_gib: 64, shared_gib: 8.1},
      swap: {pct: 0, used_gib: 0, total_gib: 32}, gpu_pct: 21, temp_c: 48.2, power_w: 28.6, fan_pct: 35,
      cpu_pct: [12, 9, 28, 14, 7, 8, 12, 5, 17, 5, 10, 8], nvp: 'MAXN', state: 'ok',
      history: {ram_pct: [22,23,24,25,25,35,37,39,38,38], gpu_pct: [0,2,8,3,4,15,42,32,24,21]},
      llama: {state: 'active', uptime_s: 7263, model: 'Qwen3-8B', model_status: 'loaded', model_configured: true},
      disk: {pct: 42, used_gib: 390.6, total_gib: 930, path: '/mnt/ssd/models'}
    };
    window.EventSource = class {
      constructor(url) {
        this.url = url; this.closed = false; window.__streams.push(this);
        setTimeout(() => {
          if (this.closed) return;
          this.onopen?.({});
          if (url === '/stream') {
            this.onmessage?.({data: JSON.stringify({board: {model: 'Jetson AGX Orin', jetpack: '6.2'}})});
            this.tick();
            this.timer = setInterval(() => this.tick(), 1000);
          } else this.onmessage?.({data: 'llama-server: ready for requests'});
        }, 10);
      }
      tick() { if (!this.closed) this.onmessage?.({data: JSON.stringify(window.__tick)}); }
      close() { this.closed = true; clearInterval(this.timer); }
    };
  });
  await context.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const body = req.method() === 'POST' ? (req.postDataJSON() || {}) : {};
    state.requests.push({path: url.pathname, method: req.method(), body});
    const reply = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    if (state.failNext && url.pathname === state.failNext.path) {
      const failure = state.failNext; state.failNext = null;
      if (failure.network) return route.abort('failed');
      return reply({ok: false, error: 'Test failure'}, failure.status || 500);
    }
    if (url.pathname === '/api/models' && req.method() === 'GET') {
      const aliases = {};
      state.models.filter(s => !s.archived && s.model).forEach(s => (aliases[s.model] ||= []).push(s.name));
      return reply({ok: true, writable: state.writable, write_reason: state.writable ? '' : 'file is read-only',
        revision: 'r' + state.revision, models: clone(state.models), aliases, raw: state.raw,
        undo_available: !!state.undo, git: {ok: true, branch: 'main', dirty: true, ahead: 1, behind: 0, last_commit: 'abc123 Tune presets'}});
    }
    if (url.pathname === '/api/models/downloads') {
      if (req.method() === 'GET') return reply({ok: true, root: '/models', jobs: clone(state.downloads)});
      if (state.downloads.some(j => ['connecting', 'downloading'].includes(j.state))) return reply({ok: false, error: 'A download is already running'}, 409);
      const original = decodeURIComponent(new URL(body.url).pathname.split('/').pop());
      let name = body.filename || original;
      if (!name.endsWith('.gguf')) name += '.gguf';
      const destination = (body.subdirectory ? body.subdirectory + '/' : '') + name;
      const job = {id: String(state.downloads.length + 1), state: 'downloading', filename: name, destination,
        path: '/models/' + destination, downloaded_bytes: 1048576, total_bytes: 10485760, error: null};
      state.downloads.unshift(job);
      return reply({ok: true, job}, 202);
    }
    if (/^\/api\/models\/downloads\/[^/]+\/cancel$/.test(url.pathname)) {
      const job = state.downloads.find(j => j.id === url.pathname.split('/')[4]);
      if (!job) return reply({ok: false, error: 'Not found'}, 404);
      job.state = 'cancelled';
      return reply({ok: true, job});
    }
    if (url.pathname === '/api/models/files') return reply({ok: true, gguf: [{path: '/models/Qwen3-8B-Q4_K_M.gguf', rel: 'Qwen3-8B-Q4_K_M.gguf', size_gib: 8}], mmproj: [], mtp: []});
    if (url.pathname === '/api/models/raw/check') return reply({ok: true, sections: 2});
    if (url.pathname === '/api/models/raw/diff') return reply({ok: true, sections: 2, revision: 'r' + state.revision,
      changed: body.text !== state.raw, diff: body.text === state.raw ? [] : ['--- Latest saved file', '+++ Your draft', '-' + state.raw, '+' + body.text]});
    if (url.pathname === '/api/restart-llama') return reply({ok: true});
    if (url.pathname === '/api/models/backup') return route.fulfill({contentType: 'text/plain', body: state.raw, headers: {'Content-Disposition': 'attachment; filename=models.ini'}});
    if (url.pathname === '/api/models/git') { if (body.action !== 'push') state.undo = null; return reply({ok: true, output: 'Done', commit: 'def456 Saved configuration'}); }
    const header = req.headers()['if-match'];
    if (header && header !== '"r' + state.revision + '"') return reply({ok: false, error: 'models.ini changed; reload before saving'}, 409);
    if (url.pathname === '/api/models/undo') {
      if (!state.undo) return reply({ok: false, error: 'Nothing to undo'}, 400);
      state.models = state.undo.models; state.raw = state.undo.raw; state.undo = null; state.revision++;
      return reply({ok: true});
    }
    const old = {models: clone(state.models), raw: state.raw};
    if (url.pathname === '/api/models/raw') state.raw = body.text;
    else if (url.pathname === '/api/models/section/add') {
      if (state.models.some(s => s.name === body.name)) return reply({ok:false,error:'Section already exists'},400);
      const added = model(body.name, body.region, body.model);
      added.keys = [{key:'model',value:body.model}, ...Object.entries(body.params).map(([key,value])=>({key,value}))];
      state.models.push(added);
    } else {
      const index = state.models.findIndex(s => s.name === body.name && (url.pathname.endsWith('/restore') ? s.archived : s.archived === !!body.archived));
      if (index < 0) return reply({ok:false,error:'Section not found'},400);
      const section = state.models[index];
      if (url.pathname.endsWith('/edit')) {
        section.name = body.new_name || section.name;
        section.keys = section.keys.filter(kv => !(body.remove || []).includes(kv.key));
        Object.entries(body.set || {}).forEach(([key,value]) => {
          const kv = section.keys.find(kv => kv.key === key);
          if (kv) kv.value = value; else section.keys.push({key,value});
        });
        if (body.key_order) section.keys.sort((a,b) => (a.key === 'model' ? -1 : body.key_order.indexOf(a.key)) - (b.key === 'model' ? -1 : body.key_order.indexOf(b.key)));
        section.model = section.keys.find(kv => kv.key === 'model')?.value || '';
      } else if (url.pathname.endsWith('/archive')) { section.archived = true; section.region = 'archived_' + section.region; }
      else if (url.pathname.endsWith('/restore')) { section.archived = false; section.region = section.region.replace('archived_', ''); }
      else if (url.pathname.endsWith('/delete')) state.models.splice(index,1);
      else if (url.pathname.endsWith('/move')) {
        const [moved] = state.models.splice(index,1);
        const target = state.models.findIndex(s => s.name === body.target);
        state.models.splice(target + (body.position === 'after' ? 1 : 0),0,moved);
      } else return reply({ok:false,error:'Unmocked route'},404);
      state.raw = JSON.stringify(state.models);
    }
    state.undo = old; state.revision++;
    return reply({ok: true, revision: 'r' + state.revision});
  });
}
module.exports = {createState, installFixtures};
