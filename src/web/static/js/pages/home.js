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
  settings: { privacy: null, quiet: null, muted: null, motion: null },
  volumeDragging: false,
};

// Unit formatting comes from js/core/units.js via initHome(); until then (and in
// standalone tests) readings render in their native units.
let units = {
  formatTemp: (celsius, digits = 1) => `${Number(celsius).toFixed(digits)} °C`,
  convertDisplay: () => null,
};

function fmtTemp(celsius, digits = 1) {
  const c = Number(celsius);
  if (celsius == null || !Number.isFinite(c)) return '—';
  return units.formatTemp(c, digits);
}

function fmtNum(value, digits = 0, suffix = '') {
  const n = Number(value);
  return value == null || !Number.isFinite(n) ? '—' : `${n.toFixed(digits)}${suffix}`;
}

function setText(id, text) {
  const node = $(id);
  if (node && node.textContent !== text) node.textContent = text;
}

function note(message, ok = true, id = 'home-action-status') {
  const out = $(id);
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
  if (status && status.healthy === false) alerts.push({ id: 'degraded', level: 'fail', text: 'System degraded' });
  if (temp && temp.ok === false) alerts.push({ id: 'temp-fault', level: 'fail', text: 'Temperature sensor fault' });
  const soc = temp && Number(temp.cpu_celsius);
  if (Number.isFinite(soc) && soc >= SOC_HOT_C) alerts.push({ id: 'soc-hot', level: 'fail', text: `SoC hot ${fmtTemp(soc, 0)}` });
  if (hailo && hailo.ok === false) alerts.push({ id: 'hailo-unavailable', level: 'warn', text: 'Hailo unavailable' });
  const hailoC = hailo && Number(hailo.celsius);
  if (hailo && hailo.ok !== false && Number.isFinite(hailoC) && hailoC >= HAILO_HOT_C) {
    alerts.push({ id: 'hailo-hot', level: 'fail', text: `Hailo hot ${fmtTemp(hailoC, 0)}` });
  }
  const stopped = Object.entries((status && status.services) || {})
    .filter(([, svcState]) => svcState !== 'running')
    .map(([name, svcState]) => `${name} ${svcState}`);
  if (stopped.length) {
    alerts.push({ id: 'services', level: 'fail', text: `Service ${stopped.slice(0, 3).join(', ')}${stopped.length > 3 ? ` +${stopped.length - 3}` : ''}` });
  }
  if (update && update.available_version && update.available_version !== update.installed_version) {
    alerts.push({ id: 'update', level: 'warn', text: `Update ${update.available_version} available`, route: 'system' });
  }
  if (settings.privacy) alerts.push({ id: 'privacy', level: 'unknown', text: 'Privacy on' });
  if (quietHoursActive(settings.quiet, now)) alerts.push({ id: 'quiet', level: 'unknown', text: 'Quiet hours' });
  if (settings.muted) alerts.push({ id: 'muted', level: 'warn', text: 'Muted' });
  return alerts;
}

const ALERT_RANK = { fail: 3, warn: 2, unknown: 1 };
const ACK_KEY = 'vera-alert-acks';

// An acknowledgement is keyed by alert id and level, so an alert that clears and
// comes back, or escalates from warn to fail, shows the bar again.
export function alertAckKey(alert) {
  return `${alert.id || alert.text}|${alert.level}`;
}

export function visibleAlerts(alerts, acks) {
  const acked = new Set(acks || []);
  return alerts.filter(alert => !acked.has(alertAckKey(alert)));
}

export function pruneAcks(alerts, acks) {
  const active = new Set(alerts.map(alertAckKey));
  return (acks || []).filter(key => active.has(key));
}

