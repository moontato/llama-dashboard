// All scripts are local and loaded after the DOM. Navigation never recreates streams or drafts.
function navigateView(initial) {
  var models = location.hash === '#models';
  document.getElementById('view-overview').hidden = models;
  document.getElementById('view-models').hidden = !models;
  ['overview', 'models'].forEach(function (view) {
    var active = models === (view === 'models');
    var link = document.getElementById('nav-' + view);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  if (models && _logOpen) logToggle();
  if (models && !miFiles) miLoadFiles();
  document.dispatchEvent(new Event('ui:view'));
  if (!initial) {
    var heading = document.getElementById(models ? 'models-title' : 'overview-title');
    heading.setAttribute('tabindex', '-1');
    heading.focus({ preventScroll: true });
  }
}
function openLogs() {
  if (location.hash === '#models') location.hash = '#overview';
  navigateView(true);
  if (!_logOpen) logToggle();
  document.getElementById('log-toggle-btn').focus();
  document.querySelector('.logs-card').scrollIntoView({ block: 'nearest' });
}
window.addEventListener('hashchange', function () { navigateView(false); });
navigateView(true);
miLoad();
