// Downloads live on the server, independent of navigation and INI drafts.
var downloadRoot = '';
var downloadTimer = null;
var downloadBusy = false;
var downloadSeen = new Map();

function downloadToggle() {
  var panel = document.getElementById('download-panel');
  panel.hidden = !panel.hidden;
  document.getElementById('download-toggle').setAttribute('aria-expanded', String(!panel.hidden));
  if (!panel.hidden) {
    document.getElementById('download-url').focus();
    downloadPoll();
  }
}
function downloadPreview() {
  var name = document.getElementById('download-filename').value;
  try {
    if (!name) name = decodeURIComponent(new URL(document.getElementById('download-url').value).pathname.split('/').pop());
  } catch (_) { /* incomplete input */ }
  if (name && !/\.gguf$/i.test(name) && name.indexOf('.') === -1) name += '.gguf';
  var sub = document.getElementById('download-directory').value;
  document.getElementById('download-preview').textContent = 'Save to: ' +
    (downloadRoot || 'Model root') + '/' + (sub ? sub + '/' : '') + (name || '<filename.gguf>');
}
function downloadBytes(value) {
  return (value / 1048576).toFixed(1) + ' MiB';
}
function downloadRender(jobs) {
  var container = document.getElementById('download-jobs');
  var active = jobs.some(function (job) { return ['connecting', 'downloading'].includes(job.state); });
  document.getElementById('download-start').disabled = active || downloadBusy;
  // Keep existing controls/focus while updating progress.
  var ids = new Set(jobs.map(function (job) { return job.id; }));
  Array.from(container.children).forEach(function (el) { if (!ids.has(el.dataset.id)) el.remove(); });
  jobs.forEach(function (job) {
    var row = Array.from(container.children).find(function (el) { return el.dataset.id === job.id; });
    if (!row) {
      row = document.createElement('div'); row.className = 'download-job'; row.dataset.id = job.id;
      var title = document.createElement('strong'); title.className = 'wrap'; row.appendChild(title);
      var status = document.createElement('p'); status.setAttribute('role', 'status'); row.appendChild(status);
      var progress = document.createElement('progress'); progress.max = 100; progress.setAttribute('aria-label', 'Download progress'); row.appendChild(progress);
      var bytes = document.createElement('p'); bytes.className = 'muted'; row.appendChild(bytes);
      var cancel = document.createElement('button'); cancel.className = 'mini-btn'; cancel.textContent = 'Cancel';
      cancel.onclick = async function () {
        cancel.disabled = true;
        try {
          await downloadRequest('/api/models/downloads/' + job.id + '/cancel', {});
          document.getElementById('download-message').textContent = 'Cancellation requested…';
        } catch (err) {
          document.getElementById('download-message').textContent = err.message;
          cancel.disabled = false;
        }
        downloadPoll();
      };
      row.appendChild(cancel); container.appendChild(row);
    }
    var running = ['connecting', 'downloading'].includes(job.state);
    row.children[0].textContent = job.destination;
    var text = job.state + (job.error ? ': ' + job.error : '') +
      (job.state === 'completed' ? ' — Saved to ' + job.path + '. Select this file in Add model or an existing preset.' : '');
    if (row.children[1].textContent !== text) row.children[1].textContent = text;
    row.children[2].hidden = !running;
    if (job.total_bytes) row.children[2].value = Math.min(100, job.downloaded_bytes / job.total_bytes * 100);
    else row.children[2].removeAttribute('value');
    row.children[3].textContent = downloadBytes(job.downloaded_bytes) +
      (job.total_bytes ? ' / ' + downloadBytes(job.total_bytes) + ' (' + Math.floor(job.downloaded_bytes / job.total_bytes * 100) + '%)' : ' transferred');
    row.children[4].hidden = !running;
    if (job.state === 'completed' && downloadSeen.get(job.id) !== 'completed') miLoadFiles();
    downloadSeen.set(job.id, job.state);
  });
  downloadSeen.forEach(function (_, id) { if (!ids.has(id)) downloadSeen.delete(id); });
}
async function downloadRequest(url, body) {
  var options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  var response = await fetch(url, options);
  var data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || 'Download request failed');
  return data;
}
var downloadPolling = false;
async function downloadPoll() {
  clearTimeout(downloadTimer);
  if (downloadPolling) return;
  downloadPolling = true;
  try {
    var data = await downloadRequest('/api/models/downloads');
    downloadRoot = data.root;
    downloadPreview();
    downloadRender(data.jobs);
  } catch (_) {
    document.getElementById('download-message').textContent = 'Cannot reach download service. Retrying…';
  } finally {
    downloadPolling = false;
    downloadTimer = setTimeout(downloadPoll, 1500);
  }
}
document.getElementById('download-form').addEventListener('submit', async function (event) {
  event.preventDefault();
  if (downloadBusy) return;
  var message = document.getElementById('download-message');
  var url = document.getElementById('download-url').value.trim();
  var name = document.getElementById('download-filename').value;
  try {
    var parsed = new URL(url);
    if (parsed.protocol !== 'https:' || parsed.host !== 'huggingface.co' || parsed.username || parsed.password ||
        !/^\/[^/]+\/[^/]+\/(blob|resolve)\/[^/]+\/.+\.gguf$/i.test(parsed.pathname)) {
      throw new Error('Enter an https://huggingface.co URL to a .gguf file (blob or resolve).');
    }
    if (name && (/[/\\\x00-\x1f\x7f]/.test(name) || name.startsWith('.') || name !== name.trim() ||
        (name.includes('.') && !/\.gguf$/i.test(name)))) throw new Error('Use a plain filename ending in .gguf (no directories).');
    downloadBusy = true;
    document.getElementById('download-start').disabled = true;
    await downloadRequest('/api/models/downloads', { url: url, subdirectory: document.getElementById('download-directory').value, filename: name });
    message.textContent = 'Download started. You can leave this tab; restarting the dashboard server interrupts the download.';
  } catch (err) { message.textContent = err.message; }
  finally {
    downloadBusy = false;
    document.getElementById('download-start').disabled = false;
    downloadPoll();
  }
});
['download-url', 'download-directory', 'download-filename'].forEach(function (id) {
  document.getElementById(id).addEventListener('input', downloadPreview);
});
downloadPoll();
