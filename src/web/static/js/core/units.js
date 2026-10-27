// Display unit systems. The choice is device-wide (GET/PUT /api/settings/units,
// see src/core/display_units.py) and cached in localStorage so the first paint
// already uses it. Readings arrive in native units; pages convert at render
// time with the helpers below and re-render on the `vera:units` event.

export const SYSTEMS = {
  us: { temperature: '°F', flow: 'gal/min', volume: 'gal', pressure: 'psi', radon: 'pCi/L', length: 'ft' },
  si: { temperature: '°C', flow: 'L/min', volume: 'L', pressure: 'kPa', radon: 'Bq/m³', length: 'm' },
  imperial: { temperature: '°C', flow: 'imp gal/min', volume: 'imp gal', pressure: 'psi', radon: 'Bq/m³', length: 'ft' },
  scientific: { temperature: 'K', flow: 'L/min', volume: 'L', pressure: 'kPa', radon: 'Bq/m³', length: 'm' },
};
export const DEFAULT_SYSTEM = 'us';
export const UNITS_KEY = 'vera-units';

// Linear factors to each quantity's base unit (L/min, L, kPa, Bq/m³, m).
// Native spellings from device plugins (gpm, PSI) are aliases.
const FACTORS = {
  flow: { 'L/min': 1, 'gal/min': 3.785411784, gpm: 3.785411784, 'imp gal/min': 4.54609 },
  volume: { L: 1, gal: 3.785411784, 'imp gal': 4.54609 },
  pressure: { kPa: 1, psi: 6.894757293, PSI: 6.894757293 },
  radon: { 'Bq/m³': 1, 'pCi/L': 37 },
  length: { m: 1, ft: 0.3048 },
};
const TEMP_FROM_C = { '°C': c => c, '°F': c => c * 9 / 5 + 32, K: c => c + 273.15 };
const TEMP_TO_C = { '°C': v => v, '°F': v => (v - 32) * 5 / 9, K: v => v - 273.15 };
const DIGITS = { 'L/min': 1, 'gal/min': 2, 'imp gal/min': 2, 'Bq/m³': 0, 'pCi/L': 2, kPa: 0, psi: 0 };

let current = DEFAULT_SYSTEM;

export function currentSystem() { return current; }
export function unitFor(quantity, system = current) { return (SYSTEMS[system] || SYSTEMS[DEFAULT_SYSTEM])[quantity]; }

function quantityOf(unit) {
  if (unit in TEMP_TO_C) return 'temperature';
  return Object.keys(FACTORS).find(q => unit in FACTORS[q]) || null;
}

// Convert `value` in `fromUnit` to the active system. Unknown units pass through.
export function convert(value, fromUnit, system = current) {
  const quantity = quantityOf(fromUnit);
  const n = Number(value);
  if (!quantity || value == null || value === '' || !Number.isFinite(n)) return { value, unit: fromUnit, quantity };
  const unit = unitFor(quantity, system);
  if (quantity === 'temperature') return { value: TEMP_FROM_C[unit](TEMP_TO_C[fromUnit](n)), unit, quantity };
  return { value: n * FACTORS[quantity][fromUnit] / FACTORS[quantity][unit], unit, quantity };
}

export function formatValue(value, fromUnit, digits, system = current) {
  const out = convert(value, fromUnit, system);
  const n = Number(out.value);
  if (out.value == null || out.value === '' || !Number.isFinite(n)) return '—';
  const places = digits ?? DIGITS[out.unit] ?? 1;
  return `${n.toFixed(places)} ${out.unit}`;
}

export function formatTemp(celsius, digits = 1, system = current) {
  return formatValue(celsius, '°C', digits, system);
}

// Device plugins send display strings: either a bare number plus a unit
// ("1.25" + "gpm") or a value with an embedded temperature ("72.0°F").
// Returns the converted text, or null when the reading isn't convertible.
export function convertDisplay(value, unit, system = current) {
  const text = String(value ?? '').trim();
  const embedded = /^(-?\d+(?:\.\d+)?)\s*(°F|°C|K)$/.exec(text);
  if (embedded) {
    return formatValue(embedded[1], embedded[2], 1, system) + (unit ? ` ${unit}` : '');
  }
  if (/^-?\d+(?:\.\d+)?$/.test(text) && unit && quantityOf(unit)) return formatValue(text, unit, undefined, system);
  return null;
}

export function describeSystem(system = current) {
  const u = SYSTEMS[system] || SYSTEMS[DEFAULT_SYSTEM];
  return `Temperature ${u.temperature} · Flow ${u.flow} · Pressure ${u.pressure} · Radon ${u.radon} · Length ${u.length}`;
}

function apply(system, { persist = true } = {}) {
  if (!SYSTEMS[system]) return false;
  const changed = system !== current;
  current = system;
  if (persist) { try { localStorage.setItem(UNITS_KEY, system); } catch { /* storage unavailable */ } }
  document.querySelectorAll('[data-units-option]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.unitsOption === system));
  });
  const detail = document.getElementById('units-detail');
  if (detail) detail.textContent = describeSystem(system);
  if (changed) window.dispatchEvent(new CustomEvent('vera:units', { detail: { system } }));
  return true;
}

async function request(method, body) {
  const options = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch('/api/settings/units', options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

export async function setSystem(system) {
  const status = document.getElementById('units-status');
  const buttons = document.querySelectorAll('[data-units-option]');
  const previous = current;
  apply(system);
  buttons.forEach(b => { b.disabled = true; });
  try {
    const saved = await request('PUT', { system });
    apply(saved.system);
    if (status) status.textContent = 'Saved';
  } catch (err) {
    apply(previous);
    if (status) status.textContent = `Could not save: ${err.message}`;
  } finally {
    buttons.forEach(b => { b.disabled = false; });
  }
}

export async function refreshUnits() {
  try {
    const data = await request('GET');
    apply(data.system);
  } catch { /* keep the cached choice */ }
}

export function initUnits() {
  let cached = DEFAULT_SYSTEM;
  try { cached = localStorage.getItem(UNITS_KEY) || DEFAULT_SYSTEM; } catch { /* storage unavailable */ }
  current = SYSTEMS[cached] ? cached : DEFAULT_SYSTEM;
  apply(current, { persist: false });
  document.querySelectorAll('[data-units-option]').forEach(button => {
    button.addEventListener('click', () => setSystem(button.dataset.unitsOption));
  });
  // Another browser may change the setting; pick it up when this tab regains focus.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshUnits(); });
  refreshUnits();
}