function readAcks() {
  try {
    const parsed = JSON.parse(localStorage.getItem(ACK_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}

function writeAcks(acks) {
  try { localStorage.setItem(ACK_KEY, JSON.stringify(acks)); } catch { /* storage unavailable */ }
}

function acknowledgeAlerts() {
  const all = computeAlerts(state);
  writeAcks([...new Set([...readAcks(), ...all.map(alertAckKey)])]);
  renderAlerts();
}

function renderAlerts() {
  const host = $('home-alerts');
  const frame = $('card-home-alerts');
  if (!host) return;
  const all = computeAlerts(state);
  const stored = readAcks();
  const acks = pruneAcks(all, stored);
  if (acks.length !== stored.length) writeAcks(acks);
  const alerts = visibleAlerts(all, acks);
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
    const converted = units.convertDisplay(primary.value, primary.unit);
    return {
      id,
      label: `${snap.device_icon || '🔌'} ${snap.device_name || id}`,
      value: !available ? 'unavailable'
        : converted ?? `${primary.value ?? '—'}${primary.unit ? ' ' + primary.unit : ''}`,
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

// Objects in view, most confident first, duplicates counted: "keyboard, cup ×2".
// Detections older than OBJECTS_STALE_S (detector off or stalled) read as "—".
const OBJECTS_STALE_S = 10;

export function describeObjects(payload, nowS = Date.now() / 1000) {
  if (!payload || !Array.isArray(payload.objects)) return '—';
  if (payload.ts && nowS - payload.ts > OBJECTS_STALE_S) return '—';
  const counts = new Map();
  const sorted = [...payload.objects].sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
  for (const obj of sorted) {
    const label = obj.label || '?';
    counts.set(label, (counts.get(label) || 0) + 1);
  }
  if (!counts.size) return 'None';
  return [...counts].map(([label, n]) => (n > 1 ? `${label} ×${n}` : label)).join(', ');
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
  const roomName = data.room || (data.room_detail && data.room_detail.name) || '—';
  setReadout('home-room', roomName);
  setReadout('home-control-room', roomName);
  setReadout('home-objects', describeObjects(last['perception.objects']));
  const faces = last['perception.faces'] || {};
  const named = (faces.faces || []).map(f => f.name).filter(n => n && n !== 'unknown');
  const count = Number(faces.count || 0);
  const people = count ? `${count}${named.length ? ' · ' + named.join(', ') : ''}` : 'None';
  setReadout('home-faces', people);
  setReadout('home-control-people', people);
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
  renderHead(pos);
  const level = last['audio.level'] || {};
  setText('home-eng-line', [
    `pan ${fmtNum(pos.angle, 1, '°')}`, `tilt ${fmtNum(pos.tilt_angle, 1, '°')}`,
    `mic ${fmtNum(level.dbfs, 1, ' dBFS')}`, `cpu ${fmtNum(data.cpu_percent, 0, '%')}`,
    `mem ${fmtNum(data.mem_percent, 0, '%')}`,
  ].join(' · '));

  renderIot(data.iot);
  renderAlerts();
}

// ── Media player ───────────────────────────────────────────────────────────
// One player for Pandora and podcasts. The view follows whichever source starts
// playing; otherwise it stays on the source the user picked.

const SOURCE_KEY = 'vera-home-media-source';
let mediaSource = 'pandora';
let lastActiveSource = null;
let podcastsLoaded = false;

function podcastActive() {
  return state.podcast && ['playing', 'paused'].includes(state.podcast.state);
}

function musicActive() {
  return state.music && ['playing', 'paused'].includes(state.music.state);
}

export function fmtClock(sec) {
  const total = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

function fmtProgress(position, duration) {
  if (!(Number(position) >= 0) || !(Number(duration) > 0)) return '';
  return `${fmtClock(position)} / ${fmtClock(duration)}`;
}

function selectSource(source, persist = true) {
  mediaSource = source === 'podcast' ? 'podcast' : 'pandora';
  if (persist) { try { localStorage.setItem(SOURCE_KEY, mediaSource); } catch { /* ignore */ } }
  if (mediaSource === 'podcast' && !podcastsLoaded) loadPodcastList();
  renderMedia();
}

function fillSelect(select, options, selected) {
  if (!select) return;
  const key = JSON.stringify(options);
  if (select.dataset.key !== key) {
    select.dataset.key = key;
    const keep = select.value;
    select.replaceChildren(...options.map(({ value, label }) => {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = label;
      return opt;
    }));
    if (options.some(o => o.value === keep)) select.value = keep;
  }
  if (selected != null && document.activeElement !== select && options.some(o => o.value === String(selected))) {
    select.value = String(selected);
  }
}

async function loadPodcastList() {
  try {
    const data = await api('/api/podcasts');
    const subs = data.subscriptions || [];
    podcastsLoaded = true;
    const opts = subs.length
      ? subs.map(p => ({ value: String(p.id), label: p.title || `Podcast ${p.id}` }))
      : [{ value: '', label: '— no subscriptions —' }];
    const select = $('home-podcast-select');
    fillSelect(select, opts, state.podcast && state.podcast.podcast_id);
    if (select && select.value) loadEpisodes(select.value);
  } catch (err) {
    note(`Podcasts: ${err.message}`, false);
  }
}

async function loadEpisodes(podcastId) {
  const select = $('home-episode-select');
  if (!select || !podcastId) return;
  try {
    const data = await api(`/api/podcasts/${encodeURIComponent(podcastId)}/episodes?limit=25`);
    const episodes = data.episodes || [];
    const opts = episodes.length
      ? episodes.map((ep, idx) => ({ value: String(idx), label: ep.title || `Episode ${idx + 1}` }))
      : [{ value: '0', label: '— latest episode —' }];
    const p = state.podcast || {};
    fillSelect(select, opts, String(p.podcast_id) === String(podcastId) ? p.episode_index : null);
  } catch (err) {
    note(`Episodes: ${err.message}`, false);
  }
}

function renderMedia() {
  const music = state.music || {};
  const pod = state.podcast || {};
  const muted = music.muted ?? state.settings.muted;
  if (music.muted !== undefined) state.settings.muted = !!music.muted;

  const active = podcastActive() ? 'podcast' : musicActive() ? 'pandora' : null;
  if (active && active !== lastActiveSource) {
    mediaSource = active;
    if (active === 'podcast' && !podcastsLoaded) loadPodcastList();
  }
  lastActiveSource = active;

  let source, title, subtitle, playing, time = '', art = '';
  if (mediaSource === 'podcast') {
    const on = podcastActive();
    source = 'Podcast';
    title = on ? (pod.episode_title || 'Episode') : 'Nothing playing';
    subtitle = on ? (pod.podcast_title || '') : '';
    playing = pod.state === 'playing';
    time = on ? fmtProgress(pod.position_sec, pod.duration_sec) : '';
  } else {
    const song = music.song || {};
    const on = music.state && music.state !== 'stopped';
    source = music.configured === false ? 'Pandora (not configured)' : 'Pandora';
    playing = music.state === 'playing';
    title = on ? (song.title || '—') : 'Nothing playing';
    subtitle = on ? [song.artist, song.album].filter(Boolean).join(' · ') : '';
    art = on ? (song.cover_art_url || '') : '';
    time = on ? fmtProgress(song.elapsed_sec, song.duration_sec) : '';
    const stations = (music.stations || []).map(st => ({ value: String(st.id), label: st.name || String(st.id) }));
    if (stations.length) fillSelect($('home-station-select'), stations, song.current_station_id);
  }

  document.querySelectorAll('[data-home-source]').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.dataset.homeSource === mediaSource));
  });
  const pandoraPanel = $('home-pandora-panel');
  if (pandoraPanel) pandoraPanel.hidden = mediaSource !== 'pandora';
  const podcastPanel = $('home-podcast-panel');
  if (podcastPanel) podcastPanel.hidden = mediaSource !== 'podcast';
  document.querySelectorAll('[data-pandora-only]').forEach(n => { n.hidden = mediaSource !== 'pandora'; });
  document.querySelectorAll('[data-podcast-only]').forEach(n => {
    n.hidden = mediaSource !== 'podcast';
    n.disabled = !podcastActive();
  });
  ['home-media-next', 'home-media-like', 'home-media-ban'].forEach(id => {
    const btn = $(id);
    if (btn) btn.disabled = !musicActive();
  });
  const stop = $('home-media-stop');
  if (stop) stop.disabled = mediaSource === 'podcast' ? !podcastActive() : !musicActive();

  setText('home-media-source', source);
  setText('home-media-title', title);
  setText('home-media-subtitle', subtitle);
  setText('home-media-time', time);
  const img = $('home-media-art');
  if (img) {
    if (art && img.getAttribute('src') !== art) img.src = art;
    if (!art) img.removeAttribute('src');
    img.hidden = !art;
  }
  setText('home-media-play', playing ? '⏸ Pause' : '▶ Play');
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

async function mediaAction(label, fn, refresh) {
  try {
    await fn();
  } catch (err) {
    note(`${label}: ${err.message}`, false);
  }
  callLegacy(refresh);
}

function mediaPlayPause() {
  if (mediaSource === 'podcast') {
    return mediaAction('Podcast', async () => {
      const pod = state.podcast || {};
      const id = $('home-podcast-select')?.value;
      const episode = Number($('home-episode-select')?.value || 0);
      const sameEpisode = podcastActive() && String(pod.podcast_id) === String(id) && Number(pod.episode_index) === episode;
      if (sameEpisode || (podcastActive() && !id)) {
        await api(`/api/podcasts/${pod.state === 'playing' ? 'pause' : 'resume'}`, 'POST');
      } else {
        if (!id) throw new Error('subscribe to a podcast on the Media page first');
        if (musicActive()) await api('/api/music/stop', 'POST');
        await api('/api/podcasts/play', 'POST', { podcast_id: id, episode_index: episode });
      }
    }, 'loadPodcastStatus');
  }
  return mediaAction('Playback', async () => {
    const musicState = (state.music && state.music.state) || 'stopped';
    const station = $('home-station-select')?.value;
    if (musicState === 'stopped') {
      if (podcastActive()) await api('/api/podcasts/stop', 'POST');
      await api('/api/music/play', 'POST', station ? { station_id: station } : {});
    } else {
      await api('/api/music/pause', 'POST');
    }
  }, 'loadMusicStatus');
}

function changeStation() {
  const station = $('home-station-select')?.value;
  if (!station || !musicActive()) return;
  const current = state.music && state.music.song && state.music.song.current_station_id;
  if (String(current) === station) return;
  mediaAction('Station', () => api('/api/music/station', 'POST', { station_id: station }), 'loadMusicStatus');
}

function mediaStop() {
  if (mediaSource === 'podcast') return mediaAction('Stop', () => api('/api/podcasts/stop', 'POST'), 'loadPodcastStatus');
  return mediaAction('Stop', () => api('/api/music/stop', 'POST'), 'loadMusicStatus');
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

function initMedia() {
  try { mediaSource = localStorage.getItem(SOURCE_KEY) === 'podcast' ? 'podcast' : 'pandora'; } catch { /* ignore */ }
  document.querySelectorAll('[data-home-source]').forEach(btn => {
    btn.addEventListener('click', () => selectSource(btn.dataset.homeSource));
  });
  $('home-media-play')?.addEventListener('click', mediaPlayPause);
  $('home-media-stop')?.addEventListener('click', mediaStop);
  $('home-media-next')?.addEventListener('click', () => mediaAction('Skip', () => api('/api/music/next', 'POST'), 'loadMusicStatus'));
  $('home-media-like')?.addEventListener('click', () => mediaAction('Like', () => api('/api/music/thumbs-up', 'POST'), 'loadMusicStatus'));
  $('home-media-ban')?.addEventListener('click', () => mediaAction('Ban', () => api('/api/music/thumbs-down', 'POST'), 'loadMusicStatus'));
  $('home-media-back')?.addEventListener('click', () => mediaAction('Skip', () => api('/api/podcasts/skip', 'POST', { delta_sec: -30 }), 'loadPodcastStatus'));
  $('home-media-forward')?.addEventListener('click', () => mediaAction('Skip', () => api('/api/podcasts/skip', 'POST', { delta_sec: 30 }), 'loadPodcastStatus'));
  $('home-station-select')?.addEventListener('change', changeStation);
  $('home-podcast-select')?.addEventListener('change', e => loadEpisodes(e.target.value));
  if (mediaSource === 'podcast') loadPodcastList();
  renderMedia();
}

// ── Data cascade ───────────────────────────────────────────────────────────
// Decorative columns of numbers in the Scene sub-frame; the colour wash is CSS.

export function cascadeColumns(cols, rows, rand = Math.random) {
  return Array.from({ length: cols }, () => Array.from({ length: rows }, () => {
    const digits = 2 + Math.floor(rand() * 6);
    let value = String(1 + Math.floor(rand() * 9));
    while (value.length < digits) value += Math.floor(rand() * 10);
    return value;
  }));
}

function renderCascade() {
  const host = $('home-scene-cascade');
  if (!host) return;
  // Whole rows only: 14px type at 1.25 line height (see .lcars-data-cascade).
  const rows = host.clientHeight > 0 ? Math.max(1, Math.floor(host.clientHeight / 17.5)) : 6;
  host.replaceChildren(...cascadeColumns(6, rows).map(col => {
    const column = document.createElement('div');
    column.className = 'lcars-cascade-col';
    column.replaceChildren(...col.map(value => {
      const cell = document.createElement('span');
      cell.textContent = value;
      return cell;
    }));
    return column;
  }));
}

// ── Quick toggles ──────────────────────────────────────────────────────────

const TOGGLES = {
  'home-toggle-motion': {
    label: 'Head motion',
    async save(enabled) {
      await api('/api/settings/servo', 'PUT', { enabled });
      state.settings.motion = enabled;
      callLegacy('loadServoEnabled');
      renderHeadEnabled();
    },
  },
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
  'home-toggle-confidence': {
    label: 'Overlay confidence',
    async save(show_confidence) {
      await api('/api/settings/camera/overlay', 'PUT', { show_confidence });
    },
  },
};

async function onToggle(event) {
  const input = event.target;
  const toggle = TOGGLES[input.id];
  const out = input.closest('#card-home-vision-control') ? 'home-control-status' : 'home-action-status';
  input.disabled = true;
  try {
    await toggle.save(input.checked);
    note(`${toggle.label} ${input.checked ? 'on' : 'off'}`, true, out);
  } catch (err) {
    input.checked = !input.checked;
    note(`${toggle.label}: ${err.message}`, false, out);
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
    read('/api/settings/camera/overlay', d => check('home-toggle-confidence', d.show_confidence !== false)),
    read('/api/settings/servo', d => { state.settings.motion = d.enabled !== false; check('home-toggle-motion', state.settings.motion); }),
    read('/api/settings/servo/limits', d => setHeadRange('pan', d.min_deg, d.max_deg)),
    read('/api/settings/servo/channels', d => {
      const row = $('home-tilt-row');
      if (row) row.hidden = !d.tilt_available;
      setHeadRange('tilt', d.tilt_min_deg, d.tilt_max_deg);
    }),
  ]);
  renderHeadEnabled();
  renderAlerts();
  renderMedia();
}

// ── Head position sliders ───────────────────────────────────────────────────
// Drag to aim; while idle they follow the live angle from motion.position.
// Only the latest value is sent while a request is in flight.

const HEAD = {
  pan: { field: 'angle', path: '/api/pan', dragging: false, sending: false, pending: null },
  tilt: { field: 'tilt_angle', path: '/api/tilt', dragging: false, sending: false, pending: null },
};

function setHeadRange(axis, min, max) {
  const slider = $(`home-${axis}-slider`);
  if (!slider || !Number.isFinite(Number(min)) || !Number.isFinite(Number(max))) return;
  slider.min = min;
  slider.max = max;
}

function renderHeadEnabled() {
  const off = state.settings.motion === false;
  for (const axis of Object.keys(HEAD)) {
    const slider = $(`home-${axis}-slider`);
    if (slider) {
      slider.disabled = off;
      slider.title = off ? 'Head motion is off' : '';
    }
  }
}

function renderHead(pos) {
  for (const [axis, head] of Object.entries(HEAD)) {
    const angle = Number(pos[head.field]);
    if (pos[head.field] == null || !Number.isFinite(angle)) continue;
    if (head.dragging || head.sending) continue;
    const slider = $(`home-${axis}-slider`);
    if (slider) slider.value = Math.round(angle);
    setText(`home-${axis}-value`, fmtNum(angle, 0, '°'));
  }
}

async function sendHead(axis) {
  const head = HEAD[axis];
  if (head.sending) return;
  head.sending = true;
  try {
    while (head.pending !== null) {
      const angle = head.pending;
      head.pending = null;
      await api(head.path, 'POST', { angle });
    }
  } catch (err) {
    head.pending = null;
    note(`${axis === 'pan' ? 'Pan' : 'Tilt'}: ${err.message}`, false, 'home-control-status');
  } finally {
    head.sending = false;
  }
}

function initHeadSliders() {
  for (const axis of Object.keys(HEAD)) {
    const slider = $(`home-${axis}-slider`);
    if (!slider) continue;
    const head = HEAD[axis];
    slider.addEventListener('input', () => {
      head.dragging = true;
      setText(`home-${axis}-value`, `${slider.value}°`);
      head.pending = Number(slider.value);
      sendHead(axis);
    });
    for (const type of ['change', 'pointerup', 'pointercancel', 'blur']) {
      slider.addEventListener(type, () => { head.dragging = false; });
    }
  }
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
let cascadeTimer = null;
const CASCADE_REFRESH_MS = 6000;
let homeActive = false;

// ── Camera source switch (bottom sweep) ────────────────────────────────────

// Home omits the pan-heading dial; the Head position sliders already show it.
const CAMERA_STREAMS = { 1: '/stream?heading=0', 2: '/stream2' };
const CAMERA_KEY = 'vera-home-cam';
const EYE_NAMES = { 1: 'Left eye', 2: 'Right eye' };
let homeCam = 1;

function renderCameraSwitch() {
  document.querySelectorAll('[data-home-cam]').forEach(btn => {
    btn.setAttribute('aria-pressed', String(Number(btn.dataset.homeCam) === homeCam));
  });
  const img = $('home-camera');
  if (img) img.alt = `Live view from the ${EYE_NAMES[homeCam].toLowerCase()}`;
  setReadout('home-eye', EYE_NAMES[homeCam].replace(/ eye$/, ''));
}

function startCamera() {
  if (typeof window._registerStream === 'function') window._registerStream('home-camera', CAMERA_STREAMS[homeCam]);
}

export function selectCamera(cam) {
  homeCam = Number(cam) === 2 ? 2 : 1;
  try { localStorage.setItem(CAMERA_KEY, String(homeCam)); } catch { /* storage unavailable */ }
  renderCameraSwitch();
  if (homeActive) startCamera();
}

function enter() {
  homeActive = true;
  startCamera();
  refreshSettings();
  clearInterval(settingsTimer);
  settingsTimer = setInterval(() => { if (!document.hidden) refreshSettings(); }, SETTINGS_REFRESH_MS);
  setTimeout(renderCascade, 0);
  clearInterval(cascadeTimer);
  cascadeTimer = setInterval(() => { if (!document.hidden) renderCascade(); }, CASCADE_REFRESH_MS);
}

function leave() {
  homeActive = false;
  if (typeof window._unregisterStream === 'function') window._unregisterStream('home-camera');
  clearInterval(settingsTimer);
  settingsTimer = null;
  clearInterval(cascadeTimer);
  cascadeTimer = null;
}

export function initHome(deps = {}) {
  if (!$('tab-home')) return;
  if (deps.units) units = deps.units;
  window.addEventListener('vera:status', e => onStatus(e.detail || {}));
  window.addEventListener('vera:units', () => { if (state.status) onStatus(state.status); });
  window.addEventListener('vera:music', e => { state.music = e.detail || {}; renderMedia(); });
  window.addEventListener('vera:podcast', e => { state.podcast = e.detail || {}; renderMedia(); });
  window.addEventListener('vera:update', e => { state.update = e.detail || {}; renderAlerts(); });
  window.addEventListener('vera:route', e => {
    const { tab, previous } = e.detail || {};
    if (tab === 'home') enter();
    else if (previous === 'home') leave();
  });

  Object.keys(TOGGLES).forEach(id => $(id)?.addEventListener('change', onToggle));
  initMedia();
  renderCascade();
  $('home-alerts-ack')?.addEventListener('click', acknowledgeAlerts);
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
  initHeadSliders();
  renderCameraSwitch();
  renderAlerts();
}
