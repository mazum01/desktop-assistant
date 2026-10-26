// Normal / Advanced / Engineering interface modes.
// Elements tagged data-min-mode="advanced|engineering" are hidden by CSS until
// the body's data-mode reaches that level. This is presentation only — every
// API remains reachable regardless of mode.

export const MODES = ['normal', 'advanced', 'engineering'];
export const MODE_KEY = 'vera-ui-mode';
const ENGINEERING_PROMPT =
  'Engineering mode shows raw diagnostics and low-level controls that can ' +
  'disrupt VERA if misused. Continue?';

export function storedMode() {
  const saved = localStorage.getItem(MODE_KEY);
  return MODES.includes(saved) ? saved : 'normal';
}

export function currentMode() {
  return MODES.includes(document.body.dataset.mode) ? document.body.dataset.mode : 'normal';
}

export function modeAtLeast(level) {
  return MODES.indexOf(currentMode()) >= MODES.indexOf(level);
}

function applyMode(mode) {
  document.body.dataset.mode = mode;
  document.querySelectorAll('[data-mode-option]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.modeOption === mode));
  });
  window.dispatchEvent(new CustomEvent('vera:mode', { detail: { mode } }));
}

export function setMode(mode, confirmFn = window.confirm.bind(window)) {
  if (!MODES.includes(mode)) return false;
  if (mode === 'engineering' && currentMode() !== 'engineering' && !confirmFn(ENGINEERING_PROMPT)) {
    return false;
  }
  localStorage.setItem(MODE_KEY, mode);
  applyMode(mode);
  return true;
}

export function initMode() {
  applyMode(storedMode());
  document.querySelectorAll('[data-mode-option]').forEach(button => {
    button.addEventListener('click', () => setMode(button.dataset.modeOption));
  });
}
