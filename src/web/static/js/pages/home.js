// Home: at-a-glance status plus the handful of controls used every day.
// Live data arrives via events dispatched by app.js (vera:status/music/podcast/
// update) so Home adds no extra polling beyond a slow settings refresh.

const SETTINGS_REFRESH_MS = 30000;
// Matches ThermalManager's default critical_c and Hailo-8's documented throttle point.
const SOC_HOT_C = 75;
const HAILO_HOT_C = 85;

const $ = id => document.getElementById(id);

const state = {
  status: null,
  music: null,
  podcast: null,
  update: null,
  settings: { privacy: null, quiet: null, muted: null },
  volumeDragging: false,
};

function fmtTemp(celsius) {
  const c = Number(celsius);
  if (celsius == null || !Number.isFinite(c)) return '—';
  return `${c.toFixed(1)} °C`;
}

function fmtNum(value, digits = 0, suffix = '') {
  const n = Number(value);
  return value == null || !Number.isFinite(n) ? '—' : `${n.toFixed(digits)}${suffix}`;
}

function setText(id, text) {
  const node = $(id);
  if (node && node.textContent !== text) node.textContent = text;
}

function note(message, ok = true) {
  const out = $('home-action-status');
  if (!out) return;
  out.textContent = message;
  out.style.color = ok ? 'var(--green)' : 'var(--red)';
}

async function api(path, method = 'GET', body) {
  const options = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function callLegacy(name) {
  if (typeof window[name] === 'function') window[name]();
}

// ── Quiet hours ────────────────────────────────────────────────────────────

function minutesOf(hhmm) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm || ''));
  return match ? Number(match[1]) * 60 + Number(match[2]) : null;
}

export function quietHoursActive(quiet, now = new Date()) {
  if (!quiet || !quiet.enabled) return false;
  const start = minutesOf(quiet.start);
  const end = minutesOf(quiet.end);
  if (start == null || end == null || start === end) return false;
  const minute = now.getHours() * 60 + now.getMinutes();
  return start < end ? minute >= start && minute < end : minute >= start || minute < end;
}

// ── Alerts ─────────────────────────────────────────────────────────────────

export function computeAlerts({ status, update, settings }, now = new Date()) {
  const alerts = [];
  const last = (status && status.last) || {};
  const temp = last['thermal.temp'];
  const hailo = last['thermal.hailo'];
  if (status && status.healthy === false) alerts.push({ level: 'fail', text: 'System degraded' });
  if (temp && temp.ok === false) alerts.push({ level: 'fail', text: 'Temperature sensor fault' });
  const soc = temp && Number(temp.cpu_celsius);
  if (Number.isFinite(soc) && soc >= SOC_HOT_C) alerts.push({ level: 'fail', text: `SoC hot ${soc.toFixed(0)} °C` });
  if (hailo && hailo.ok === false) alerts.push({ level: 'warn', text: 'Hailo unavailable' });
  const hailoC = hailo && Number(hailo.celsius);
  if (hailo && hailo.ok !== false && Number.isFinite(hailoC) && hailoC >= HAILO_HOT_C) {
    alerts.push({ level: 'fail', text: `Hailo hot ${hailoC.toFixed(0)} °C` });
  }
  const stopped = Object.entries((status && status.services) || {})
    .filter(([, svcState]) => svcState !== 'running')
    .map(([name, svcState]) => `${name} ${svcState}`);
  if (stopped.length) {
    alerts.push({ level: 'fail', text: `Service ${stopped.slice(0, 3).join(', ')}${stopped.length > 3 ? ` +${stopped.length - 3}` : ''}` });
  }
  if (update && update.available_version && update.available_version !== update.installed_version) {
    alerts.push({ level: 'warn', text: `Update ${update.available_version} available`, route: 'system' });
  }
  if (settings.privacy) alerts.push({ level: 'unknown', text: 'Privacy on' });
  if (quietHoursActive(settings.quiet, now)) alerts.push({ level: 'unknown', text: 'Quiet hours' });
  if (settings.muted) alerts.push({ level: 'warn', text: 'Muted' });
  return alerts;
}

const ALERT_RANK = { fail: 3, warn: 2, unknown: 1 };

function renderAlerts() {
  const host = $('home-alerts');
  const frame = $('card-home-alerts');
  if (!host) return;
  const alerts = computeAlerts(state);
  const key = JSON.stringify(alerts);
  if (host.dataset.key === key) return;
  host.dataset.key = key;
  if (frame) {
    frame.hidden = alerts.length === 0;
    const top = alerts.reduce((best, a) => (ALERT_RANK[a.level] || 0) > (ALERT_RANK[best] || 0) ? a.level : best, 'unknown');
    frame.dataset.level = top;
    $('tab-home')?.classList?.toggle('has-alerts', alerts.length > 0);
  }
  host.replaceChildren(...alerts.map(alert => {
    const pill = document.createElement(alert.route ? 'button' : 'span');
    pill.className = `pill pill-${alert.level}`;
    pill.textContent = alert.text;
    if (alert.route) {
      pill.type = 'button';
      pill.addEventListener('click', () => window.switchTab(alert.route));
    }
    return pill;
  }));
}

