// Hash router for the LCARS workspaces: #/home, #/media, #/vision, …
// Each route maps to a `.tab-pane#tab-<id>`; unknown routes fall back to Home.

export const DEFAULT_TAB = 'home';

// Public slug → pane id. Legacy ids (and the old "audio" tab) keep working.
const SLUG_TO_TAB = {
  home: 'home',
  media: 'controls',
  vision: 'perception',
  voice: 'voice',
  'smart-home': 'smart-home',
  settings: 'settings',
  overview: 'overview',
  system: 'system',
  controls: 'controls',
  perception: 'perception',
  audio: 'controls',
};
const TAB_TO_SLUG = {
  home: 'home', controls: 'media', perception: 'vision', voice: 'voice',
  'smart-home': 'smart-home', settings: 'settings', overview: 'overview', system: 'system',
};

let currentTab = null;

export function tabFromHash(hash) {
  const slug = String(hash || '').replace(/^#\/?/, '').split(/[/?]/)[0];
  return SLUG_TO_TAB[slug] || null;
}

export function hashForTab(tabId) {
  return '#/' + (TAB_TO_SLUG[tabId] || tabId);
}

function resolveTab(tabId) {
  const id = SLUG_TO_TAB[tabId] || tabId;
  return document.getElementById('tab-' + id) ? id : DEFAULT_TAB;
}

function scrambleFrameCodes() {
  document.querySelectorAll('.frame-code').forEach(code => {
    code.textContent = code.textContent.replace(/\d/g,
      digit => String((Number(digit) + 1 + Math.floor(Math.random() * 9)) % 10));
  });
}

function activate(tabId) {
  const pane = document.getElementById('tab-' + tabId);
  const previous = document.querySelector('.tab-pane.active');
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => {
    const selected = b.dataset.tab === tabId;
    b.classList.toggle('active', selected);
    b.setAttribute('aria-selected', String(selected));
    b.tabIndex = selected ? 0 : -1;
  });
  pane.classList.add('active');
  document.body.dataset.workspace = tabId;
  document.getElementById('workspace-title').textContent = pane.dataset.title;
  document.getElementById('workspace-description').textContent = pane.dataset.description;
  document.title = `VERA / ${pane.dataset.title}`;
  const previousTab = currentTab;
  currentTab = tabId;
  if (previous !== pane) {
    scrambleFrameCodes();
    if (previous) window.scrollTo({ top: 0, behavior: 'instant' });
  }
  if (previousTab !== tabId) {
    window.dispatchEvent(new CustomEvent('vera:route', { detail: { tab: tabId, previous: previousTab } }));
  }
}

export function currentRoute() {
  return currentTab;
}

export function switchTab(tabId) {
  const id = resolveTab(tabId);
  activate(id);
  const hash = hashForTab(id);
  if (location.hash !== hash) history.pushState(null, '', hash);
}

export function openWorkspace(tabId, cardId) {
  switchTab(tabId);
  const target = document.getElementById(cardId || 'workspace-title');
  target.closest('details')?.setAttribute('open', '');
  target.querySelector('details')?.setAttribute('open', '');
  target.setAttribute('tabindex', '-1');
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: 'start' });
}

export function railStardate(date = new Date()) {
  const year = date.getFullYear();
  const day = Math.floor((Date.UTC(year, date.getMonth(), date.getDate()) - Date.UTC(year, 0, 0)) / 86400000);
  return `${year}.${String(day).padStart(3, '0')}`;
}

function updateRailStardate() {
  const stardate = document.getElementById('rail-stardate');
  if (!stardate) return;
  const now = new Date();
  stardate.textContent = railStardate(now);
  stardate.dateTime = now.toISOString().slice(0, 10);
}

function onKeydown(event) {
  if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const buttons = Array.from(document.querySelectorAll('.tab-btn'));
  const index = buttons.indexOf(event.target);
  if (index < 0) return;
  event.preventDefault();
  const direction = ['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 1;
  const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (index + direction + buttons.length) % buttons.length;
  const next = buttons[nextIndex];
  switchTab(next.dataset.tab);
  next.focus();
}

export function initRouter() {
  // Retired: the URL hash replaces the per-browser "last tab" preference.
  localStorage.removeItem('vera-active-tab');
  updateRailStardate();
  setInterval(updateRailStardate, 60000);
  const followHash = () => {
    const tab = resolveTab(tabFromHash(location.hash) || DEFAULT_TAB);
    activate(tab);
    // Canonicalize legacy or unknown slugs without adding a history entry.
    if (location.hash !== hashForTab(tab)) history.replaceState(null, '', hashForTab(tab));
  };
  followHash();
  window.addEventListener('hashchange', followHash);
  document.querySelector('.tab-nav').addEventListener('keydown', onKeydown);
}

// Inline onclick handlers in index.html call these as globals.
Object.assign(window, { switchTab, openWorkspace, railStardate });
