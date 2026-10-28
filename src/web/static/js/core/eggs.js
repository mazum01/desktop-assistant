// Star Trek easter eggs. Front-end only: nothing here speaks, moves the head or
// changes a setting. Typed phrases are only heard outside form fields.

const MESSAGE_MS = 6000;
const RED_ALERT_MS = 10000;
const KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'ArrowLeft', 'ArrowRight', 'b', 'a'];
const BUFFER_MAX = 24;
const TRIBBLE_MAX = 48;
const EFFECTS = ['is-red-alert', 'is-warp', 'is-beaming', 'is-shields', 'is-cloaked', 'is-torpedo'];

// Typed outside any input, spaces ignored. No phrase may contain another one,
// or the shorter one would fire first.
export const PHRASES = {
  engage: 'warp',
  maximumwarp: 'warp',
  makeitso: 'Acknowledged. Making it so.',
  earlgrey: 'Tea, Earl Grey, hot… replicator offline. Please visit the galley.',
  shieldsup: 'shields',
  cloak: 'cloak',
  tribble: 'tribbles',
  torpedo: 'torpedo',
  selfdestruct: 'destruct',
  beammeup: 'beam',
  energize: 'beam',
  redalert: 'red-alert',
  computer: 'Working.',
  resistanceisfutile: 'Borg signature detected. Shields adapting… adaptation complete.',
  fascinating: 'Indeed.',
  khan: 'Ceti alpha five is not on our flight plan.',
  livelong: 'And prosper. 🖖',
  qapla: 'Qapla’! Today is a good day to compute.',
  hail: 'Hailing frequencies open. No response.',
  dammitjim: 'I’m a doctor, not a desktop assistant.',
  fourlights: 'There are four lights!',
  darmok: 'Darmok and Jalad at Tanagra.',
  scotty: 'I’m givin’ her all she’s got, Captain!',
  lcars: 'Library Computer Access and Retrieval System. Online.',
  redshirt: 'Ensign Ricky has been assigned to the away team. Good luck, Ensign.',
  numberone: 'Number One, you have the conn.',
  wesley: 'Shut up, Wesley.',
};

// Hash routes that aren't pages but get a reply before falling back to Home.
export const ROUTE_EGGS = {
  holodeck: 'Holodeck 3 is in use. Your program has been queued.',
  'ten-forward': 'Ten Forward is closed for a private function. Guinan sends her regards.',
  'warp-core': 'Main Engineering reports the warp core is stable at 98% efficiency.',
  bridge: 'You have the bridge.',
  'jefferies-tube': 'Crawling through the Jefferies tube… nothing to see here.',
  sickbay: 'Please state the nature of the medical emergency.',
  'transporter-room': 'Transporter room standing by. Pattern buffers nominal.',
  shuttlebay: 'Shuttlebay 2 doors are closed. Request clearance from the bridge.',
  brig: 'The brig is empty. Keep it that way.',
  astrometrics: 'Astrometrics: long-range sensors show nothing but the living room.',
};

// Once-a-day greetings, keyed by MM-DD.
export const HOLIDAYS = {
  '04-05': 'Happy First Contact Day. The Vulcans landed in Bozeman on this date in 2063.',
  '09-08': 'Happy Star Trek Day. The original series first aired on this date in 1966.',
};