// ── Live status ────────────────────────────────────────────────────────────

function renderIot(iot) {
  const host = $('home-iot');
  if (!host) return;
  const devices = Object.entries(iot || {});
  const rows = devices.map(([id, snap]) => {
    const primary = (snap.display && snap.display.primary) || {};
    const available = snap.available !== false;
    return {
      id,
      label: `${snap.device_icon || '🔌'} ${snap.device_name || id}`,
      value: available ? `${primary.value ?? '—'}${primary.unit ? ' ' + primary.unit : ''}` : 'unavailable',
      color: available ? (primary.color || '') : 'var(--red)',
      title: available ? '' : (snap.error || ''),
    };
  });
  const key = JSON.stringify(rows);
  if (host.dataset.key === key) return;
  host.dataset.key = key;
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'home-muted';
    empty.textContent = 'No devices configured.';
    host.replaceChildren(empty);
    return;
  }
  host.replaceChildren(...rows.map(row => {
    const item = document.createElement('div');
    item.className = 'home-iot-row';
    item.title = row.title;
    const label = document.createElement('span');
    label.textContent = row.label;
    const value = document.createElement('strong');
    value.textContent = row.value;
    if (row.color) value.style.color = row.color;
    item.append(label, value);
    return item;
  }));
}

// Sweep-bar readouts truncate, so the full text rides along as a tooltip.
function setReadout(id, text) {
  setText(id, text);
  const node = $(id);
  if (node) node.title = text;
}

function onStatus(data) {
  state.status = data;
  const last = data.last || {};
  setReadout('home-room', data.room || (data.room_detail && data.room_detail.name) || '—');
  const faces = last['perception.faces'] || {};
  const named = (faces.faces || []).map(f => f.name).filter(n => n && n !== 'unknown');
  const count = Number(faces.count || 0);
  setReadout('home-faces', count ? `${count}${named.length ? ' · ' + named.join(', ') : ''}` : 'None');
  state.faceCount = count;
  renderCameraCaption();
  const spoke = last['av.spoke'];
  setText('home-last-spoken', spoke && spoke.text ? `“${spoke.text}”` : '—');

  const temp = last['thermal.temp'] || {};
  const hailo = last['thermal.hailo'] || {};
  const fan = last['thermal.fan'] || {};
  setText('home-temp-case', fmtTemp(temp.case_celsius));
  setText('home-temp-soc', fmtTemp(temp.cpu_celsius));
  setText('home-temp-blended', fmtTemp(temp.blended_celsius));
  setText('home-temp-hailo', hailo.ok === false ? 'unavailable' : fmtTemp(hailo.celsius));
  setText('home-fan', fan.duty == null ? '—' : `${fmtNum(fan.duty, 0, '%')}${fan.override ? ' (override)' : ''}`);
  const room = data.room_detail || {};
  setText('home-scene-confidence', room.last_similarity == null ? '—'
    : `${fmtNum(room.last_similarity * 100, 0, '%')} / ${fmtNum(room.similarity_thresh * 100, 0, '%')}`);
  setText('home-scene-check', room.checking_enabled === false ? 'checking off'
    : room.last_check_age_s == null ? '—' : `${fmtNum(room.last_check_age_s / 60, 0)} min ago`);

  const pos = last['motion.position'] || {};
  const level = last['audio.level'] || {};
  setText('home-eng-line', [
    `pan ${fmtNum(pos.angle, 1, '°')}`, `tilt ${fmtNum(pos.tilt_angle, 1, '°')}`,
    `mic ${fmtNum(level.dbfs, 1, ' dBFS')}`, `cpu ${fmtNum(data.cpu_percent, 0, '%')}`,
    `mem ${fmtNum(data.mem_percent, 0, '%')}`,
  ].join(' · '));

  renderIot(data.iot);
  renderAlerts();
}

// ── Now playing ────────────────────────────────────────────────────────────

function podcastActive() {
  return state.podcast && ['playing', 'paused'].includes(state.podcast.state);
}

