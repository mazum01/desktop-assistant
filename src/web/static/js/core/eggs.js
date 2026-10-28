// Star Trek easter eggs. Front-end only: nothing here speaks, moves the head or
// changes a setting. Typed phrases are only heard outside form fields.

const MESSAGE_MS = 6000;
const RED_ALERT_MS = 10000;
const KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'ArrowLeft', 'ArrowRight', 'b', 'a'];
const BUFFER_MAX = 24;

// Typed outside any input, spaces ignored.
export const PHRASES = {
  engage: 'warp',
  makeitso: 'Acknowledged. Making it so.',
  teaearlgreyhot: 'Replicator offline. Please visit the galley.',
  earlgrey: 'Replicator offline. Please visit the galley.',
  beammeup: 'beam',
  energize: 'beam',
  redalert: 'red-alert',
  computer: 'Working.',
  resistanceisfutile: 'Borg signature detected. Shields adapting… adaptation complete.',
  fascinating: 'Indeed.',
  khan: 'Ceti alpha five is not on our flight plan.',
  livelong: 'And prosper. 🖖',
  qapla: 'Qapla’! Today is a good day to compute.',
};

// Hash routes that aren't pages but get a reply before falling back to Home.
export const ROUTE_EGGS = {
  holodeck: 'Holodeck 3 is in use. Your program has been queued.',
  'ten-forward': 'Ten Forward is closed for a private function. Guinan sends her regards.',
  'warp-core': 'Main Engineering reports the warp core is stable at 98% efficiency.',
  bridge: 'You have the bridge.',
  'jefferies-tube': 'Crawling through the Jefferies tube… nothing to see here.',
};

export const STARDATE_LOG = 'Captain’s log, stardate %s. VERA continues her mission: '
  + 'to seek out new rooms and new faces, and boldly compute what no assistant has computed before.';
export const DESTRUCT_REPLY = 'Auto-destruct requires command authorization from two senior officers. Request denied.';

export function konamiStep(progress, key) {
  const k = key.length === 1 ? key.toLowerCase() : key;
  if (k === KONAMI[progress]) return progress + 1;
  return k === KONAMI[0] ? 1 : 0;
}

export const KONAMI_LENGTH = KONAMI.length;

// Returns the matched phrase key when the typed buffer ends with one.
export function matchPhrase(buffer) {
  return Object.keys(PHRASES)
    .sort((a, b) => b.length - a.length)
    .find(phrase => buffer.endsWith(phrase)) || null;
}

export function isTypingTarget(target) {
  if (!target) return false;
  const tag = (target.tagName || '').toLowerCase();
  return ['input', 'textarea', 'select'].includes(tag) || !!target.isContentEditable;
}

let messageTimer = null;
let effectTimer = null;

function commBar() {
  let bar = document.getElementById('lcars-comm');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'lcars-comm';
    bar.className = 'lcars-comm';
    bar.setAttribute('role', 'status');
    bar.setAttribute('aria-live', 'polite');
    bar.hidden = true;
    const key = document.createElement('span');
    key.className = 'lcars-comm-key';
    key.textContent = 'Computer';
    const text = document.createElement('span');
    text.className = 'lcars-comm-text';
    bar.append(key, text);
    document.body.append(bar);
  }
  return bar;
}

export function computerSays(text) {
  const bar = commBar();
  bar.querySelector('.lcars-comm-text').textContent = text;
  bar.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => { bar.hidden = true; }, MESSAGE_MS);
}

function bodyEffect(cls, ms) {
  const body = document.body;
  ['is-red-alert', 'is-warp', 'is-beaming'].forEach(c => body.classList.remove(c));
  void body.offsetWidth; // restart the animation if it's already running
  body.classList.add(cls);
  clearTimeout(effectTimer);
  effectTimer = setTimeout(() => body.classList.remove(cls), ms);
}

function redAlert() {
  bodyEffect('is-red-alert', RED_ALERT_MS);
  computerSays('Red alert! All hands to battle stations. (Esc to stand down.)');
}

function standDown() {
  if (!document.body.classList.contains('is-red-alert')) return;
  document.body.classList.remove('is-red-alert');
  computerSays('Condition green. Standing down from red alert.');
}

function runPhrase(phrase) {
  const action = PHRASES[phrase];
  if (action === 'warp') {
    bodyEffect('is-warp', 1600);
    computerSays('Warp factor one. Engaging.');
  } else if (action === 'beam') {
    bodyEffect('is-beaming', 2200);
    computerSays('Energizing… transport complete.');
  } else if (action === 'red-alert') {
    redAlert();
  } else {
    computerSays(action);
  }
}

// Count quick repeated clicks on an element and fire after `needed`.
function onRepeatedClicks(el, needed, fire) {
  if (!el) return;
  let count = 0;
  let timer = null;
  el.addEventListener('click', () => {
    count += 1;
    clearTimeout(timer);
    timer = setTimeout(() => { count = 0; }, 1500);
    if (count >= needed) {
      count = 0;
      fire();
    }
  });
}

function checkRouteEgg() {
  const slug = (location.hash || '').replace(/^#\/?/, '').toLowerCase();
  if (ROUTE_EGGS[slug]) computerSays(ROUTE_EGGS[slug]);
}

export function initEggs() {
  let konami = 0;
  let typed = '';
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') standDown();
    if (isTypingTarget(event.target) || event.ctrlKey || event.metaKey || event.altKey) return;
    konami = konamiStep(konami, event.key);
    if (konami === KONAMI_LENGTH) {
      konami = 0;
      redAlert();
      return;
    }
    if (event.key.length !== 1 || !/[a-z]/i.test(event.key)) return;
    typed = (typed + event.key.toLowerCase()).slice(-BUFFER_MAX);
    const phrase = matchPhrase(typed);
    if (phrase) {
      typed = '';
      runPhrase(phrase);
    }
  });
  onRepeatedClicks(document.getElementById('rail-stardate')?.closest('.rail-stardate'), 5, () => {
    const stardate = document.getElementById('rail-stardate')?.textContent || '—';
    computerSays(STARDATE_LOG.replace('%s', stardate));
  });
  onRepeatedClicks(document.querySelector('.rail-brand .logo'), 7, () => computerSays(DESTRUCT_REPLY));
  // Registered before the router, which rewrites unknown slugs to Home.
  window.addEventListener('hashchange', checkRouteEgg);
  checkRouteEgg();
}