export function holidayGreeting(date) {
  const key = `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  return HOLIDAYS[key] || null;
}

export const FRAME_CODE_REPLY = 'NCC-1701. No bloody A, B, C or D.';

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
let destructTimer = null;

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
  EFFECTS.forEach(c => body.classList.remove(c));
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
  if (destructTimer) {
    clearTimeout(destructTimer);
    destructTimer = null;
    document.body.classList.remove('is-red-alert');
    computerSays('Auto-destruct aborted.');
    return;
  }
  if (!document.body.classList.contains('is-red-alert')) return;
  document.body.classList.remove('is-red-alert');
  computerSays('Condition green. Standing down from red alert.');
}

function reducedMotion() {
  return !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

function selfDestruct() {
  clearTimeout(destructTimer);
  bodyEffect('is-red-alert', 8000);
  let n = 5;
  const tick = () => {
    if (n > 0) {
      computerSays(`Auto-destruct in ${n}… (Esc to abort)`);
      n -= 1;
      destructTimer = setTimeout(tick, 1000);
    } else {
      destructTimer = null;
      document.body.classList.remove('is-red-alert');
      computerSays('Auto-destruct aborted. Authorization Picard four-seven-alpha-tango. It was only a drill.');
    }
  };
  tick();
}

// Tribbles: they multiply, then get beamed to a Klingon engine room.
function tribbles() {
  if (reducedMotion()) {
    computerSays('There’s a tribble on the console. Just one. For now.');
    return;
  }
  if (document.querySelector('.lcars-tribble')) return;
  computerSays('There’s a tribble on the console… and it’s multiplying.');
  const spawned = [];
  const spawn = () => {
    const t = document.createElement('span');
    t.className = 'lcars-tribble';
    t.setAttribute('aria-hidden', 'true');
    t.style.left = `${Math.random() * 94}vw`;
    t.style.top = `${Math.random() * 90}vh`;
    t.style.setProperty('--tribble-size', `${18 + Math.random() * 18}px`);
    t.dataset.tone = String(Math.floor(Math.random() * 3));
    document.body.append(t);
    spawned.push(t);
  };
  spawn();
  const grow = setInterval(() => {
    const n = Math.min(spawned.length, TRIBBLE_MAX - spawned.length);
    for (let i = 0; i < n; i += 1) spawn();
    if (spawned.length >= TRIBBLE_MAX) clearInterval(grow);
  }, 500);
  setTimeout(() => {
    clearInterval(grow);
    computerSays('Tribbles transported to the Klingons’ engine room. Where they’ll be no tribble at all.');
    spawned.forEach(t => t.classList.add('is-leaving'));
    setTimeout(() => spawned.forEach(t => t.remove()), 900);
  }, 4500);
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
  } else if (action === 'shields') {
    bodyEffect('is-shields', 3000);
    computerSays('Shields up. Holding at 100%.');
  } else if (action === 'cloak') {
    bodyEffect('is-cloaked', 3200);
    computerSays('Cloaking device engaged. Romulan technology, on loan.');
  } else if (action === 'torpedo') {
    bodyEffect('is-torpedo', 1300);
    computerSays('Photon torpedo away.');
  } else if (action === 'tribbles') {
    tribbles();
  } else if (action === 'destruct') {
    selfDestruct();
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

function greetHoliday() {
  const now = new Date();
  const greeting = holidayGreeting(now);
  if (!greeting) return;
  const today = now.toDateString();
  try {
    if (localStorage.getItem('vera-egg-holiday') === today) return;
    localStorage.setItem('vera-egg-holiday', today);
  } catch { /* private mode: greet anyway */ }
  setTimeout(() => computerSays(greeting), 2500);
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
  onRepeatedClicks(document.querySelector('.frame-code'), 3, () => computerSays(FRAME_CODE_REPLY));
  onRepeatedClicks(document.getElementById('health-badge'), 3, () => {
    const status = document.getElementById('health-badge-value')?.textContent?.trim() || 'unknown';
    computerSays(`Level-1 diagnostic complete. Ship’s status: ${status}.`);
  });
  greetHoliday();
  console.info('%c LCARS %c VERA online. Live long and prosper. 🖖',
    'background:#f9a66c;color:#000;font-weight:bold;border-radius:8px 0 0 8px;padding:2px 6px',
    'background:#000;color:#a9d3ff;padding:2px 6px');
  // Registered before the router, which rewrites unknown slugs to Home.
  window.addEventListener('hashchange', checkRouteEgg);
  checkRouteEgg();
}