function renderMedia() {
  const music = state.music || {};
  const muted = music.muted ?? state.settings.muted;
  if (music.muted !== undefined) state.settings.muted = !!music.muted;
  let source, title, subtitle, playing, art = '';
  if (podcastActive()) {
    const p = state.podcast;
    source = 'Podcast';
    title = p.episode_title || 'Episode';
    subtitle = p.podcast_title || '';
    playing = p.state === 'playing';
  } else {
    const song = music.song || {};
    source = music.configured === false ? 'Pandora (not configured)' : 'Pandora';
    playing = music.state === 'playing';
    title = music.state && music.state !== 'stopped' ? (song.title || '—') : 'Nothing playing';
    subtitle = song.artist && music.state !== 'stopped' ? song.artist : '';
    art = music.state !== 'stopped' ? (song.cover_art_url || '') : '';
  }
  setText('home-media-source', source);
  setText('home-media-title', title);
  setText('home-media-subtitle', subtitle);
  const img = $('home-media-art');
  if (img) {
    if (art && img.getAttribute('src') !== art) img.src = art;
    if (!art) img.removeAttribute('src');
    img.hidden = !art;
  }
  setText('home-media-play', playing ? '⏸ Pause' : '▶ Play');
  const next = $('home-media-next');
  if (next) next.disabled = podcastActive();
  const slider = $('home-volume');
  if (slider && !state.volumeDragging && Number(music.volume) >= 0) {
    slider.value = music.volume;
    setText('home-volume-label', `${music.volume}%`);
  }
  setText('home-mute-btn', muted ? '🔊 Unmute' : '🔇 Mute');
  const toggle = $('home-toggle-mute');
  if (toggle && document.activeElement !== toggle) toggle.checked = !!muted;
  renderAlerts();
}

async function mediaPlayPause() {
  try {
    if (podcastActive()) {
      await api(`/api/podcasts/${state.podcast.state === 'playing' ? 'pause' : 'resume'}`, 'POST');
      callLegacy('loadPodcastStatus');
    } else {
      const musicState = (state.music && state.music.state) || 'stopped';
      if (musicState === 'stopped') await window.musicPlay();
      else await api('/api/music/pause', 'POST');
      callLegacy('loadMusicStatus');
    }
  } catch (err) {
    note(`Playback: ${err.message}`, false);
  }
}

async function mediaNext() {
  try {
    await api('/api/music/next', 'POST');
    callLegacy('loadMusicStatus');
  } catch (err) {
    note(`Skip: ${err.message}`, false);
  }
}

async function setVolume(level) {
  try {
    await api('/api/music/volume', 'PUT', { level: Number(level) });
    callLegacy('loadMusicStatus');
  } catch (err) {
    note(`Volume: ${err.message}`, false);
  }
}

async function setMuted(muted) {
  await api('/api/audio/mute', 'PUT', { muted });
  state.settings.muted = muted;
  if (state.music) state.music.muted = muted;
  callLegacy('loadMusicStatus');
  renderMedia();
}

// ── Quick toggles ──────────────────────────────────────────────────────────

const TOGGLES = {
  'home-toggle-privacy': {
    label: 'Privacy',
    async save(enabled) {
      await api('/api/settings/privacy', 'PUT', { enabled });
      state.settings.privacy = enabled;
      callLegacy('loadPrivacySettings');
    },
  },
  'home-toggle-quiet': {
    label: 'Quiet hours',
    async save(enabled) {
      const current = await api('/api/settings/quiet-hours');
      const saved = await api('/api/settings/quiet-hours', 'PUT', { ...current, enabled });
      state.settings.quiet = { ...current, ...saved, enabled };
      callLegacy('loadQuietHours');
    },
  },
  'home-toggle-mute': { label: 'Mute', save: setMuted },
  'home-toggle-face': {
    label: 'Face tracking',
    async save(enabled) {
      await api('/api/settings/face-tracking', 'PUT', { enabled });
      callLegacy('loadFaceTrackingEnabled');
    },
  },
  'home-toggle-idle': {
    label: 'Idle motion',
    async save(enabled) {
      await api('/api/settings/random-motion', 'PUT', { enabled });
      callLegacy('loadRandomMotionEnabled');
    },
  },
};

async function onToggle(event) {
  const input = event.target;
  const toggle = TOGGLES[input.id];
  input.disabled = true;
  try {
    await toggle.save(input.checked);
    note(`${toggle.label} ${input.checked ? 'on' : 'off'}`);
  } catch (err) {
    input.checked = !input.checked;
    note(`${toggle.label}: ${err.message}`, false);
  } finally {
    input.disabled = false;
    renderAlerts();
  }
}

async function refreshSettings() {
  const read = async (path, apply) => {
    try { apply(await api(path)); } catch (_) { /* leave the previous value */ }
  };
  const check = (id, value) => {
    const input = $(id);
    if (input && !input.disabled) input.checked = !!value;
  };
  await Promise.all([
    read('/api/settings/privacy', d => { state.settings.privacy = !!d.enabled; check('home-toggle-privacy', d.enabled); }),
    read('/api/settings/quiet-hours', d => {
      state.settings.quiet = d;
      check('home-toggle-quiet', d.enabled);
      setText('home-quiet-window', d.start && d.end ? `${d.start}–${d.end}` : '');
    }),
    read('/api/audio/mute', d => { state.settings.muted = !!d.muted; check('home-toggle-mute', d.muted); }),
    read('/api/settings/face-tracking', d => check('home-toggle-face', d.enabled)),
    read('/api/settings/random-motion', d => check('home-toggle-idle', d.enabled)),
  ]);
  renderAlerts();
  renderMedia();
}

