const sidebarToggle = document.querySelector('#sidebar-toggle');
const clusterSection = document.querySelector('[data-sidebar-section="clusters"]');
const mobileNavToggle = document.querySelector('#mobile-nav-toggle');
const sidebarBackdrop = document.querySelector('#sidebar-backdrop');
const sidebar = document.querySelector('#app-sidebar');
const collapsedKey = 'cluster-builder-sidebar-collapsed';
const clustersOpenKey = 'cluster-builder-clusters-open';
const readPreference = key => {
  try { return localStorage.getItem(key); }
  catch (_) { return null; }
};
const writePreference = (key, value) => {
  try { localStorage.setItem(key, value); }
  catch (_) { /* Navigation bleibt ohne persistente Browserdaten funktionsfähig. */ }
};

const setCollapsed = collapsed => {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  if (sidebarToggle) {
    sidebarToggle.setAttribute('aria-pressed', String(collapsed));
    sidebarToggle.setAttribute('aria-label', collapsed ? 'Sidebar ausklappen' : 'Sidebar einklappen');
  }
};

setCollapsed(readPreference(collapsedKey) === '1');

if (clusterSection) {
  const storedOpen = readPreference(clustersOpenKey);
  if (storedOpen !== null) clusterSection.open = storedOpen === '1';
  document.body.classList.remove('sidebar-clusters-closed');
  clusterSection.addEventListener('toggle', () => {
    writePreference(clustersOpenKey, clusterSection.open ? '1' : '0');
  });
}

sidebarToggle?.addEventListener('click', () => {
  const collapsed = !document.body.classList.contains('sidebar-collapsed');
  setCollapsed(collapsed);
  writePreference(collapsedKey, collapsed ? '1' : '0');
});

const setMobileOpen = open => {
  document.body.classList.toggle('sidebar-open', open);
  mobileNavToggle?.setAttribute('aria-expanded', String(open));
  mobileNavToggle?.setAttribute('aria-label', open ? 'Navigation schließen' : 'Navigation öffnen');
};

mobileNavToggle?.addEventListener('click', () => {
  setMobileOpen(!document.body.classList.contains('sidebar-open'));
});

sidebarBackdrop?.addEventListener('click', () => setMobileOpen(false));

sidebar?.addEventListener('click', event => {
  if (event.target.closest('a') && window.matchMedia('(max-width: 860px)').matches) {
    setMobileOpen(false);
  }
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') setMobileOpen(false);
});

const desktopMedia = window.matchMedia('(min-width: 861px)');
const closeMobileNavigation = event => {
  if (event.matches) setMobileOpen(false);
};
if (desktopMedia.addEventListener) desktopMedia.addEventListener('change', closeMobileNavigation);
else desktopMedia.addListener(closeMobileNavigation);