// ── Say ────────────────────────────────────────────────────────────────────

async function say() {
  const input = $('home-say-input');
  const text = input.value.trim();
  if (!text) return;
  try {
    await api('/api/say', 'POST', { text });
    input.value = '';
    note('Speaking…');
  } catch (err) {
    note(`Say: ${err.message}`, false);
  }
}

async function repeat() {
  try {
    await api('/api/audio/repeat', 'POST');
    note('Repeating last phrase');
  } catch (err) {
    note(`Repeat: ${err.message}`, false);
  }
}

// ── Lifecycle ──────────────────────────────────────────────────────────────

let settingsTimer = null;
let homeActive = false;

// ── Camera source switch (bottom sweep) ────────────────────────────────────

const CAMERA_STREAMS = { 1: '/stream', 2: '/stream2' };
const CAMERA_KEY = 'vera-home-cam';
let homeCam = 1;

function renderCameraCaption() {
  if (homeCam === 2) {
    setText('home-camera-caption', 'Cam 2 · raw view');
    return;
  }
  const count = state.faceCount || 0;
  setText('home-camera-caption', count ? `${count} face${count === 1 ? '' : 's'} in view` : 'No faces in view');
}

function renderCameraSwitch() {
  document.querySelectorAll('[data-home-cam]').forEach(btn => {
    btn.setAttribute('aria-pressed', String(Number(btn.dataset.homeCam) === homeCam));
  });
  const img = $('home-camera');
  if (img) img.alt = `Live view from camera ${homeCam}`;
}

function startCamera() {
  if (typeof window._registerStream === 'function') window._registerStream('home-camera', CAMERA_STREAMS[homeCam]);
}

export function selectCamera(cam) {
  homeCam = Number(cam) === 2 ? 2 : 1;
  try { localStorage.setItem(CAMERA_KEY, String(homeCam)); } catch { /* storage unavailable */ }
  renderCameraSwitch();
  renderCameraCaption();
  if (homeActive) startCamera();
}

function enter() {
  homeActive = true;
  startCamera();
  refreshSettings();
  clearInterval(settingsTimer);
  settingsTimer = setInterval(() => { if (!document.hidden) refreshSettings(); }, SETTINGS_REFRESH_MS);
}

function leave() {
  homeActive = false;
  if (typeof window._unregisterStream === 'function') window._unregisterStream('home-camera');
  clearInterval(settingsTimer);
  settingsTimer = null;
}

export function initHome() {
  if (!$('tab-home')) return;
  window.addEventListener('vera:status', e => onStatus(e.detail || {}));
  window.addEventListener('vera:music', e => { state.music = e.detail || {}; renderMedia(); });
  window.addEventListener('vera:podcast', e => { state.podcast = e.detail || {}; renderMedia(); });
  window.addEventListener('vera:update', e => { state.update = e.detail || {}; renderAlerts(); });
  window.addEventListener('vera:route', e => {
    const { tab, previous } = e.detail || {};
    if (tab === 'home') enter();
    else if (previous === 'home') leave();
  });

  Object.keys(TOGGLES).forEach(id => $(id)?.addEventListener('change', onToggle));
  $('home-media-play')?.addEventListener('click', mediaPlayPause);
  $('home-media-next')?.addEventListener('click', mediaNext);
  $('home-mute-btn')?.addEventListener('click', async () => {
    try { await setMuted(!state.settings.muted); } catch (err) { note(`Mute: ${err.message}`, false); }
  });
  const slider = $('home-volume');
  slider?.addEventListener('input', () => {
    state.volumeDragging = true;
    setText('home-volume-label', `${slider.value}%`);
  });
  slider?.addEventListener('change', async () => {
    await setVolume(slider.value);
    state.volumeDragging = false;
  });
  $('home-say-btn')?.addEventListener('click', say);
  $('home-say-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') say(); });
  $('home-repeat-btn')?.addEventListener('click', repeat);
  try { homeCam = localStorage.getItem(CAMERA_KEY) === '2' ? 2 : 1; } catch { homeCam = 1; }
  document.querySelectorAll('[data-home-cam]').forEach(btn => {
    btn.addEventListener('click', () => selectCamera(btn.dataset.homeCam));
  });
  $('home-camera')?.addEventListener('error', () => {
    if (homeActive && $('home-camera').getAttribute('src')) setText('home-camera-caption', `Cam ${homeCam} unavailable`);
  });
  renderCameraSwitch();
  renderAlerts();
}
