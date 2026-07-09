/* VERA Dashboard — app.js */

// ── Tab navigation ────────────────────────────────────────────────────────

const _TAB_KEY     = 'vera-active-tab';
const _DEFAULT_TAB = 'overview';

function switchTab(tabId) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  const pane = document.getElementById('tab-' + tabId);
  if (pane) pane.classList.add('active');
  const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
  if (btn) btn.classList.add('active');
  localStorage.setItem(_TAB_KEY, tabId);
}

function initTabs() {
  const saved = localStorage.getItem(_TAB_KEY) || _DEFAULT_TAB;
  switchTab(saved);
}

// ── API key management ────────────────────────────────────────────────────
// If VERA_API_KEY is set in secrets.env the server enforces authentication.
// The overlay is shown reactively (by the fetch patcher below) only when the
// server actually returns 401 — not preemptively. This way the dashboard
// works out-of-the-box when no key is configured.

const VERA_API_KEY = localStorage.getItem('vera_api_key') || '';

// Patch fetch() globally to inject the X-API-Key header on every request.
(function () {
  const _orig = window.fetch;
  window.fetch = function (url, opts) {
    opts = Object.assign({}, opts);
    opts.headers = Object.assign({ 'X-API-Key': VERA_API_KEY }, opts.headers || {});
    return _orig.call(this, url, opts).then(resp => {
      if (resp.status === 401) { _showKeyOverlay(); }
      return resp;
    });
  };
})();

function _showKeyOverlay() {
  if (document.getElementById('vera-key-overlay')) return;
  const overlay = document.createElement('div');
  overlay.id = 'vera-key-overlay';
  overlay.style.cssText =
    'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.85);' +
    'display:flex;align-items:center;justify-content:center;z-index:9999;';
  overlay.innerHTML = `
    <div style="background:#1e1e2e;padding:2rem;border-radius:12px;min-width:320px;text-align:center;border:1px solid #444;">
      <h2 style="color:#cdd6f4;margin-top:0">VERA Dashboard</h2>
      <p style="color:#a6adc8">Enter the API key from <code>/etc/desktop-assistant/secrets.env</code></p>
      <input id="vera-key-input" type="password" placeholder="API key"
        style="width:100%;padding:.6rem;font-size:1rem;border-radius:6px;border:1px solid #555;background:#313244;color:#cdd6f4;box-sizing:border-box;margin-bottom:.8rem;" />
      <button onclick="_saveApiKey()"
        style="width:100%;padding:.6rem 1.2rem;font-size:1rem;border-radius:6px;border:none;background:#89b4fa;color:#1e1e2e;cursor:pointer;font-weight:600;">
        Connect
      </button>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#vera-key-input').focus();
  overlay.querySelector('#vera-key-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') _saveApiKey();
  });
}

function _saveApiKey() {
  const key = (document.getElementById('vera-key-input') || {}).value || '';
  if (key.trim()) {
    localStorage.setItem('vera_api_key', key.trim());
    location.reload();
  }
}

const WS_URL = `ws://${location.host}/ws?key=${encodeURIComponent(VERA_API_KEY)}`;

// Append ?key= to URLs used in img.src (can't use custom headers for image loads).
function authUrl(path) {
  return `${path}?key=${encodeURIComponent(VERA_API_KEY)}`;
}
let ws = null;
let wsRetryMs = 1000;

// ── Pan slider state ──────────────────────────────────────────────
// True while the user is dragging the pan slider — auto-updates from
// the WebSocket are suppressed during this window so they don't fight
// the user's input.
let _isDragging = false;
let _panDebounceTimer = null;

function _onPanSliderInput() {
  const angle = parseFloat(document.getElementById("pan-slider").value);
  document.getElementById("pan-angle-display").textContent = angle + "°";
  // Debounce: send the pan command 80 ms after the user stops moving.
  clearTimeout(_panDebounceTimer);
  _panDebounceTimer = setTimeout(() => {
    fetch("/api/pan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ angle }),
    }).catch(() => {});
  }, 80);
}

// Called once the DOM is ready (see bottom of file).
function _initPanSlider() {
  const slider = document.getElementById("pan-slider");
  if (!slider) return;
  slider.oninput = _onPanSliderInput;
  slider.addEventListener("pointerdown",   () => { _isDragging = true; });
  slider.addEventListener("pointerup",     () => { _isDragging = false; });
  slider.addEventListener("pointercancel", () => { _isDragging = false; });
}


function connectWS() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    wsRetryMs = 1000;
    setHealth("ok");
  };

  ws.onclose = () => {
    setHealth("unknown");
    setTimeout(connectWS, wsRetryMs);
    wsRetryMs = Math.min(wsRetryMs * 1.5, 15000);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data);
      updateDashboard(data);
    } catch (e) { console.error('[VERA] WS update error:', e); }
  };
}

// ── Room status polling (REST fallback for stability gauge) ───────
// Polls /api/room/status every 10 s so the gauge updates even if
// the WebSocket path fails to deliver room_detail.
function _startRoomPoller() {
  async function poll() {
    try {
      const r = await fetch("/api/room/status");
      if (r.ok) updateRoomDetail(await r.json());
    } catch (_) {}
    setTimeout(poll, 10000);
  }
  setTimeout(poll, 2000); // first poll after 2 s (let WS try first)
}

// ── Dashboard update ──────────────────────────────────────────────

function updateDashboard(data) {
  const last = data.last || {};

  // Header
  const version = data.version || "?";
  document.getElementById("version-uptime").textContent = `v${version}`;
  setHealth(data.healthy === false ? "degraded" : "ok");

  // Telemetry
  const temp = last["thermal.temp"];
  if (temp) {
    const renderTemp = (elemId, celsius) => {
      if (celsius == null || Number.isNaN(Number(celsius))) {
        el(elemId).textContent = "—";
      } else {
        const c = num(celsius, 1);
        const f = num((Number(celsius) * 9) / 5 + 32, 1);
        el(elemId).textContent = `${c} °C / ${f} °F`;
      }
      el(elemId).style.color = temp.ok === false ? "var(--red)" : "var(--blue)";
    };

    renderTemp("stat-temp-case", temp.case_celsius);
    renderTemp("stat-temp-soc", temp.cpu_celsius);
    renderTemp("stat-temp-blended", temp.blended_celsius ?? temp.celsius);
  }

  const fan = last["thermal.fan"];
  const rpm = last["thermal.rpm"];
  if (fan) {
    const duty = Math.round(fan.duty ?? 0);
    const tachOn = rpm ? rpm.enabled !== false : true;
    const rpmVal = (tachOn && rpm && rpm.rpm != null) ? `${rpm.rpm} RPM` : (tachOn ? "— RPM" : "tach off");
    const ovr = fan.override ? " ⚡override" : "";
    el("stat-fan").textContent = `${duty}%  ·  ${rpmVal}${ovr}`;
    el("stat-fan").title = `backend: ${fan.backend ?? "?"}${fan.override ? ` | override pinned at ${Math.round(fan.override_duty ?? duty)}%` : ""}`;
    el("stat-fan").style.color = fan.override ? "var(--yellow)" : "";
  }

  const motion = last["motion.position"];
  if (motion) {
    el("stat-servo").textContent = `${num(motion.angle, 1)}°`;
    if (!_isDragging) {
      const slider = el("pan-slider");
      if (slider) {
        slider.value = Math.round(motion.angle);
        el("pan-angle-display").textContent = Math.round(motion.angle) + "°";
      }
    }
  }

  const audio = last["audio.level"];
  if (audio) {
    el("stat-audio").textContent = `${num(audio.dbfs, 1)} dBFS`;
    el("stat-audio").title = `rms=${num(audio.rms, 4)}`;
  }

  const vad = last["audio.vad"];
  if (vad) {
    const vadEl = el("audio-vad-state");
    if (vadEl) {
      vadEl.textContent = vad.active ? "Speaking" : "Idle";
      vadEl.style.color = vad.active ? "var(--green)" : "var(--text-dim)";
    }
  }

  const liveSpectrum = last["audio.spectrum"];
  if (liveSpectrum && Array.isArray(liveSpectrum.bins)) {
    drawAudioSpectrum(liveSpectrum);
  }
  const toneNow = el("audio-spectrum-tone-now");
  const tone = last["av.spectrum_test_tone"];
  if (toneNow) {
    if (
      tone &&
      tone.active === true &&
      typeof tone.hz === "number" &&
      (typeof tone.ends_ts !== "number" || (Date.now() / 1000) <= tone.ends_ts)
    ) {
      toneNow.textContent = `Now playing: ${Math.round(tone.hz)} Hz`;
      toneNow.style.color = "var(--yellow)";
    } else {
      toneNow.textContent = "Now playing: —";
      toneNow.style.color = "var(--text-dim)";
    }
  }

  const spoke = last["av.spoke"];
  if (spoke && spoke.text) {
    const snip = spoke.text.length > 60 ? spoke.text.slice(0, 60) + "…" : spoke.text;
    el("stat-spoken").textContent = `"${snip}"`;
    const repeatBtn = el("repeat-spoken-btn");
    if (repeatBtn) repeatBtn.disabled = false;
  }

  // Face overlay badge (boxes are now drawn server-side into the JPEG stream)
  const pf = last["perception.faces"];
  const po = last["perception.objects"];

  if (pf != null || po != null) {
    const count = pf?.count ?? 0;
    const names = (pf?.faces || [])
      .filter(f => f.name)
      .map(f => f.name)
      .join(", ");
    const objCount = po?.count ?? 0;
    let overlayText = count === 0
      ? "0 faces"
      : `${count} face${count !== 1 ? "s" : ""}${names ? ": " + names : ""}`;
    if (objCount > 0) overlayText += ` · ${objCount} object${objCount !== 1 ? "s" : ""}`;
    el("face-overlay").textContent = overlayText;
  }

  // Services pills
  const services = data.services || {};
  const svcKeys = Object.keys(services);
  const svcContainer = el("services-container");
  if (svcKeys.length > 0) {
    svcContainer.innerHTML = svcKeys.sort().map(name => {
      const state = services[name];
      const cls = state === "running" ? "pill-ok"
                : state === "error"   ? "pill-fail"
                : state === "stopped" ? "pill-warn"
                : "pill-unknown";
      const label = state === "error" ? `${esc(name)} ⚠` : esc(name);
      return `<span class="pill ${cls}" title="${esc(state)}">${label}</span>`;
    }).join("");
  }

  // Event log
  const events = data.events || [];
  renderEventLog(events);
  renderSttTranscriptLog(events);

  // FPS overlays — driven by server-side frame counters (Chrome-compatible)
  updateFpsOverlays(data);

  // System resource graphs
  if (data.cpu_history != null) {
    el("stat-cpu").textContent = `${Math.round(data.cpu_percent ?? 0)}%`;
    drawSparkline("cpu-graph", data.cpu_history, "#58a6ff", { min: 0, max: 100 });
  }
  if (data.mem_history != null) {
    el("stat-mem").textContent = `${Math.round(data.mem_percent ?? 0)}%`;
    drawSparkline("mem-graph", data.mem_history, "#3fb950", { min: 0, max: 100 });
  }

  // Music state/song updates from bus events
  const musicState = last["music.state_changed"];
  if (musicState) _applyMusicState(musicState.state || "stopped");
  const musicSong = last["music.song_changed"];
  if (musicSong) _applyMusicSong(musicSong);
  const musicStations = last["music.stations_updated"];
  if (musicStations) _applyMusicStations(musicStations.stations || []);

  // Room display
  if (data.room !== undefined) {
    el("stat-room").textContent = data.room || "Unknown";
  }
  if (data.room_detail) updateRoomDetail(data.room_detail);

  // IoT plugin devices — auto-render cards in Smart Home tab
  if (data.iot && typeof data.iot === "object") {
    renderIoTDevices(data.iot);
  }

  // Process list — updated at WS rate (same as CPU/mem sparklines)
  if (Array.isArray(data.processes)) {
    renderProcesses(data.processes);
  }
}

// ── IoT plugin device cards ────────────────────────────────────────

// Sparkline history buffers per IoT device id
const _iotHistories = {};
// Track which IoT cards were rendered from the registry (vs. hard-wired)
const _iotRegisteredCards = new Set();

function _fmtIotInterval(seconds) {
  const s = Math.max(1, Number(seconds || 0));
  if (s >= 3600) return `${Math.round(s / 3600)}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}

function renderIoTDevices(iot) {
  const pane = el("tab-smart-home");
  if (!pane) return;

  const activeIds = new Set(Object.keys(iot));

  // Remove cards for devices that were unregistered
  for (const cardId of [..._iotRegisteredCards]) {
    const devId = cardId.replace(/^iot-card-/, "");
    if (!activeIds.has(devId)) {
      const card = el(cardId);
      if (card) card.remove();
      _iotRegisteredCards.delete(cardId);
      delete _iotHistories[devId];
    }
  }

  for (const [deviceId, snap] of Object.entries(iot)) {
    const cardId = `iot-card-${deviceId}`;
    let card = el(cardId);

    if (!card) {
      // First time — create the card and append to pane
      card = document.createElement("section");
      card.className = "card";
      card.id = cardId;
      card.dataset.iotDevice = deviceId;
      card.innerHTML = `
        <h2>
          <span class="drag-handle" title="Drag to reorder">⠿</span>
          <span class="iot-card-icon">${snap.device_icon || "🔌"}</span>
          <span class="iot-card-name">${snap.device_name || deviceId}</span>
          <span style="margin-left:auto;display:flex;gap:6px">
            <button class="btn btn-secondary btn-sm" data-iot-action="announce" data-device-id="${deviceId}" title="Speak status via TTS">📢</button>
            <button class="btn btn-secondary btn-sm" data-iot-action="config"   data-device-id="${deviceId}" title="Configure device">⚙️</button>
            <button class="btn btn-sm" style="background:rgba(248,81,73,0.15);border-color:#f8514966;color:#f85149" data-iot-action="remove" data-device-id="${deviceId}" title="Remove device">🗑</button>
          </span>
        </h2>
        <div class="iot-primary-row">
          <span class="resource-pct iot-primary-value">—</span>
          <span class="iot-primary-unit"></span>
          <span class="pill iot-badge-row"></span>
          <span class="iot-detail" style="font-size:11px;color:var(--muted);margin-left:auto"></span>
        </div>
        <div class="iot-sparkline-meta" style="font-size:11px;color:var(--text-muted);margin:4px 0 2px 0"></div>
        <canvas class="resource-canvas iot-sparkline" id="iot-graph-${deviceId}" height="60" width="400"></canvas>
        <div class="iot-metrics"></div>
        <div class="iot-actions" style="display:none;gap:6px;flex-wrap:wrap;margin-top:8px"></div>
      `;
      pane.appendChild(card);
      _iotHistories[deviceId] = [];
      _iotRegisteredCards.add(cardId);
    }

    // Update card content
    const available = snap.available !== false;
    const disp      = snap.display || {};
    const primary   = disp.primary   || {};
    const badges    = disp.badges    || [];
    const metrics   = disp.metrics   || [];
    const detail    = disp.detail    || "";
    const actions   = snap.actions   || [];

    const valueEl   = card.querySelector(".iot-primary-value");
    const unitEl    = card.querySelector(".iot-primary-unit");
    const badgeEl   = card.querySelector(".iot-badge-row");
    const detailEl  = card.querySelector(".iot-detail");
    const sparkMetaEl = card.querySelector(".iot-sparkline-meta");
    const metricsEl = card.querySelector(".iot-metrics");
    const actionsEl = card.querySelector(".iot-actions");
    const horizonMin = Math.max(1, Number(snap.history_horizon_min || 120));
    const updateS = Math.max(1, Number(snap.history_update_s || 60));
    const historyLabel = snap.history_label || "History";

    if (!available) {
      if (valueEl) valueEl.textContent = "—";
      if (unitEl)  unitEl.textContent = "";
      if (badgeEl) { badgeEl.textContent = snap.error || "unavailable"; badgeEl.style.cssText = "background:#f8514933;color:#f85149;border:1px solid #f8514966"; }
      if (detailEl) detailEl.textContent = "";
      if (sparkMetaEl) sparkMetaEl.textContent = `${historyLabel} · ${horizonMin}m horizon · ${_fmtIotInterval(updateS)} update`;
      if (metricsEl) metricsEl.innerHTML = "";
      if (actionsEl) { actionsEl.innerHTML = ""; actionsEl.style.display = "none"; }
    } else {
      const color = primary.color || "#58a6ff";
      if (valueEl) { valueEl.textContent = primary.value || "—"; valueEl.style.color = color; }
      if (unitEl)  unitEl.textContent = primary.unit || "";

      // Badges
      if (badgeEl) {
        if (badges.length > 0) {
          const b = badges[0];
          const bc = b.color || "#3fb950";
          badgeEl.textContent = b.text || "";
          badgeEl.style.cssText = `background:${bc}33;color:${bc};border:1px solid ${bc}66`;
        } else {
          badgeEl.textContent = "";
          badgeEl.style.cssText = "";
        }
      }

      if (detailEl) detailEl.textContent = detail;
      if (sparkMetaEl) sparkMetaEl.textContent = `${historyLabel} · ${horizonMin}m horizon · ${_fmtIotInterval(updateS)} update`;

      // Metrics grid
      if (metricsEl) {
        metricsEl.innerHTML = metrics
          .map(m => `<span><b>${m.label}:</b> ${m.value}</span>`)
          .join("");
      }

      // Action buttons
      if (actionsEl) {
        if (actions.length > 0) {
          actionsEl.style.display = "flex";
          actionsEl.innerHTML = actions.map(a => {
            const btnColor = a.color || "#58a6ff";
            return `<button class="btn btn-sm" style="background:${btnColor}22;border:1px solid ${btnColor}66;color:${btnColor}"
              data-iot-action="do-action" data-device-id="${deviceId}" data-action-id="${a.id}"
              data-requires-pin="${!!a.requires_pin}"
              data-requires-input="${!!a.requires_input}"
              data-action-prompt="${a.input_prompt ? a.input_prompt.replace(/"/g, '&quot;') : ''}"
              data-action-param="${a.input_param || 'value'}"
              title="${a.label}">
              ${a.icon || ""} ${a.label}
            </button>`;
          }).join("");
        } else {
          actionsEl.style.display = "none";
          actionsEl.innerHTML = "";
        }
      }
    }

    // Sparkline — server now sends full accumulated history; just render it
    if (snap.history && snap.history.length) {
      _iotHistories[deviceId] = snap.history;
    }
    const hist = _iotHistories[deviceId];
    if (hist && hist.length >= 2) {
      const color = (disp.primary || {}).color || "#58a6ff";
      drawSparkline(`iot-graph-${deviceId}`, hist, color);
    }
  }
}

async function announceIotDevice(deviceId) {
  try {
    const resp = await fetch(`/api/iot/${deviceId}/announce`, { method: "POST" });
    const data = await resp.json();
    if (!data.ok) console.warn("IoT announce error:", data.error);
  } catch (e) { console.error("IoT announce request failed:", e); }
}

async function doIotAction(deviceId, action, requiresPin, requiresInput, inputPrompt, inputParam) {
  let params = {};
  if (requiresPin) {
    const pin = window.prompt(`Enter PIN to ${action} ${deviceId}:`);
    if (pin === null) return; // user cancelled
    params.pin = pin;
  }
  if (requiresInput) {
    const val = window.prompt(inputPrompt || `Enter value for ${action}:`);
    if (val === null) return; // user cancelled
    params[inputParam || "value"] = val;
  }
  try {
    const resp = await fetch(`/api/iot/${deviceId}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, params }),
    });
    const data = await resp.json();
    if (!data.ok) {
      alert(`Action failed: ${data.message || "Unknown error"}`);
    }
  } catch (e) { console.error("IoT action request failed:", e); }
}

// ── IoT CRUD modals ───────────────────────────────────────────────

let _iotConfigTargetId = null;

async function openIoTAddModal() {
  const sel   = document.getElementById("iot-add-type-select");
  const errEl = document.getElementById("iot-add-error");
  const cfgEl = document.getElementById("iot-add-config");
  const modal = document.getElementById("iot-add-modal");
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  if (cfgEl) cfgEl.value = "{}";
  if (sel) sel.innerHTML = "<option value=''>— loading… —</option>";
  // Show the modal immediately — don't wait for the API fetch
  if (modal) modal.style.display = "flex";
  if (sel) {
    try {
      const resp = await fetch("/api/iot/types");
      const data = await resp.json();
      if (data.types && data.types.length > 0) {
        sel.innerHTML = data.types
          .map(t => `<option value="${t.type_id}">${t.device_icon || "🔌"} ${t.device_name} (${t.type_id})</option>`)
          .join("");
      } else {
        sel.innerHTML = "<option value=''>No plugin types found</option>";
      }
    } catch (e) {
      sel.innerHTML = "<option value=''>Error loading types</option>";
    }
  }
}

function closeIoTAddModal() {
  const modal = document.getElementById("iot-add-modal");
  if (modal) modal.style.display = "none";
}

async function submitIoTAdd() {
  const sel   = document.getElementById("iot-add-type-select");
  const cfgEl = document.getElementById("iot-add-config");
  const errEl = document.getElementById("iot-add-error");
  const btn   = document.getElementById("iot-add-submit-btn");
  if (!sel || !sel.value) {
    if (errEl) { errEl.textContent = "Please select a plugin type."; errEl.style.display = "block"; }
    return;
  }
  let cfg = {};
  try { cfg = JSON.parse(cfgEl ? cfgEl.value || "{}" : "{}"); }
  catch (e) {
    if (errEl) { errEl.textContent = "Invalid JSON config: " + e.message; errEl.style.display = "block"; }
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch("/api/iot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type_id: sel.value, config: cfg })
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      const msg = (data.detail || data.error || "Unknown error");
      if (errEl) { errEl.textContent = msg; errEl.style.display = "block"; }
    } else {
      closeIoTAddModal();
    }
  } catch (e) {
    if (errEl) { errEl.textContent = "Request failed: " + e.message; errEl.style.display = "block"; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function openIoTConfigModal(deviceId) {
  _iotConfigTargetId = deviceId;
  const titleEl = document.getElementById("iot-config-modal-title");
  const cfgEl   = document.getElementById("iot-config-textarea");
  const errEl   = document.getElementById("iot-config-error");
  const modal   = document.getElementById("iot-config-modal");
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  if (titleEl) titleEl.textContent = `⚙️ Configure: ${deviceId}`;
  if (cfgEl) cfgEl.value = "{}";
  // Show the modal immediately — don't wait for the API fetch
  if (modal) modal.style.display = "flex";
  try {
    const resp = await fetch(`/api/iot/${deviceId}`);
    const data = await resp.json();
    if (data.config && typeof data.config === "object") {
      if (cfgEl) cfgEl.value = JSON.stringify(data.config, null, 2);
    }
  } catch (e) { /* leave default */ }
}

function closeIoTConfigModal() {
  const modal = document.getElementById("iot-config-modal");
  if (modal) modal.style.display = "none";
  _iotConfigTargetId = null;
}

async function submitIoTConfig() {
  const cfgEl = document.getElementById("iot-config-textarea");
  const errEl = document.getElementById("iot-config-error");
  const btn   = document.getElementById("iot-config-submit-btn");
  if (!_iotConfigTargetId) return;
  let cfg = {};
  try { cfg = JSON.parse(cfgEl ? cfgEl.value || "{}" : "{}"); }
  catch (e) {
    if (errEl) { errEl.textContent = "Invalid JSON: " + e.message; errEl.style.display = "block"; }
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch(`/api/iot/${_iotConfigTargetId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: cfg })
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      const msg = data.detail || data.error || "Unknown error";
      if (errEl) { errEl.textContent = msg; errEl.style.display = "block"; }
    } else {
      closeIoTConfigModal();
    }
  } catch (e) {
    if (errEl) { errEl.textContent = "Request failed: " + e.message; errEl.style.display = "block"; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function removeIoTDevice(deviceId) {
  if (!confirm(`Remove IoT device "${deviceId}"? This stops the device and removes it from the registry.`)) return;
  try {
    const resp = await fetch(`/api/iot/${deviceId}`, { method: "DELETE" });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      alert(`Remove failed: ${data.detail || data.error || "Unknown error"}`);
    }
    // Card removal happens automatically on next renderIoTDevices() call
  } catch (e) {
    alert("Request failed: " + e.message);
  }
}



// Ring buffer storing the last 20 similarity readings for the history sparkline.
const _roomSimHistory = [];
const _ROOM_SIM_HISTORY_MAX = 20;

function _pushRoomSim(sim) {
  if (sim == null) return;
  _roomSimHistory.push(sim);
  if (_roomSimHistory.length > _ROOM_SIM_HISTORY_MAX)
    _roomSimHistory.shift();
}

function _drawRoomSparkline(thresh) {
  const canvas = el("room-sim-sparkline");
  if (!canvas || _roomSimHistory.length < 1) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth  || 160;
  const h = canvas.clientHeight || 40;
  if (canvas.width  !== Math.round(w * dpr) ||
      canvas.height !== Math.round(h * dpr)) {
    canvas.width  = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pad = 4;
  const yw  = h - pad * 2;
  const xw  = w - pad * 2;

  // Map similarity [0.6, 1.0] → pixel height (clamp outside that range)
  const simMin = 0.60, simMax = 1.00;
  const toY = v => pad + yw * (1 - Math.min(1, Math.max(0, (v - simMin) / (simMax - simMin))));
  const toX = i => pad + (xw / Math.max(_roomSimHistory.length - 1, 1)) * i;

  // Threshold line
  const ty = toY(thresh);
  ctx.beginPath();
  ctx.setLineDash([4, 3]);
  ctx.strokeStyle = "rgba(210,153,34,0.5)";
  ctx.lineWidth = 1;
  ctx.moveTo(pad, ty);
  ctx.lineTo(w - pad, ty);
  ctx.stroke();
  ctx.setLineDash([]);

  // Threshold label
  ctx.fillStyle = "rgba(210,153,34,0.7)";
  ctx.font = "9px monospace";
  ctx.fillText((thresh * 100).toFixed(0) + "%", w - pad - 22, ty - 2);

  // Area fill
  const grad = ctx.createLinearGradient(0, pad, 0, h - pad);
  grad.addColorStop(0, "rgba(88,166,255,0.25)");
  grad.addColorStop(1, "rgba(88,166,255,0.02)");
  ctx.beginPath();
  ctx.moveTo(toX(0), h - pad);
  for (let i = 0; i < _roomSimHistory.length; i++) {
    ctx.lineTo(toX(i), toY(_roomSimHistory[i]));
  }
  ctx.lineTo(toX(_roomSimHistory.length - 1), h - pad);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  for (let i = 0; i < _roomSimHistory.length; i++) {
    const x = toX(i), y = toY(_roomSimHistory[i]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.strokeStyle = "#58a6ff";
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();

  // Current value dot
  const last = _roomSimHistory[_roomSimHistory.length - 1];
  const dotColor = last >= thresh ? "#3fb950" : last >= thresh - 0.1 ? "#d2993a" : "#f85149";
  ctx.beginPath();
  ctx.arc(toX(_roomSimHistory.length - 1), toY(last), 3, 0, Math.PI * 2);
  ctx.fillStyle = dotColor;
  ctx.fill();
}

function updateRoomDetail(d) {
  if (!d) return;

  // Scene stability bar
  const sim = d.last_similarity;
  const thresh = d.similarity_thresh != null ? d.similarity_thresh : 0.85;
  const fillEl = el("room-stability-fill");
  const pctEl  = el("room-stability-pct");
  if (fillEl && pctEl) {
    if (sim != null) {
      const pct = Math.round(sim * 100);
      fillEl.style.width = pct + "%";
      if (sim >= thresh)            fillEl.style.background = "var(--green)";
      else if (sim >= thresh - 0.1) fillEl.style.background = "var(--yellow)";
      else                          fillEl.style.background = "var(--red)";
      pctEl.textContent = pct + "%";
    } else {
      fillEl.style.width = "0%";
      fillEl.style.background = "var(--text-dim)";
      pctEl.textContent = "—";
    }
  }

  // Similarity history sparkline + strikes badge
  _pushRoomSim(sim);
  _drawRoomSparkline(thresh);
  const count = d.consec_diverged  || 0;
  const max   = d.consec_diverged_threshold || 3;
  const badge = el("room-strikes-badge");
  if (badge) {
    badge.textContent = `${count} / ${max} strikes`;
    badge.className   = "room-strikes-badge" +
      (count === 0 ? "" : count < max ? " warn" : " danger");
  }

  // Status chips
  const chips = [];
  if (!d.baseline_ready) {
    chips.push(`<span class="room-chip room-chip-warn">⚠ No baseline</span>`);
  }
  if (d.last_skip_reason === "low_light") {
    chips.push(`<span class="room-chip room-chip-warn">🌑 Low light</span>`);
  } else if (d.last_skip_reason === "faces") {
    chips.push(`<span class="room-chip room-chip-info">👤 Faces — skipped</span>`);
  } else if (d.faces_present && d.skip_when_faces) {
    chips.push(`<span class="room-chip room-chip-info">👤 Faces visible</span>`);
  }
  if (count >= max) {
    chips.push(`<span class="room-chip room-chip-err">⚠ Diverged</span>`);
  } else if (chips.length === 0) {
    chips.push(`<span class="room-chip room-chip-ok">✓ Scanning</span>`);
  }
  const chipsRow = el("room-chips-row");
  if (chipsRow) chipsRow.innerHTML = chips.join("");

  // Last scan age
  const age    = d.last_check_age_s;
  const scanEl = el("room-last-scan");
  if (scanEl) {
    if (age == null)      scanEl.textContent = "Never";
    else if (age < 60)    scanEl.textContent = `${Math.round(age)}s ago`;
    else if (age < 3600)  scanEl.textContent = `${Math.round(age / 60)}m ago`;
    else                  scanEl.textContent = `${(age / 3600).toFixed(1)}h ago`;
  }
}


function drawSparkline(canvasId, values, color, opts = {}) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  // Resize backing store if needed
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width  = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!values || values.length < 2) return;

  const pad = 4;
  const graphW = w - pad * 2;
  const graphH = h - pad * 2;
  const step = graphW / (values.length - 1);

  let minVal;
  let maxVal;
  if (Number.isFinite(opts.min) && Number.isFinite(opts.max) && opts.max > opts.min) {
    // Fixed range keeps comparable vertical scale across related graphs.
    minVal = Number(opts.min);
    maxVal = Number(opts.max);
  } else {
    // Auto-scale Y axis to the data range so small variations are visible.
    // Apply a 10% padding around the range; when all values are identical
    // (truly flat) centre the line at 50% height.
    minVal = Math.min(...values);
    maxVal = Math.max(...values);
    const range = maxVal - minVal;
    if (range < 1e-9) {
      minVal = minVal - 1;
      maxVal = maxVal + 1;
    } else {
      const pad10 = range * 0.10;
      minVal -= pad10;
      maxVal += pad10;
    }
  }
  const scale = maxVal - minVal;
  const toY = v => pad + graphH - ((v - minVal) / scale) * graphH;

  // Fill area under the line
  ctx.beginPath();
  ctx.moveTo(pad, pad + graphH);
  for (let i = 0; i < values.length; i++) {
    ctx.lineTo(pad + i * step, toY(values[i]));
  }
  ctx.lineTo(pad + (values.length - 1) * step, pad + graphH);
  ctx.closePath();
  ctx.fillStyle = color + "28";  // ~16% opacity fill
  ctx.fill();

  // Draw line
  ctx.beginPath();
  for (let i = 0; i < values.length; i++) {
    const x = pad + i * step;
    const y = toY(values[i]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();

  // Draw mid-range reference line (50% of data range)
  ctx.setLineDash([2, 4]);
  ctx.strokeStyle = "rgba(139,148,158,0.25)";
  ctx.lineWidth = 1;
  const midY = toY(minVal + scale * 0.5);
  ctx.beginPath();
  ctx.moveTo(pad, midY);
  ctx.lineTo(pad + graphW, midY);
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawAudioSpectrum(payload) {
  const canvas = el("audio-spectrum-canvas");
  if (!canvas || !payload || !Array.isArray(payload.bins)) return;
  const bins = payload.bins;
  const meta = el("audio-spectrum-meta");

  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 400;
  const h = canvas.clientHeight || 90;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  // Background + grid
  ctx.fillStyle = "rgba(13,17,23,0.35)";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "rgba(139,148,158,0.25)";
  ctx.lineWidth = 1;
  for (let i = 1; i <= 3; i++) {
    const y = Math.round((h * i) / 4);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  if (bins.length === 0) return;
  const barW = w / bins.length;
  for (let i = 0; i < bins.length; i++) {
    const mag = Math.max(0, Math.min(1, Number(bins[i]) || 0));
    const bh = mag * (h - 2);
    const x = i * barW;
    const y = h - bh;
    const hue = 200 - Math.round(140 * mag); // blue -> green/yellow
    ctx.fillStyle = `hsl(${hue} 85% 55%)`;
    ctx.fillRect(x + 0.5, y, Math.max(1, barW - 1), bh);
  }

  if (meta) {
    const sr = Number(payload.sample_rate || 16000);
    const maxHz = Number(payload.max_hz || sr / 2);
    meta.textContent = `FFT: ${bins.length} bins · 0–${Math.round(maxHz)} Hz`;
  }
}

// ── Health badge ──────────────────────────────────────────────────

function setHealth(state) {
  const el = document.getElementById("health-badge");
  el.className = "badge";
  if (state === "ok") {
    el.classList.add("badge-ok");
    el.textContent = "⬤ OK";
  } else if (state === "degraded") {
    el.classList.add("badge-degraded");
    el.textContent = "⬤ DEGRADED";
  } else {
    el.classList.add("badge-unknown");
    el.textContent = "⬤ connecting";
  }
}

// ── Event log ─────────────────────────────────────────────────────

function renderEventLog(events) {
  const container = el("event-log");
  container.innerHTML = "";
  // Newest first (column-reverse makes it appear at top visually)
  for (const ev of [...events].reverse()) {
    const ts = new Date(ev.ts * 1000).toLocaleTimeString();
    const body = JSON.stringify(ev.payload ?? {});
    const snip = body.length > 80 ? body.slice(0, 80) + "…" : body;
    const row = document.createElement("div");
    row.className = "event-row";
    row.innerHTML = `
      <span class="event-ts">${ts}</span>
      <span class="event-topic">${esc(ev.topic)}</span>
      <span class="event-body">${esc(snip)}</span>
    `;
    container.appendChild(row);
  }
}

function renderSttTranscriptLog(events) {
  const container = el("stt-transcript-log");
  if (!container) return;
  container.innerHTML = "";

  const transcriptEvents = (events || [])
    .filter((ev) => ev && ev.topic === "voice.transcript")
    .slice(-20);

  if (transcriptEvents.length === 0) {
    const row = document.createElement("div");
    row.className = "event-row";
    row.innerHTML = `
      <span class="event-ts">—</span>
      <span class="event-topic">voice.transcript</span>
      <span class="event-body" style="color:var(--text-dim)">No transcripts yet.</span>
    `;
    container.appendChild(row);
    return;
  }

  for (const ev of [...transcriptEvents].reverse()) {
    const ts = new Date(ev.ts * 1000).toLocaleTimeString();
    const payload = ev.payload || {};
    const text = typeof payload.text === "string" ? payload.text.trim() : "";
    const elapsed = Number(payload.elapsed_s);
    const tail = Number.isFinite(elapsed) ? ` (${elapsed.toFixed(2)}s)` : "";
    const snip = text.length > 160 ? text.slice(0, 160) + "…" : text || "(empty)";
    const row = document.createElement("div");
    row.className = "event-row";
    row.innerHTML = `
      <span class="event-ts">${ts}</span>
      <span class="event-topic">voice.transcript${tail}</span>
      <span class="event-body">${esc(snip)}</span>
    `;
    container.appendChild(row);
  }
}

// ── Face registry ─────────────────────────────────────────────────

async function loadFaces() {
  const tbody = el("face-tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="empty-row">Loading…</td></tr>`;
  try {
    const r = await fetch("/api/faces");
    const data = await r.json();
    const faces = data.faces || [];
    if (faces.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty-row">No faces registered yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = "";
    for (const face of faces) {
      tbody.appendChild(makeFaceRow(face));
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-row">Error loading faces.</td></tr>`;
  }
}

async function refreshFaces() {
  try {
    await fetch("/api/faces/refresh", { method: "POST" });
  } catch (_) {}
  await loadFaces();
}

function makeFaceRow(face) {
  const tr = document.createElement("tr");
  tr.dataset.faceId = face.id;
  const firstSeen = face.first_seen ? fmtTime(face.first_seen) : "—";
  const lastSeen  = face.last_seen  ? fmtAge(face.last_seen)   : "—";
  const count     = face.seen_count ?? "—";

  const thumbHtml = face.has_thumb
    ? `<img src="${authUrl(`/api/faces/${esc(face.id)}/thumb`)}" class="face-thumb face-thumb-clickable"
           alt="face"
           data-lightbox-src="${authUrl(`/api/faces/${esc(face.id)}/photo`)}"
           data-lightbox-label="${esc(face.name || 'Unknown')}" />`
    : `<div class="face-thumb face-thumb-placeholder">?</div>`;

  tr.innerHTML = `
    <td><input type="checkbox" class="face-merge-cb" onchange="updateMergeBtn()" title="Select to merge" /></td>
    <td class="name-cell">
      ${thumbHtml}
      <input class="name-input" type="text" value="${esc(face.name || "")}"
             id="name-${esc(face.id)}" title="id: ${esc(face.id)}" />
      <button class="btn btn-save btn-sm" onclick="saveName('${esc(face.id)}')">Save</button>
    </td>
    <td>${firstSeen}</td>
    <td>${lastSeen}</td>
    <td>${count}×</td>
    <td>
      <button class="btn btn-train btn-sm" id="train-btn-${esc(face.id)}" onclick="captureTrainingImage('${esc(face.id)}')">📷 Train</button>
      <button class="btn btn-danger btn-sm" onclick="deleteFace('${esc(face.id)}')">Delete</button>
    </td>
  `;

  // Wire click after innerHTML is parsed so the handler gets the real string values
  const img = tr.querySelector(".face-thumb-clickable");
  if (img) {
    img.addEventListener("click", () =>
      openLightbox(img.dataset.lightboxSrc, img.dataset.lightboxLabel)
    );
  }

  return tr;
}

function updateMergeBtn() {
  const checked = [...document.querySelectorAll(".face-merge-cb:checked")];
  const btn = el("merge-btn");
  const hint = el("merge-hint");
  if (!btn) return;
  btn.disabled = (checked.length !== 2);
  hint.textContent = checked.length === 0 ? "Select 2 faces to merge"
    : checked.length === 1 ? "Select 1 more face"
    : checked.length === 2 ? "Ready to merge"
    : `${checked.length} selected (need exactly 2)`;
}

// ── Merge modal state ──────────────────────────────────────────
let _mergeIds   = [null, null];  // [idA, idB]
let _mergeNames = ["", ""];

function mergeFaces() {
  const rows = [...document.querySelectorAll("#face-tbody tr[data-face-id]")];
  const checked = rows.filter(r => r.querySelector(".face-merge-cb")?.checked);
  if (checked.length !== 2) return;
  _mergeIds   = checked.map(r => r.dataset.faceId);
  _mergeNames = _mergeIds.map(id => {
    const inp = document.getElementById(`name-${id}`);
    return inp ? (inp.value.trim() || id.slice(0, 8)) : id.slice(0, 8);
  });

  // Populate face A
  el("merge-name-a").textContent = _mergeNames[0];
  el("merge-fid-a").textContent  = _mergeIds[0].slice(0, 12) + "…";
  const imgA = el("merge-img-a");
  imgA.src = authUrl(`/api/faces/${encodeURIComponent(_mergeIds[0])}/thumb`);
  imgA.onerror = () => { imgA.style.display = "none"; };

  // Populate face B
  el("merge-name-b").textContent = _mergeNames[1];
  el("merge-fid-b").textContent  = _mergeIds[1].slice(0, 12) + "…";
  const imgB = el("merge-img-b");
  imgB.src = authUrl(`/api/faces/${encodeURIComponent(_mergeIds[1])}/thumb`);
  imgB.onerror = () => { imgB.style.display = "none"; };

  // Default: A = keep, B = absorb
  el("merge-keep-a").checked = true;
  updateMergeRoles();

  el("merge-modal").classList.add("active");
}

function updateMergeRoles() {
  // Determine which radio is checked: value "a" means face A is keep
  const keepVal = document.querySelector("input[name='merge-keep']:checked")?.value;
  const aIsKeep = keepVal === "a";
  const cardA = el("merge-card-a");
  const cardB = el("merge-card-b");
  cardA.classList.toggle("is-keep",   aIsKeep);
  cardA.classList.toggle("is-absorb", !aIsKeep);
  cardB.classList.toggle("is-keep",   !aIsKeep);
  cardB.classList.toggle("is-absorb", aIsKeep);

  const keepName   = aIsKeep ? _mergeNames[0] : _mergeNames[1];
  const absorbName = aIsKeep ? _mergeNames[1] : _mergeNames[0];
  el("merge-summary").innerHTML =
    `✅ <strong>${esc(keepName)}</strong> will be kept as the parent identity.<br>` +
    `🗑 <strong>${esc(absorbName)}</strong>'s embeddings will be merged in, then deleted.`;
}

function swapMergeRoles() {
  const keepVal = document.querySelector("input[name='merge-keep']:checked")?.value;
  // Toggle to the other face
  if (keepVal === "a") {
    el("merge-keep-b").checked = true;
  } else {
    el("merge-keep-a").checked = true;
  }
  updateMergeRoles();
}

function closeMergeModal() {
  el("merge-modal").classList.remove("active");
}

async function confirmMerge() {
  const keepVal = document.querySelector("input[name='merge-keep']:checked")?.value;
  const aIsKeep = keepVal === "a";
  const keepId   = aIsKeep ? _mergeIds[0] : _mergeIds[1];
  const absorbId = aIsKeep ? _mergeIds[1] : _mergeIds[0];
  const confirmBtn = el("merge-confirm-btn");
  confirmBtn.disabled = true;
  confirmBtn.textContent = "Merging…";
  try {
    const r = await fetch("/api/faces/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_id: keepId, absorb_id: absorbId }),
    });
    if (r.ok) {
      closeMergeModal();
      loadFaces();
    } else {
      const d = await r.json().catch(() => ({}));
      alert("Merge failed: " + (d.detail || r.status));
    }
  } catch (e) {
    alert("Merge error: " + e);
  } finally {
    confirmBtn.disabled = false;
    confirmBtn.textContent = "⛓ Merge";
  }
}

async function saveName(faceId) {
  const input = document.getElementById(`name-${faceId}`);
  if (!input) return;
  const name = input.value.trim();
  if (!name) return;
  try {
    const r = await fetch(`/api/faces/${faceId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (r.ok) {
      input.style.borderColor = "var(--green)";
      setTimeout(() => input.style.borderColor = "", 1500);
    } else {
      input.style.borderColor = "var(--red)";
    }
  } catch (e) {
    input.style.borderColor = "var(--red)";
  }
}

async function deleteFace(faceId) {
  if (!confirm("Delete this face from the registry?")) return;
  try {
    const r = await fetch(`/api/faces/${faceId}`, { method: "DELETE" });
    if (r.ok) loadFaces();
  } catch (e) { /* ignore */ }
}

async function captureTrainingImage(faceId) {
  const btn = document.getElementById(`train-btn-${faceId}`);
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Capturing…"; }
  try {
    const r = await fetch(`/api/faces/${faceId}/train`, { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      if (btn) { btn.textContent = "✅ Done"; btn.style.background = "var(--green)"; }
      // Reload faces so updated thumbnail appears
      setTimeout(() => loadFaces(), 800);
    } else {
      const msg = d.detail || "failed";
      if (btn) { btn.textContent = "❌ " + msg; btn.style.background = "var(--red)"; }
      setTimeout(() => {
        if (btn) { btn.disabled = false; btn.textContent = "📷 Train"; btn.style.background = ""; }
      }, 2500);
    }
  } catch (e) {
    if (btn) { btn.textContent = "❌ Error"; btn.style.background = "var(--red)"; }
    setTimeout(() => {
      if (btn) { btn.disabled = false; btn.textContent = "📷 Train"; btn.style.background = ""; }
    }, 2500);
  }
}

async function loadQuietHours() {
  try {
    const r = await fetch("/api/settings/quiet-hours");
    if (!r.ok) return;
    const d = await r.json();
    el("qh-enabled").checked = !!d.enabled;
    el("qh-start").value = d.start || "21:00";
    el("qh-end").value = d.end || "06:00";
  } catch (e) { /* ignore */ }
}

// ── Fan curve table ─────────────────────────────────────────────────────────

function updateBlendLabels() {
  const slider = el("blend-slider");
  if (!slider) return;
  const caseVal = parseInt(slider.value, 10);
  const cpuVal  = 100 - caseVal;
  const caseEl  = el("blend-case-pct");
  const cpuEl   = el("blend-cpu-pct");
  if (caseEl) caseEl.textContent = caseVal + "%";
  if (cpuEl)  cpuEl.textContent  = cpuVal  + "%";
}

function renderFanCurveTable(points) {
  const tbody = el("fan-curve-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  points.forEach((pt, idx) => {
    const isFloor   = idx === 0;
    const isCeiling = idx === points.length - 1;
    const isLocked  = isFloor || isCeiling;
    const label     = isFloor ? "🔒 Floor" : (isCeiling ? "🔒 Ceiling" : "");

    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid var(--border)";

    const tdTemp = document.createElement("td");
    tdTemp.style.padding = "4px 8px";
    const tempInput = document.createElement("input");
    tempInput.type  = "number";
    tempInput.value = pt.temp_c;
    tempInput.min   = -40;
    tempInput.max   = 150;
    tempInput.step  = 0.5;
    tempInput.style.width = "80px";
    tempInput.dataset.field = "temp_c";
    if (isLocked) tempInput.style.fontWeight = "bold";
    tdTemp.appendChild(tempInput);
    if (label) {
      const badge = document.createElement("small");
      badge.textContent = " " + label;
      badge.style.color = "var(--text-muted)";
      tdTemp.appendChild(badge);
    }

    const tdDuty = document.createElement("td");
    tdDuty.style.padding = "4px 8px";
    const dutyInput = document.createElement("input");
    dutyInput.type  = "number";
    dutyInput.value = Math.round(pt.duty);
    dutyInput.min   = 0;
    dutyInput.max   = 100;
    dutyInput.step  = 1;
    dutyInput.style.width = "70px";
    dutyInput.dataset.field = "duty";
    if (isLocked) dutyInput.style.fontWeight = "bold";
    tdDuty.appendChild(dutyInput);

    const tdDel = document.createElement("td");
    tdDel.style.padding = "4px";
    if (!isLocked) {
      const btn = document.createElement("button");
      btn.className   = "btn btn-danger btn-sm";
      btn.textContent = "✕";
      btn.title       = "Remove this point";
      btn.onclick     = () => deleteFanCurveRow(btn);
      tdDel.appendChild(btn);
    }

    tr.appendChild(tdTemp);
    tr.appendChild(tdDuty);
    tr.appendChild(tdDel);
    tbody.appendChild(tr);
  });
}

function _readFanCurveTable() {
  const tbody = el("fan-curve-tbody");
  if (!tbody) return [];
  const rows = [...tbody.querySelectorAll("tr")];
  return rows.map((tr) => {
    const inputs = tr.querySelectorAll("input");
    return {
      temp_c: parseFloat(inputs[0].value),
      duty:   parseFloat(inputs[1].value),
    };
  });
}

function addFanCurveRow() {
  const tbody = el("fan-curve-tbody");
  if (!tbody) return;
  const current = _readFanCurveTable();
  if (current.length < 2) return;
  const last   = current[current.length - 1];
  const prev   = current[current.length - 2];
  const newTemp = parseFloat(((prev.temp_c + last.temp_c) / 2).toFixed(1));
  const newDuty = Math.round((prev.duty + last.duty) / 2);
  const newPoints = [
    ...current.slice(0, current.length - 1),
    { temp_c: newTemp, duty: newDuty },
    last,
  ];
  renderFanCurveTable(newPoints);
}

function deleteFanCurveRow(btn) {
  const tr     = btn.closest("tr");
  const tbody  = el("fan-curve-tbody");
  const rows   = [...tbody.querySelectorAll("tr")];
  const idx    = rows.indexOf(tr);
  if (idx <= 0 || idx >= rows.length - 1) return; // never delete floor/ceiling
  const current = _readFanCurveTable();
  current.splice(idx, 1);
  renderFanCurveTable(current);
}

async function loadFanControlPoints() {
  const status = el("fan-curve-status");
  try {
    const [rPts, rBlend] = await Promise.all([
      fetch("/api/settings/fan/control-points"),
      fetch("/api/settings/fan/temp-blend"),
    ]);
    const dPts   = await rPts.json().catch(() => ({}));
    const dBlend = await rBlend.json().catch(() => ({}));
    if (!rPts.ok) throw new Error(dPts.detail || "Failed to load fan control points");
    renderFanCurveTable(dPts.control_points || []);
    const slider = el("blend-slider");
    if (slider && dBlend.case_weight !== undefined) {
      slider.value = Math.round(dBlend.case_weight * 100);
      updateBlendLabels();
    }
    if (status) status.textContent = "";
  } catch (e) {
    if (status) {
      status.className = "qh-status error";
      status.textContent = e.message || "Network error";
    }
  }
}

async function saveFanControlPoints() {
  const status = el("fan-curve-status");
  try {
    const points = _readFanCurveTable();
    if (points.length < 2) throw new Error("At least two control points are required.");
    for (const p of points) {
      if (!Number.isFinite(p.temp_c) || !Number.isFinite(p.duty)) {
        throw new Error("All temp/duty fields must be valid numbers.");
      }
    }
    const slider    = el("blend-slider");
    const casePct   = slider ? parseInt(slider.value, 10) : 20;
    const blendBody = { case_weight: casePct / 100, cpu_weight: (100 - casePct) / 100 };

    const [rPts, rBlend] = await Promise.all([
      fetch("/api/settings/fan/control-points", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ points }),
      }),
      fetch("/api/settings/fan/temp-blend", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(blendBody),
      }),
    ]);
    const dPts = await rPts.json().catch(() => ({}));
    if (!rPts.ok) throw new Error(dPts.detail || "Failed to save fan control points");
    renderFanCurveTable(dPts.control_points || points);
    if (status) {
      status.className = "qh-status";
      status.textContent = dPts.runtime_applied ? "Saved ✓" : "Saved to config (thermal offline)";
    }
  } catch (e) {
    if (status) {
      status.className = "qh-status error";
      status.textContent = e.message || "Network error";
    }
  }
  setTimeout(() => {
    if (status) status.textContent = "";
  }, 3500);
}

async function saveQuietHours() {
  const enabled = el("qh-enabled").checked;
  const start = el("qh-start").value;
  const end = el("qh-end").value;
  const status = el("qh-status");
  try {
    const r = await fetch("/api/settings/quiet-hours", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, start, end }),
    });
    if (r.ok) {
      status.className = "qh-status";
      status.textContent = "Saved ✓";
    } else {
      const d = await r.json().catch(() => ({}));
      status.className = "qh-status error";
      status.textContent = d.detail || "Error saving";
    }
  } catch (e) {
    status.className = "qh-status error";
    status.textContent = "Network error";
  }
  setTimeout(() => { status.textContent = ""; }, 3000);
}

async function deleteAllFaces() {
  if (!confirm("Delete ALL known faces from the registry? This cannot be undone.")) return;
  try {
    const r = await fetch("/api/faces", { method: "DELETE" });
    if (r.ok) {
      const data = await r.json();
      loadFaces();
      const log = el("event-log");
      const row = document.createElement("div");
      row.className = "event-row";
      row.innerHTML = `<span class="event-ts">${new Date().toLocaleTimeString()}</span><span class="event-topic" style="color:var(--red)">face.registry_cleared</span><span class="event-body">Deleted ${data.deleted} face(s)</span>`;
      log.prepend(row);
    }
  } catch (e) { /* ignore */ }
}

async function deleteGuestFaces() {
  if (!confirm("Remove all unnamed Guest entries? Named identities will be kept.")) return;
  try {
    const r = await fetch("/api/faces/guests", { method: "DELETE" });
    if (r.ok) {
      const data = await r.json();
      loadFaces();
      const log = el("event-log");
      const row = document.createElement("div");
      row.className = "event-row";
      row.innerHTML = `<span class="event-ts">${new Date().toLocaleTimeString()}</span><span class="event-topic" style="color:var(--yellow,#f5c542)">face.guests_cleared</span><span class="event-body">Removed ${data.deleted} guest(s)</span>`;
      log.prepend(row);
    }
  } catch (e) { /* ignore */ }
}

// ── Room ──────────────────────────────────────────────────────────

function editRoom() {
  const nameEl = el("stat-room");
  const editWrap = el("room-edit-wrap");
  const editBtn = el("room-edit-btn");
  const input = el("room-input");
  const current = nameEl.textContent;
  input.value = current === "Unknown" ? "" : current;
  editWrap.style.display = "inline-flex";
  editBtn.style.display = "none";
  input.focus();
  input.onkeydown = (e) => { if (e.key === "Enter") saveRoom(); if (e.key === "Escape") cancelEditRoom(); };
}

function cancelEditRoom() {
  el("room-edit-wrap").style.display = "none";
  el("room-edit-btn").style.display = "";
  el("room-status").textContent = "";
}

async function saveRoom() {
  const name = (el("room-input").value || "").trim();
  if (!name) { el("room-status").textContent = "Enter a room name"; return; }
  const status = el("room-status");
  try {
    const r = await fetch("/api/room", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const d = await r.json();
    if (r.ok) {
      el("stat-room").textContent = name;
      cancelEditRoom();
      status.style.color = "var(--green)";
      status.textContent = "Saved ✓";
    } else {
      status.style.color = "var(--red)";
      status.textContent = d.detail || "Error";
    }
  } catch (e) {
    el("room-status").style.color = "var(--red)";
    el("room-status").textContent = "Network error";
  }
  setTimeout(() => { el("room-status").textContent = ""; }, 3000);
}

// ── Servo enable/disable ──────────────────────────────────────────

async function loadServoEnabled() {
  try {
    const r = await fetch("/api/settings/servo");
    if (!r.ok) return;
    const d = await r.json();
    el("servo-enabled").checked = !!d.enabled;
  } catch (e) { /* ignore */ }
}

async function saveServoEnabled(enabled) {
  try {
    await fetch("/api/settings/servo", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  } catch (e) { /* ignore */ }
}

async function loadServoLimits() {
  try {
    const d = await fetch("/api/settings/servo/limits").then(r => r.json());
    el("servo-travel-min").value = d.min_deg ?? 135;
    el("servo-travel-max").value = d.max_deg ?? 215;
    // Update pan slider range to match limits
    const slider = el("pan-slider");
    if (slider) {
      slider.min = d.min_deg ?? 135;
      slider.max = d.max_deg ?? 215;
      if (parseFloat(slider.value) < d.min_deg) slider.value = d.min_deg;
      if (parseFloat(slider.value) > d.max_deg) slider.value = d.max_deg;
    }
  } catch (e) { /* ignore */ }
}

async function saveServoLimits() {
  const min_deg = parseFloat(el("servo-travel-min").value);
  const max_deg = parseFloat(el("servo-travel-max").value);
  const st = el("servo-limits-status");
  if (min_deg >= max_deg || min_deg < 1 || max_deg > 360) {
    st.textContent = "Invalid range";
    st.style.color = "var(--red)";
    setTimeout(() => { st.textContent = ""; }, 3000);
    return;
  }
  try {
    await fetch("/api/settings/servo/limits", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min_deg, max_deg }),
    });
    st.textContent = "Saved ✓";
    st.style.color = "var(--green)";
    // Update pan slider range
    const slider = el("pan-slider");
    if (slider) { slider.min = min_deg; slider.max = max_deg; }
  } catch (e) {
    st.textContent = "Error";
    st.style.color = "var(--red)";
  }
  setTimeout(() => { st.textContent = ""; }, 3000);
}

async function loadFaceTrackingEnabled() {
  try {
    const r = await fetch("/api/settings/face-tracking");
    if (!r.ok) return;
    const d = await r.json();
    el("face-tracking-enabled").checked = !!d.enabled;
  } catch (e) { /* ignore */ }
}

async function saveFaceTrackingEnabled(enabled) {
  try {
    await fetch("/api/settings/face-tracking", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  } catch (e) { /* ignore */ }
}

async function loadRandomMotionEnabled() {
  try {
    const r = await fetch("/api/settings/random-motion");
    if (!r.ok) return;
    const d = await r.json();
    el("random-motion-enabled").checked = !!d.enabled;
  } catch (e) { /* ignore */ }
}

async function saveRandomMotionEnabled(enabled) {
  try {
    await fetch("/api/settings/random-motion", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  } catch (e) { /* ignore */ }
}

async function loadObjectDetectionEnabled() {
  try {
    const r = await fetch("/api/settings/object-detection");
    if (!r.ok) return;
    const d = await r.json();
    el("object-detection-enabled").checked = !!d.enabled;
  } catch (e) { /* ignore */ }
}

async function saveObjectDetectionEnabled(enabled) {
  try {
    await fetch("/api/settings/object-detection", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  } catch (e) { /* ignore */ }
}

async function loadPrivacySettings() {
  const st = el("privacy-status");
  try {
    const d = await fetch("/api/settings/privacy").then(r => r.json());
    _setVal("privacy-enabled", !!d.enabled);
    _setVal("privacy-rate-hz", d.rate_hz ?? 1.0);
    _setVal("privacy-threshold", d.threshold ?? 0.6);
    _setVal("privacy-look-away-deg", d.look_away_angle_deg ?? 45.0);
    _setVal("privacy-cooldown-s", d.cooldown_s ?? 10.0);
    _setVal("privacy-clear-frames", d.clear_frames ?? 3);
    _setVal("privacy-announce", d.announce !== false);
    _setVal("privacy-announce-text", d.announce_text ?? "I'll give you some privacy.");
    _setVal("privacy-resume-text", d.resume_text ?? "");
    if (st) st.textContent = "";
  } catch (e) {
    if (st) {
      st.textContent = "Load failed";
      st.style.color = "var(--red)";
    }
  }
}

async function savePrivacySettings() {
  const st = el("privacy-status");
  const body = {
    enabled: !!el("privacy-enabled").checked,
    rate_hz: parseFloat(el("privacy-rate-hz").value),
    threshold: parseFloat(el("privacy-threshold").value),
    look_away_angle_deg: parseFloat(el("privacy-look-away-deg").value),
    cooldown_s: parseFloat(el("privacy-cooldown-s").value),
    clear_frames: parseInt(el("privacy-clear-frames").value, 10),
    announce: !!el("privacy-announce").checked,
    announce_text: el("privacy-announce-text").value,
    resume_text: el("privacy-resume-text").value,
  };
  try {
    const r = await fetch("/api/settings/privacy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Save failed");
    if (st) {
      st.textContent = "Saved ✓";
      st.style.color = "var(--green)";
    }
  } catch (e) {
    if (st) {
      st.textContent = e.message || "Save failed";
      st.style.color = "var(--red)";
    }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 4000);
}

// ── Greeting settings ─────────────────────────────────────────────

async function loadGreetingSettings() {
  try {
    const d = await fetch("/api/settings/greeting").then(r => r.json());
    el("greeting-cooldown").value    = d.cooldown_min ?? 30;
    el("greeting-jitter").value      = d.jitter_pct ?? 25;
    el("greeting-min-absence").value = d.min_absence_s ?? 30;
    el("greeting-confidence").value  = d.confidence_threshold ?? 0.5;
  } catch (e) { /* ignore */ }
}

async function saveGreetingSettings() {
  const cooldown_min         = parseFloat(el("greeting-cooldown").value);
  const jitter_pct           = parseFloat(el("greeting-jitter").value);
  const min_absence_s        = parseFloat(el("greeting-min-absence").value);
  const confidence_threshold = parseFloat(el("greeting-confidence").value);
  const st = el("greeting-status");
  try {
    await fetch("/api/settings/greeting", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cooldown_min, jitter_pct, min_absence_s, confidence_threshold }),
    });
    st.textContent = "Saved ✓";
    st.style.color = "var(--green)";
  } catch (e) {
    st.textContent = "Error";
    st.style.color = "var(--red)";
  }
  setTimeout(() => { st.textContent = ""; }, 3000);
}


// ── Audio backend settings ─────────────────────────────────────────────────

function onAudioBackendChange() {
  const backend = el("audio-backend-select").value;
  document.querySelectorAll(".audio-backend-section").forEach(sec => {
    sec.style.display = sec.id === `audio-section-${backend}` ? "" : "none";
  });
}

async function loadAudioSettings() {
  const st = el("audio-status");
  try {
    const d = await fetch("/api/settings/audio").then(r => r.json());

    // Backend selector
    const sel = el("audio-backend-select");
    if (sel) sel.value = d.backend ?? "default";
    onAudioBackendChange();

    // Default backend fields
    const def = d.default ?? {};
    _setVal("audio-default-input-device", def.input_device_name ?? "");
    _setVal("audio-default-input-rate",   def.input_sample_rate ?? 44100);
    _setVal("audio-default-output-alsa",  def.output_alsa_device ?? "pulse");
    _setVal("audio-default-output-rate",  def.output_sample_rate ?? 44100);
    _setVal("audio-default-loudness",     def.loudness_boost ?? 2.0);
    _setVal("audio-default-eq",           def.eq_preset ?? "flat");

    // ReSpeaker Flex fields
    const rs = d.respeaker_flex ?? {};
    _setVal("audio-rs-input-device", rs.input_device_name ?? "ReSpeaker");
    _setVal("audio-rs-input-rate",   rs.input_sample_rate ?? 16000);
    _setVal("audio-rs-raw-ch",       rs.input_raw_channels ?? 6);
    _setVal("audio-rs-proc-enabled", rs.input_processing_enabled !== false);
    _setVal("audio-rs-proc-ch",      rs.input_processed_channel ?? 0);
    _setVal("audio-rs-raw-mic-ch",   rs.input_raw_mic_channel ?? 1);
    _setVal("audio-rs-output-alsa",  rs.output_alsa_device ?? "pulse");
    _setVal("audio-rs-output-rate",  rs.output_sample_rate ?? 44100);
    _setVal("audio-rs-loudness",     rs.loudness_boost ?? 2.0);
    _setVal("audio-rs-eq",           rs.eq_preset ?? "flat");
    const ledEl = el("audio-rs-led");
    if (ledEl) ledEl.checked = rs.led_enabled !== false;

    // Device list
    const listEl = el("audio-devices-list");
    if (listEl && Array.isArray(d.available_input_devices)) {
      if (d.available_input_devices.length === 0) {
        listEl.innerHTML = "<li>No input devices found</li>";
      } else {
        listEl.innerHTML = d.available_input_devices
          .map(dev => `<li>[${dev.index}] ${dev.name} &mdash; ${dev.channels} ch</li>`)
          .join("");
      }
    }

    if (st) st.textContent = "";
  } catch (e) {
    if (st) { st.className = "qh-status error"; st.textContent = e.message || "Load failed"; }
  }
}

async function loadVoiceSettings() {
  const st = el("voice-status");
  try {
    const d = await fetch("/api/settings/voice").then(r => r.json());
    _setVal("voice-enabled", d.enabled === true);
    _setVal("voice-stt-backend", d.stt_backend ?? "faster_whisper");
    _setVal("voice-stt-command", d.stt_command ?? "");
    _setVal("voice-stt-language", d.stt_language ?? "en");
    _setVal("voice-stt-timeout", d.stt_timeout_s ?? 20.0);
    _setVal("voice-wake-threshold", d.wake_threshold_dbfs ?? -38.0);
    _setVal("voice-wake-cooldown", d.wake_cooldown_s ?? 1.5);
    if (st) st.textContent = "";
  } catch (e) {
    if (st) { st.className = "qh-status error"; st.textContent = e.message || "Load failed"; }
  }
}

async function saveVoiceSettings() {
  const st = el("voice-status");
  const body = {
    enabled: el("voice-enabled").checked,
    stt_backend: el("voice-stt-backend").value,
    stt_command: el("voice-stt-command").value,
    stt_language: el("voice-stt-language").value || "en",
    stt_timeout_s: parseFloat(el("voice-stt-timeout").value),
    wake_threshold_dbfs: parseFloat(el("voice-wake-threshold").value),
    wake_cooldown_s: parseFloat(el("voice-wake-cooldown").value),
  };
  try {
    const r = await fetch("/api/settings/voice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Save failed");
    if (st) {
      st.className = "qh-status";
      st.textContent = "Saved ✓";
    }
  } catch (e) {
    if (st) { st.className = "qh-status error"; st.textContent = e.message || "Error"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 5000);
}

function _formatXvfValues(values) {
  if (!Array.isArray(values) || values.length === 0) return "";
  return values.map(v => typeof v === "number" ? `${v}` : String(v)).join(", ");
}

function _setXvfStatus(message, isError = false) {
  const st = el("xvf-status");
  if (!st) return;
  st.className = isError ? "qh-status error" : "qh-status";
  st.textContent = message;
}

function _renderXvfSnapshot(snapshot) {
  const meta = el("xvf-meta");
  const roBody = el("xvf-readonly-rows");
  const tunableBody = el("xvf-tunable-rows");
  if (!meta || !roBody || !tunableBody) return;

  if (!snapshot?.available) {
    meta.textContent = "XVF3800 controller not detected on this system.";
    roBody.innerHTML = '<tr><td colspan="3">Unavailable</td></tr>';
    tunableBody.innerHTML = '<tr><td colspan="4">Unavailable</td></tr>';
    return;
  }

  meta.textContent = `Connected: ${snapshot.connected ? "yes" : "no"}`
    + (snapshot.usb?.vendor_id ? ` | USB ${snapshot.usb.vendor_id}:${snapshot.usb.product_id}` : "")
    + (snapshot.usb?.bus !== null && snapshot.usb?.address !== null ? ` | bus ${snapshot.usb.bus} addr ${snapshot.usb.address}` : "");

  const readonly = Array.isArray(snapshot.readonly) ? snapshot.readonly : [];
  roBody.innerHTML = readonly.length
    ? readonly.map(item => `<tr><td><code>${item.command}</code></td><td>${item.label}</td><td>${_formatXvfValues(item.values)}</td></tr>`).join("")
    : '<tr><td colspan="3">No read-only values reported</td></tr>';

  const tunables = Array.isArray(snapshot.tunables) ? snapshot.tunables : [];
  tunableBody.innerHTML = tunables.length
    ? tunables.map(item => {
        const current = Array.isArray(item.values) && item.values.length ? item.values[0] : "";
        let input = "";
        if (item.dtype === "bool") {
          input = `<input type="checkbox" data-xvf-command="${item.command}" data-xvf-index="0" ${current ? "checked" : ""}/>`;
        } else if (item.dtype === "int" || item.dtype === "float") {
          const step = item.dtype === "float" ? "0.1" : "1";
          const min = item.min !== null && item.min !== undefined ? `min="${item.min}"` : "";
          const max = item.max !== null && item.max !== undefined ? `max="${item.max}"` : "";
          input = `<input type="number" step="${step}" ${min} ${max} value="${current}" data-xvf-command="${item.command}" data-xvf-index="0"/>`;
        } else {
          input = `<input type="text" value="${current}" data-xvf-command="${item.command}" data-xvf-index="0"/>`;
        }
        return `<tr>
          <td><code>${item.command}</code></td>
          <td>${item.label}</td>
          <td>${_formatXvfValues(item.values)}</td>
          <td>${input}</td>
        </tr>`;
      }).join("")
    : '<tr><td colspan="4">No tunables reported</td></tr>';
}

async function loadXvfState() {
  try {
    const r = await fetch("/api/audio/xvf");
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Load failed");
    _renderXvfSnapshot(d);
    _setXvfStatus("");
  } catch (e) {
    _setXvfStatus(e.message || "Load failed", true);
  }
}

function _collectXvfWrites() {
  const rows = new Map();
  document.querySelectorAll("[data-xvf-command]").forEach(input => {
    const command = input.dataset.xvfCommand;
    const index = parseInt(input.dataset.xvfIndex || "0", 10);
    let value;
    if (input.type === "checkbox") {
      value = input.checked;
    } else if (input.type === "number") {
      value = input.step && input.step.includes(".") ? parseFloat(input.value) : parseInt(input.value, 10);
      if (Number.isNaN(value)) value = 0;
    } else {
      value = input.value;
    }
    if (!rows.has(command)) rows.set(command, []);
    rows.get(command)[index] = value;
  });
  return Array.from(rows.entries()).map(([command, values]) => ({ command, values }));
}

async function saveXvfTunables() {
  try {
    const r = await fetch("/api/audio/xvf", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ writes: _collectXvfWrites(), save: false }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Save failed");
    _renderXvfSnapshot(d);
    _setXvfStatus("Applied ✓");
  } catch (e) {
    _setXvfStatus(e.message || "Save failed", true);
  }
}

async function saveXvfConfiguration() {
  try {
    const r = await fetch("/api/audio/xvf/save", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Save failed");
    _setXvfStatus(d.saved ? "Saved to flash ✓" : "Save requested");
  } catch (e) {
    _setXvfStatus(e.message || "Save failed", true);
  }
}

async function loadAudioInputGain() {
  const slider = el("audio-input-gain-slider");
  const label = el("audio-input-gain-label");
  const st = el("audio-input-gain-status");
  if (!slider || !label) return;
  try {
    const d = await fetch("/api/audio/input-gain").then(r => r.json());
    if (d && d.available && d.level !== null && d.level !== undefined) {
      slider.value = d.level;
      label.textContent = `${d.level}%`;
      if (st) st.textContent = "";
    } else if (st) {
      st.textContent = "Unavailable";
      st.style.color = "var(--text-muted)";
    }
  } catch (_e) {
    if (st) {
      st.textContent = "Error";
      st.style.color = "var(--red)";
    }
  }
}

async function loadAudioVoiceGain() {
  const slider = el("audio-voice-gain-slider");
  const label = el("audio-voice-gain-label");
  const st = el("audio-voice-gain-status");
  if (!slider || !label) return;
  try {
    const d = await fetch("/api/audio/voice-gain").then(r => r.json());
    if (d && d.level !== null && d.level !== undefined) {
      slider.value = d.level;
      label.textContent = `${d.level}%`;
      if (st) st.textContent = "";
    }
  } catch (_e) {
    if (st) {
      st.textContent = "Error";
      st.style.color = "var(--red)";
    }
  }
}

function _setVal(id, val) {
  const e = el(id);
  if (!e) return;
  if (e.type === "checkbox") e.checked = !!val;
  else if (e.tagName === "SELECT") e.value = String(val);
  else e.value = val;
}

async function saveAudioSettings() {
  const st = el("audio-status");
  const backend = el("audio-backend-select").value;
  const body = { backend };

  if (backend === "default") {
    body.default = {
      input_device_name: el("audio-default-input-device").value,
      input_sample_rate: parseInt(el("audio-default-input-rate").value, 10),
      output_alsa_device: el("audio-default-output-alsa").value,
      output_sample_rate: parseInt(el("audio-default-output-rate").value, 10),
      loudness_boost: parseFloat(el("audio-default-loudness").value),
      eq_preset: el("audio-default-eq").value,
    };
  } else if (backend === "respeaker_flex") {
    body.respeaker_flex = {
      input_device_name: el("audio-rs-input-device").value,
      input_sample_rate: parseInt(el("audio-rs-input-rate").value, 10),
      input_raw_channels: parseInt(el("audio-rs-raw-ch").value, 10),
      input_processing_enabled: el("audio-rs-proc-enabled").checked,
      input_processed_channel: parseInt(el("audio-rs-proc-ch").value, 10),
      input_raw_mic_channel: parseInt(el("audio-rs-raw-mic-ch").value, 10),
      output_alsa_device: el("audio-rs-output-alsa").value,
      output_sample_rate: parseInt(el("audio-rs-output-rate").value, 10),
      loudness_boost: parseFloat(el("audio-rs-loudness").value),
      eq_preset: el("audio-rs-eq").value,
      led_enabled: el("audio-rs-led").checked,
    };
  }

  try {
    const r = await fetch("/api/settings/audio", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Save failed");
    if (st) {
      st.className = "qh-status";
      st.textContent = "Saved ✓ — restart required to apply";
    }
  } catch (e) {
    if (st) { st.className = "qh-status error"; st.textContent = e.message || "Error"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 5000);
}


async function doSay() {
  const text = el("say-input").value.trim();
  if (!text) return;
  await fetch("/api/say", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  el("say-input").value = "";
}

async function doRecordClip() {
  const status = el("record-status");
  const secEl = el("record-seconds");
  const progress = el("record-progress");
  const remaining = el("record-remaining");
  const seconds = Math.max(1, Math.min(120, parseInt(secEl.value || "5", 10)));
  let timer = null;
  const start = Date.now();
  if (progress) {
    progress.max = seconds;
    progress.value = 0;
    progress.style.display = "";
  }
  if (remaining) {
    remaining.textContent = `${seconds.toFixed(1)}s left`;
  }
  timer = setInterval(() => {
    const elapsed = (Date.now() - start) / 1000;
    const left = Math.max(0, seconds - elapsed);
    if (progress) progress.value = Math.min(seconds, elapsed);
    if (remaining) remaining.textContent = `${left.toFixed(1)}s left`;
  }, 100);
  if (status) {
    status.textContent = "Recording...";
    status.style.color = "var(--yellow,#f5c542)";
  }
  try {
    const r = await fetch("/api/audio/record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      throw new Error(d.detail || d.error || `HTTP ${r.status}`);
    }
    if (status) {
      status.textContent = `Saved: ${d.path || "recording"}` +
        (d.rms != null ? ` (rms ${Number(d.rms).toFixed(4)})` : "");
      status.style.color = "var(--green)";
    }
  } catch (e) {
    if (status) {
      status.textContent = `Record failed: ${e.message || e}`;
      status.style.color = "var(--red)";
    }
  } finally {
    if (timer) clearInterval(timer);
    if (progress) {
      progress.value = seconds;
      setTimeout(() => { progress.style.display = "none"; }, 1200);
    }
    if (remaining) {
      remaining.textContent = "";
    }
  }
}

async function doPlaybackClip() {
  const status = el("record-status");
  if (status) {
    status.textContent = "Playing...";
    status.style.color = "var(--yellow,#f5c542)";
  }
  try {
    const r = await fetch("/api/audio/playback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      throw new Error(d.detail || d.error || `HTTP ${r.status}`);
    }
    if (status) {
      status.textContent = `Played: ${d.path || "latest recording"}`;
      status.style.color = "var(--green)";
    }
  } catch (e) {
    if (status) {
      status.textContent = `Playback failed: ${e.message || e}`;
      status.style.color = "var(--red)";
    }
  }
}

async function doPan() {
  const angle = parseFloat(el("pan-slider").value);
  await fetch("/api/pan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ angle }),
  });
}

async function doVersion() {
  await fetch("/api/version", { method: "POST" });
}

async function audioToggleMute() {
  const btn = el("audio-mute-btn");
  const st = el("audio-mute-status");
  let currentlyMuted = false;
  try {
    const state = await fetch("/api/audio/mute").then(r => r.json());
    currentlyMuted = !!state.muted;
  } catch (_e) {}
  const targetMuted = !currentlyMuted;
  try {
    await fetch("/api/audio/mute", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ muted: targetMuted }),
    });
    if (btn) btn.textContent = targetMuted ? "🔊 Unmute" : "🔇 Mute";
    if (st) {
      st.textContent = targetMuted ? "Muted" : "Unmuted";
      st.style.color = "var(--green)";
    }
  } catch (_e) {
    if (st) {
      st.textContent = "Error";
      st.style.color = "var(--red)";
    }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 1500);
}

async function repeatLastSpoken() {
  const st = el("repeat-spoken-status");
  try {
    const r = await fetch("/api/audio/repeat", { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    if (st) {
      st.textContent = "Repeating…";
      st.style.color = "var(--green)";
    }
  } catch (_e) {
    if (st) {
      st.textContent = "Nothing to repeat";
      st.style.color = "var(--text-muted)";
    }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 2000);
}

async function audioPlaySpectrumTest() {
  const st = el("audio-spectrum-test-status");
  if (st) {
    st.textContent = "Playing…";
    st.style.color = "var(--green)";
  }
  try {
    const r = await fetch("/api/audio/spectrum-test", { method: "POST" });
    const d = await r.json();
    if (!r.ok) {
      const msg = (d && d.detail) ? String(d.detail) : `HTTP ${r.status}`;
      throw new Error(msg);
    }
    if (st) st.textContent = `Playing ${d.bins} tones`;
  } catch (e) {
    if (st) {
      st.textContent = e?.message || "Error";
      st.style.color = "var(--red)";
    }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 4500);
}

async function loadCamRotation() {
  try {
    const r = await fetch("/api/settings/camera/rotation");
    if (!r.ok) return;
    const d = await r.json();
    const deg = d.rotation_deg ?? 0;
    const slider = el("cam-rotation-slider");
    const display = el("cam-rotation-display");
    if (slider) slider.value = deg;
    if (display) display.textContent = deg + "°";
  } catch (e) { /* ignore */ }
}

function setCamRotationPreset(val) {
  if (val === "") return;
  const slider = el("cam-rotation-slider");
  const display = el("cam-rotation-display");
  if (slider) { slider.value = val; }
  if (display) { display.textContent = val + "°"; }
  const sel = el("cam-rotation-preset");
  if (sel) sel.value = "";
  saveCamRotation();
}

async function saveCamRotation() {
  const slider = el("cam-rotation-slider");
  const st = el("cam-rotation-status");
  if (!slider) return;
  const rotation_deg = parseInt(slider.value, 10);
  try {
    const r = await fetch("/api/settings/camera/rotation", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rotation_deg }),
    });
    if (r.ok) {
      if (st) { st.textContent = "Saved ✓"; st.style.color = "var(--green)"; }
    } else {
      if (st) { st.textContent = "Error " + r.status; st.style.color = "var(--red)"; }
    }
  } catch (e) {
    if (st) { st.textContent = "Error"; st.style.color = "var(--red)"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 3000);
}

async function loadCam2Rotation() {
  try {
    const r = await fetch("/api/settings/camera2/rotation");
    if (!r.ok) return;
    const d = await r.json();
    const deg = d.rotation_deg ?? 0;
    const slider = el("cam2-rotation-slider");
    const display = el("cam2-rotation-display");
    if (slider) slider.value = deg;
    if (display) display.textContent = deg + "°";
  } catch (e) { /* ignore */ }
}

function setCam2RotationPreset(val) {
  if (val === "") return;
  const slider = el("cam2-rotation-slider");
  const display = el("cam2-rotation-display");
  if (slider) { slider.value = val; }
  if (display) { display.textContent = val + "°"; }
  const sel = el("cam2-rotation-preset");
  if (sel) sel.value = "";
  saveCam2Rotation();
}

async function saveCam2Rotation() {
  const slider = el("cam2-rotation-slider");
  const st = el("cam2-rotation-status");
  if (!slider) return;
  const rotation_deg = parseInt(slider.value, 10);
  try {
    const r = await fetch("/api/settings/camera2/rotation", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rotation_deg }),
    });
    if (r.ok) {
      if (st) { st.textContent = "Saved ✓"; st.style.color = "var(--green)"; }
    } else {
      if (st) { st.textContent = "Error " + r.status; st.style.color = "var(--red)"; }
    }
  } catch (e) {
    if (st) { st.textContent = "Error"; st.style.color = "var(--red)"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 3000);
}

async function loadStreamResolution() {
  try {
    const r = await fetch("/api/settings/camera/stream_resolution");
    if (!r.ok) return;
    const d = await r.json();
    const val = `${d.width}x${d.height}`;
    const sel = el("cam-stream-resolution-select");
    if (sel) {
      const opts = Array.from(sel.options).map(o => o.value);
      sel.value = opts.includes(val) ? val : "640x360";
    }
  } catch (e) { /* ignore */ }
}

async function saveStreamResolution(val) {
  const st = el("cam-stream-resolution-status");
  const parts = val.split("x");
  if (parts.length !== 2) return;
  const width = parseInt(parts[0], 10);
  const height = parseInt(parts[1], 10);
  try {
    const r = await fetch("/api/settings/camera/stream_resolution", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ width, height }),
    });
    if (r.ok) {
      if (st) { st.textContent = "Applied ✓"; st.style.color = "var(--green)"; }
    } else {
      if (st) { st.textContent = "Error " + r.status; st.style.color = "var(--red)"; }
    }
  } catch (e) {
    if (st) { st.textContent = "Error"; st.style.color = "var(--red)"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 4000);
}

async function loadCam2Resolutions() {
  try {
    const [cr, sr] = await Promise.all([
      fetch("/api/settings/camera2/resolution").then(r => r.ok ? r.json() : null),
      fetch("/api/settings/camera2/stream_resolution").then(r => r.ok ? r.json() : null),
    ]);
    if (cr) {
      const val = `${cr.width}x${cr.height}`;
      const sel = el("cam2-capture-resolution-select");
      if (sel) {
        const opts = Array.from(sel.options).map(o => o.value);
        sel.value = opts.includes(val) ? val : "1920x1080";
      }
    }
    if (sr) {
      const val = `${sr.width}x${sr.height}`;
      const sel = el("cam2-stream-resolution-select");
      if (sel) {
        const opts = Array.from(sel.options).map(o => o.value);
        sel.value = opts.includes(val) ? val : "640x360";
      }
    }
  } catch (e) { /* ignore */ }
}

async function saveCam2CaptureResolution(val) {
  const st = el("cam2-capture-resolution-status");
  const parts = val.split("x");
  if (parts.length !== 2) return;
  try {
    const r = await fetch("/api/settings/camera2/resolution", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ width: parseInt(parts[0], 10), height: parseInt(parts[1], 10) }),
    });
    if (st) { st.textContent = r.ok ? "Applied ✓" : "Error " + r.status; st.style.color = r.ok ? "var(--green)" : "var(--red)"; }
  } catch (e) {
    if (st) { st.textContent = "Error"; st.style.color = "var(--red)"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 4000);
}

async function saveCam2StreamResolution(val) {
  const st = el("cam2-stream-resolution-status");
  const parts = val.split("x");
  if (parts.length !== 2) return;
  try {
    const r = await fetch("/api/settings/camera2/stream_resolution", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ width: parseInt(parts[0], 10), height: parseInt(parts[1], 10) }),
    });
    if (st) { st.textContent = r.ok ? "Applied ✓" : "Error " + r.status; st.style.color = r.ok ? "var(--green)" : "var(--red)"; }
  } catch (e) {
    if (st) { st.textContent = "Error"; st.style.color = "var(--red)"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 4000);
}

async function doDescribe() {
  const btn = document.querySelector('[onclick="doDescribe()"]');
  const st = el("vision-describe-status");
  if (btn) { btn.disabled = true; btn.textContent = "Describing…"; }
  if (st) {
    st.textContent = "Listening to the current scene...";
    st.style.color = "var(--text-muted)";
  }
  try {
    const r = await fetch("/api/vision/describe", { method: "POST" });
    if (!r.ok) {
      if (st) {
        st.textContent = `Error ${r.status}`;
        st.style.color = "var(--red)";
      }
    } else {
      const data = await r.json();
      if (st) {
        st.textContent = data.description || "Description spoken.";
        st.style.color = "var(--green)";
      }
    }
  } catch (e) {
    if (st) {
      st.textContent = "Error";
      st.style.color = "var(--red)";
    }
  }
  setTimeout(() => {
    if (btn) { btn.disabled = false; btn.textContent = "Describe What I See"; }
    if (st) st.textContent = "";
  }, 3000);
}

function toggleHelp() {
  const panel = el("help-panel");
  const btn   = el("help-toggle-btn");
  if (!panel || !btn) return;
  const visible = panel.style.display !== "none";
  panel.style.display = visible ? "none" : "block";
  btn.textContent = visible ? "Show" : "Hide";
}

// ── Skills panel ─────────────────────────────────────────────────

let _skillsLoaded = false;

async function toggleSkills() {
  const panel = el("skills-panel");
  const btn   = el("skills-toggle-btn");
  if (!panel || !btn) return;
  const visible = panel.style.display !== "none";
  panel.style.display = visible ? "none" : "block";
  btn.textContent = visible ? "Show" : "Hide";
  if (!visible) await loadSkills();  // always reload to show current state
}

async function loadSkills() {
  const tbody = el("skills-tbody");
  if (!tbody) return;
  try {
    const r = await fetch("/api/skills");
    const data = await r.json();
    if (!data.skills || !data.skills.length) {
      tbody.innerHTML = "<tr><td colspan='4'>No skills registered.</td></tr>";
      return;
    }
    tbody.innerHTML = data.skills.map(s => _skillRow(s)).join("");
    _skillsLoaded = true;
  } catch (e) {
    tbody.innerHTML = "<tr><td colspan='4'>Failed to load skills.</td></tr>";
  }
}

function _skillRow(s) {
  const toggleId  = `skill-toggle-${s.name}`;
  const checked   = s.enabled ? "checked" : "";
  const cfgBtn    = s.has_config
    ? `<button class="btn btn-sm" title="Configure ${s.name}" onclick="toggleSkillConfig('${s.name}')">⚙</button>`
    : "";
  const mainRow = `
    <tr id="skill-row-${s.name}">
      <td>
        <label class="toggle-label" title="${s.enabled ? 'Enabled' : 'Disabled'}">
          <input type="checkbox" id="${toggleId}" ${checked}
            onchange="setSkillEnabled('${s.name}', this.checked)">
          <span class="toggle-slider"></span>
        </label>
      </td>
      <td><code>${esc(s.name)}</code></td>
      <td>${esc(s.example)}</td>
      <td>${cfgBtn}</td>
    </tr>`;
  const cfgRow = s.has_config
    ? `<tr id="skill-cfg-${s.name}" style="display:none">
         <td colspan="4" style="padding:8px 12px;background:var(--bg2,#1e1e2e)">
           ${_skillConfigForm(s)}
         </td>
       </tr>`
    : "";
  return mainRow + cfgRow;
}

function _skillConfigForm(s) {
  if (!s.config_schema || !s.config_schema.length) return "";
  const fields = s.config_schema.map(f => {
    const val = s.config_values ? (s.config_values[f.name] ?? f.default ?? "") : (f.default ?? "");
    let input = "";
    if (f.type === "bool") {
      input = `<input type="checkbox" id="scf-${s.name}-${f.name}" ${val ? "checked" : ""}>`;
    } else if (f.type === "select") {
      const opts = (f.options || []).map(o =>
        `<option value="${esc(o)}" ${o === val ? "selected" : ""}>${esc(o)}</option>`
      ).join("");
      input = `<select id="scf-${s.name}-${f.name}" class="text-input" style="width:auto">${opts}</select>`;
    } else if (f.type === "display") {
      input = `<span style="font-style:italic;color:var(--text-dim)">${esc(String(val))}</span>`;
    } else {
      const secret = f.secret ? 'type="password"' : 'type="text"';
      const numAttr = (f.type === "int" || f.type === "float")
        ? `type="number" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""} ${f.type === "int" ? 'step="1"' : 'step="any"'}`
        : secret;
      input = `<input ${numAttr} id="scf-${s.name}-${f.name}" value="${esc(String(val))}" class="text-input" style="width:180px">`;
    }
    return `
      <div style="display:flex;align-items:center;gap:8px;margin:4px 0">
        <label style="min-width:160px;font-size:13px" title="${esc(f.description)}">${esc(f.label)}</label>
        ${input}
      </div>`;
  }).join("");
  const saveableFields = s.config_schema.filter(f => f.type !== "display");
  const saveBtn = saveableFields.length
    ? `<button class="btn btn-primary btn-sm" style="margin-top:6px"
         onclick="saveSkillConfig('${s.name}')">Save</button>`
    : "";
  return `<div>${fields}${saveBtn}</div>`;
}

async function setSkillEnabled(name, enabled) {
  try {
    await fetch(`/api/skills/${name}/enabled`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  } catch (e) {
    alert("Error updating skill: " + e.message);
  }
}

function toggleSkillConfig(name) {
  const row = document.getElementById(`skill-cfg-${name}`);
  if (!row) return;
  row.style.display = row.style.display === "none" ? "table-row" : "none";
}

async function saveSkillConfig(name) {
  const skill = await (await fetch(`/api/skills/${name}/config`)).json();
  const schema = skill.schema || [];
  const errors = [];
  for (const f of schema) {
    if (f.type === "display") continue;
    const inp = document.getElementById(`scf-${name}-${f.name}`);
    if (!inp) continue;
    let value;
    if (f.type === "bool") value = inp.checked;
    else if (f.type === "int") value = parseInt(inp.value, 10);
    else if (f.type === "float") value = parseFloat(inp.value);
    else value = inp.value;
    try {
      const r = await fetch(`/api/skills/${name}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: f.name, value }),
      });
      if (!r.ok) {
        const body = await r.json();
        errors.push(`${f.name}: ${body.detail || r.statusText}`);
      }
    } catch (e) {
      errors.push(`${f.name}: ${e.message}`);
    }
  }
  if (errors.length) {
    alert("Save errors:\n" + errors.join("\n"));
  } else {
    // Collapse config row on success
    toggleSkillConfig(name);
    _skillsLoaded = false;  // Force reload next open to reflect new values
  }
}

async function sendUtterance() {
  const input = el("utterance-input");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  try {
    await fetch("/api/utterance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    input.value = "";
  } catch (e) {
    alert("Error dispatching utterance: " + e.message);
  }
}


async function announceRadon() {
  const btn = el("radon-announce-btn");
  if (btn) { btn.disabled = true; btn.textContent = "📢 Announcing…"; }
  try {
    const r = await fetch("/api/radon/announce", { method: "POST" });
    const d = await r.json();
    if (!d.ok) console.warn("Radon announce:", d.error || d);
  } catch (e) {
    console.warn("announceRadon error:", e);
  }
  setTimeout(() => {
    if (btn) { btn.disabled = false; btn.textContent = "📢 Announce"; }
  }, 3000);
}

async function announceDrop() {
  const btn = el("drop-announce-btn");
  if (btn) { btn.disabled = true; btn.textContent = "📢 Announcing…"; }
  try {
    const r = await fetch("/api/drop/announce", { method: "POST" });
    const d = await r.json();
    if (!d.ok) console.warn("DROP announce:", d.error || d);
  } catch (e) {
    console.warn("announceDrop error:", e);
  }
  setTimeout(() => {
    if (btn) { btn.disabled = false; btn.textContent = "📢 Announce"; }
  }, 3000);
}

// ── Process list ──────────────────────────────────────────────────

function renderProcesses(procs) {
  const tbody = el("processes-tbody");
  if (!tbody) return;
  if (!procs || procs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="padding:4px 6px;color:var(--text-muted)">No processes found</td></tr>';
    return;
  }
  const roleColor = { main: "var(--blue)", child: "var(--text-muted)", companion: "var(--green)" };
  tbody.innerHTML = procs.map(p => {
    const nameStyle = p.role === "main" ? "font-weight:600;color:var(--blue)" : "";
    const rc = roleColor[p.role] || "var(--text-muted)";
    const cpuColor = (p.cpu_pct || 0) > 50 ? "var(--red)" : (p.cpu_pct || 0) > 15 ? "var(--yellow)" : "";
    const threads = p.threads != null ? p.threads : "—";
    return `<tr>
      <td style="padding:2px 6px;${nameStyle}">${esc(p.name)}</td>
      <td style="padding:2px 6px;color:var(--text-muted);font-size:0.75em">${p.pid}</td>
      <td style="padding:2px 6px;font-size:0.75em;color:${rc}">${p.role}</td>
      <td style="padding:2px 6px;font-size:0.75em;color:var(--text-muted)">${esc(p.status)}</td>
      <td style="padding:2px 6px;text-align:right;color:${cpuColor || 'inherit'}">${p.cpu_pct != null ? p.cpu_pct.toFixed(1) : "—"}</td>
      <td style="padding:2px 6px;text-align:right">${p.mem_mb != null ? p.mem_mb.toFixed(1) : "—"}</td>
      <td style="padding:2px 6px;text-align:right;color:var(--text-muted)">${threads}</td>
    </tr>`;
  }).join("");
}

async function loadProcesses() {
  try {
    const d = await fetch("/api/processes").then(r => r.json());
    renderProcesses(d.processes || []);
  } catch (e) {
    const tbody = el("processes-tbody");
    if (tbody) tbody.innerHTML = `<tr><td colspan="7" style="padding:4px 6px;color:var(--red)">Error: ${esc(String(e))}</td></tr>`;
  }
}

async function restartDaemon() {
  if (!confirm("Restart the vera-core service?")) return;
  const btn = document.querySelector('[onclick="restartDaemon()"]');
  if (btn) { btn.disabled = true; btn.textContent = "⟳ Restarting…"; }
  try {
    await fetch("/api/restart", { method: "POST" });
  } catch (e) { /* connection drops as service restarts — expected */ }
  // Re-enable button after a delay so the user can retry if needed
  setTimeout(() => {
    if (btn) { btn.disabled = false; btn.textContent = "⟳ Restart Daemon"; }
  }, 8000);
}

async function systemReboot() {
  if (!confirm("Reboot the Raspberry Pi?")) return;
  const btn = el("btn-reboot");
  if (btn) { btn.disabled = true; btn.textContent = "↺ Rebooting…"; }
  try {
    await fetch("/api/system/reboot", { method: "POST" });
  } catch (e) { /* expected — connection drops */ }
}

async function systemShutdown() {
  if (!confirm("Shut down the Raspberry Pi? It will need to be powered on manually.")) return;
  const btn = el("btn-shutdown");
  if (btn) { btn.disabled = true; btn.textContent = "⏻ Shutting down…"; }
  try {
    await fetch("/api/system/shutdown", { method: "POST" });
  } catch (e) { /* expected — connection drops */ }
}

// ── Lightbox ─────────────────────────────────────────────────────

function openLightbox(src, label) {
  const lb = el("lightbox");
  el("lightbox-img").src = src;
  el("lightbox-label").textContent = label;
  lb.classList.add("active");
}

function closeLightbox() {
  el("lightbox").classList.remove("active");
  el("lightbox-img").src = "";
}

// ── Say on enter key ──────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initTabs();

  // Set camera stream src with API key so MJPEG streams authenticate.
  // Attach onerror AFTER setting src so the initial HTML src="" doesn't
  // hide cam2-wrap before we get a chance to load it properly.
  const _streamKey = encodeURIComponent(VERA_API_KEY);
  const _cam1 = document.getElementById('camera-stream');
  const _cam2 = document.getElementById('camera-stream2');
  if (_cam1) _cam1.src = `/stream?key=${_streamKey}`;
  if (_cam2) {
    _cam2.onerror = () => {
      const wrap = document.getElementById('cam2-wrap');
      if (wrap) wrap.style.display = 'none';
    };
    _cam2.src = `/stream2?key=${_streamKey}`;
  }

  el("say-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSay();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
  });
  _initPanSlider();
  loadFaces();
  loadQuietHours();
  loadFanControlPoints();
  loadAudioSettings();
  loadVoiceSettings();
  loadXvfState();
  loadAudioInputGain();
  loadAudioVoiceGain();
  loadServoEnabled();
  loadServoLimits();
  loadFaceTrackingEnabled();
  loadRandomMotionEnabled();
  loadGreetingSettings();
  loadCamRotation();
  loadCam2Rotation();
  loadStreamResolution();
  loadCam2Resolutions();
  loadObjectDetectionEnabled();
  loadPrivacySettings();
  loadMusicStatus();
  loadPodcasts();
  loadPodcastStatus();
  loadProcesses();
  loadDepthSettings();
  connectWS();
  _startRoomPoller();
  initFpsCounters();
  loadTrackingParams();
  initCardDragDrop();

  // IoT button event delegation — handles Add, Config, Remove, Announce, and
  // device-action buttons. Avoids inline onclick attributes which can be
  // silently blocked by certain browser security policies or extension conflicts.
  document.body.addEventListener('click', e => {
    const btn = e.target.closest('[data-iot-action]');
    if (!btn) return;
    const action   = btn.dataset.iotAction;
    const deviceId = btn.dataset.deviceId;
    switch (action) {
      case 'add':      openIoTAddModal(); break;
      case 'config':   openIoTConfigModal(deviceId); break;
      case 'announce': announceIotDevice(deviceId); break;
      case 'remove':   removeIoTDevice(deviceId); break;
      case 'do-action':
        doIotAction(
          deviceId,
          btn.dataset.actionId,
          btn.dataset.requiresPin === 'true',
          btn.dataset.requiresInput === 'true',
          btn.dataset.actionPrompt || '',
          btn.dataset.actionParam  || 'value'
        );
        break;
    }
  });
  // Refresh face registry every 30s; music status every 2s; depth maps every 3s
  setInterval(loadFaces, 30000);
  setInterval(loadMusicStatus, 2000);
  setInterval(loadPodcastStatus, 2000);
  setInterval(loadPodcasts, 30000);
  setInterval(loadAudioInputGain, 30000);
  setInterval(loadAudioVoiceGain, 30000);
  setInterval(() => {
    const dense = el("depth-dense-enabled") && el("depth-dense-enabled").checked;
    const mono  = el("depth-mono-enabled")  && el("depth-mono-enabled").checked;
    if (dense) refreshDepthMap();
    if (mono)  refreshMonoMap();
    if (dense || mono) refreshDepthStats();
  }, 3000);
});

// ── Music (Pandora/pianobar) ──────────────────────────────────────

const _MUSIC_ICONS = { playing: "▶", paused: "⏸", stopped: "■", loading: "⏳" };

function _fmtSec(s) {
  const m = Math.floor((s || 0) / 60);
  const sec = Math.floor((s || 0) % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

async function loadMusicStatus() {
  try {
    const d = await fetch("/api/music/status").then(r => r.json());
    _applyMusicState(d.state || "stopped");
    _applyMusicSong(d.song || {}, d.elapsed_sec || 0, d.duration_sec || 0);
    _applyMusicStations(d.stations || [], (d.song || {}).current_station_id);
    el("music-not-configured").style.display = d.configured === false ? "" : "none";
    // Volume
    if (d.volume >= 0) {
      el("music-volume-slider").value = d.volume;
      el("music-volume-label").textContent = `${d.volume}%`;
    }
    const muteBtn = el("audio-mute-btn");
    if (muteBtn) muteBtn.textContent = d.muted ? "🔊 Unmute" : "🔇 Mute";
    // EQ — skip if user has the custom EQ panel open (they're actively editing)
    const _customPanel = el("custom-eq-panel");
    const _customPanelOpen = _customPanel && _customPanel.style.display !== "none";
    if (d.eq_preset && !_customPanelOpen) {
      el("music-eq-select").value = d.eq_preset;
      // Show custom EQ panel immediately if preset is "custom".
      const panel = el("custom-eq-panel");
      if (panel) panel.style.display = (d.eq_preset === "custom") ? "block" : "none";
      if (d.eq_preset === "custom") loadCustomEq();
    }
  } catch (e) { /* ignore */ }
}

function _applyMusicState(state) {
  el("music-state-icon").textContent = _MUSIC_ICONS[state] || "■";
}

function _applyMusicSong(song, elapsed, duration) {
  el("music-song-title").textContent   = song.title   || "—";
  el("music-song-artist").textContent  = song.artist  ? `by ${song.artist}` : "";
  el("music-song-album").textContent   = song.album   || "";
  el("music-song-station").textContent = song.station ? `· ${song.station}` : "";

  // Album art
  const img = el("music-art-img");
  const placeholder = el("music-art-placeholder");
  const url = song.cover_art_url || "";
  if (url && img.src !== url) {
    img.src = url;
    img.style.display = "";
    placeholder.style.display = "none";
  } else if (!url) {
    img.src = "";
    img.style.display = "none";
    placeholder.style.display = "flex";
  }

  // Progress bar — only update when elapsed/duration are explicitly provided
  // (the WebSocket song_changed event does not carry timing data).
  if (elapsed !== undefined && duration !== undefined) {
    const bar = el("music-progress-bar");
    const dur = duration || 0;
    bar.max = dur > 0 ? dur : 100;
    bar.value = elapsed || 0;
    el("music-elapsed").textContent  = _fmtSec(elapsed);
    el("music-duration").textContent = _fmtSec(dur);
  }
}

function _applyMusicStations(stations, currentId) {
  const sel = el("music-station-select");
  while (sel.options.length > 1) sel.remove(1);
  for (const s of stations) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.name;
    sel.appendChild(opt);
  }
  // Select the currently playing station if known
  if (currentId !== undefined && currentId !== null) {
    sel.value = String(currentId);
  }
}

async function musicPlay() {
  const sel = el("music-station-select");
  const body = sel.value ? { station_id: parseInt(sel.value) } : {};
  await fetch("/api/music/play", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function musicStop() {
  await fetch("/api/music/stop", { method: "POST" });
}

async function musicNext() {
  await fetch("/api/music/next", { method: "POST" });
}

async function musicPause() {
  await fetch("/api/music/pause", { method: "POST" });
}

async function musicThumbsUp() {
  const st = el("music-status");
  await fetch("/api/music/thumbs-up", { method: "POST" });
  st.textContent = "👍 Loved!";
  setTimeout(() => { st.textContent = ""; }, 3000);
}

async function musicThumbsDown() {
  const st = el("music-status");
  await fetch("/api/music/thumbs-down", { method: "POST" });
  st.textContent = "👎 Banned — skipping…";
  setTimeout(() => { st.textContent = ""; }, 3000);
}

async function musicSetStation(stationId) {
  if (!stationId) return;
  await fetch("/api/music/station", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ station_id: parseInt(stationId) }),
  });
}

let _volTimer = null;
async function musicSetVolume(level) {
  const pct = parseInt(level);
  el("music-volume-label").textContent = `${pct}%`;
  clearTimeout(_volTimer);
  _volTimer = setTimeout(async () => {
    try {
      await fetch("/api/music/volume", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: pct }),
      });
    } catch (e) { /* ignore */ }
  }, 300);
}

let _inputGainTimer = null;
async function audioSetInputGain(level) {
  const pct = parseInt(level, 10);
  const label = el("audio-input-gain-label");
  const st = el("audio-input-gain-status");
  if (label) label.textContent = `${pct}%`;
  clearTimeout(_inputGainTimer);
  _inputGainTimer = setTimeout(async () => {
    try {
      const r = await fetch("/api/audio/input-gain", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: pct }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      if (st) {
        st.textContent = "Saved";
        st.style.color = "var(--green)";
      }
    } catch (_e) {
      if (st) {
        st.textContent = "Error";
        st.style.color = "var(--red)";
      }
    }
    setTimeout(() => {
      if (st) st.textContent = "";
    }, 1500);
  }, 250);
}


let _voiceGainTimer = null;
async function audioSetVoiceGain(level) {
  const pct = parseInt(level, 10);
  const label = el("audio-voice-gain-label");
  const st = el("audio-voice-gain-status");
  if (label) label.textContent = `${pct}%`;
  clearTimeout(_voiceGainTimer);
  _voiceGainTimer = setTimeout(async () => {
    try {
      const r = await fetch("/api/audio/voice-gain", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: pct }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      if (st) {
        st.textContent = "Saved";
        st.style.color = "var(--green)";
      }
    } catch (_e) {
      if (st) {
        st.textContent = "Error";
        st.style.color = "var(--red)";
      }
    }
    setTimeout(() => {
      if (st) st.textContent = "";
    }, 1500);
  }, 250);
}

async function musicSetEq(preset) {
  const panel = el("custom-eq-panel");
  if (panel) panel.style.display = (preset === "custom") ? "block" : "none";
  if (preset === "custom") {
    await loadCustomEq();
    return; // don't PUT named preset — wait for user to click Apply
  }
  try {
    await fetch("/api/music/eq", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset }),
    });
  } catch (e) { /* ignore */ }
}

async function loadCustomEq() {
  try {
    const r = await fetch("/api/music/eq/custom");
    if (!r.ok) return;
    const d = await r.json();
    renderCustomEqRows(d.bands || []);
  } catch (e) { /* ignore */ }
}

function renderCustomEqRows(bands) {
  const tbody = el("custom-eq-rows");
  if (!tbody) return;
  tbody.innerHTML = "";
  const defaultBands = bands.length ? bands : [
    {hz: 80,   gain_db: 0, q: 1.0},
    {hz: 250,  gain_db: 0, q: 1.0},
    {hz: 1000, gain_db: 0, q: 1.0},
    {hz: 4000, gain_db: 0, q: 1.0},
    {hz: 12000,gain_db: 0, q: 1.0},
  ];
  defaultBands.forEach((b, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="padding:2px 6px;color:var(--muted)">${i+1}</td>
      <td style="padding:2px 4px"><input type="number" class="ceq-hz" value="${b.hz}" min="20" max="20000" step="10"
            style="width:72px"></td>
      <td style="padding:2px 4px">
        <input type="range" class="ceq-gain" value="${b.gain_db}" min="-12" max="12" step="0.5"
               style="width:90px"
               oninput="this.nextElementSibling.textContent=parseFloat(this.value).toFixed(1)+'dB'">
        <span style="font-size:0.8em;min-width:44px;display:inline-block">${parseFloat(b.gain_db).toFixed(1)}dB</span>
      </td>
      <td style="padding:2px 4px"><input type="number" class="ceq-q" value="${b.q}" min="0.1" max="10" step="0.1"
            style="width:54px"></td>
      <td><button class="btn btn-danger btn-sm" onclick="this.closest('tr').remove()">✕</button></td>`;
    tbody.appendChild(tr);
  });
}

function customEqAddBand() {
  const tbody = el("custom-eq-rows");
  if (!tbody) return;
  const idx = tbody.rows.length + 1;
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td style="padding:2px 6px;color:var(--muted)">${idx}</td>
    <td style="padding:2px 4px"><input type="number" class="ceq-hz" value="1000" min="20" max="20000" step="10"
          style="width:72px"></td>
    <td style="padding:2px 4px">
      <input type="range" class="ceq-gain" value="0" min="-12" max="12" step="0.5"
             style="width:90px"
             oninput="this.nextElementSibling.textContent=parseFloat(this.value).toFixed(1)+'dB'">
      <span style="font-size:0.8em;min-width:44px;display:inline-block">0.0dB</span>
    </td>
    <td style="padding:2px 4px"><input type="number" class="ceq-q" value="1.0" min="0.1" max="10" step="0.1"
          style="width:54px"></td>
    <td><button class="btn btn-danger btn-sm" onclick="this.closest('tr').remove()">✕</button></td>`;
  tbody.appendChild(tr);
}

async function customEqSave() {
  const tbody = el("custom-eq-rows");
  const st = el("custom-eq-status");
  if (!tbody) return;
  const bands = [];
  tbody.querySelectorAll("tr").forEach(tr => {
    const hz      = parseFloat(tr.querySelector(".ceq-hz")?.value  || 1000);
    const gain_db = parseFloat(tr.querySelector(".ceq-gain")?.value || 0);
    const q       = parseFloat(tr.querySelector(".ceq-q")?.value   || 1.0);
    bands.push({ hz, gain_db, q });
  });
  try {
    const r = await fetch("/api/music/eq/custom", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bands }),
    });
    if (r.ok) {
      if (st) { st.textContent = "Applied ✓"; st.style.color = "var(--green)"; }
    } else {
      if (st) { st.textContent = "Error " + r.status; st.style.color = "var(--red)"; }
    }
  } catch (e) {
    if (st) { st.textContent = "Error"; st.style.color = "var(--red)"; }
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 3000);
}

// ── Podcasts (Apple Podcasts) ─────────────────────────────────────
let _podcastScrubbing = false;

function _setPodcastStatus(msg, ok = true) {
  const st = el("podcast-status");
  if (!st) return;
  st.textContent = msg;
  st.style.color = ok ? "var(--text-muted)" : "var(--red)";
}

async function loadPodcasts() {
  const sel = el("podcast-sub-select");
  if (!sel) return;
  try {
    const d = await fetch("/api/podcasts").then(r => r.json());
    const subs = d.subscriptions || [];
    const prev = sel.value;
    sel.innerHTML = "";
    if (!subs.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "— no subscriptions —";
      sel.appendChild(opt);
      _setPodcastStatus("No podcast subscriptions yet.");
      _renderPodcastEpisodes([]);
      return;
    }
    for (const s of subs) {
      const opt = document.createElement("option");
      opt.value = String(s.id);
      opt.textContent = `${s.title || s.id}${s.author ? ` — ${s.author}` : ""}`;
      sel.appendChild(opt);
    }
    if (prev && [...sel.options].some(o => o.value === prev)) {
      sel.value = prev;
    }
    await podcastLoadEpisodes();
  } catch (_e) {
    _setPodcastStatus("Podcast service unavailable.", false);
  }
}

async function loadPodcastStatus() {
  try {
    const s = await fetch("/api/podcasts/status").then(r => r.json());
    const state = s.state || "stopped";
    const pos = Math.max(0, Number(s.position_sec || 0));
    const dur = Math.max(0, Number(s.duration_sec || 0));
    const bar = el("podcast-progress-bar");
    if (bar && !_podcastScrubbing) {
      bar.max = dur > 0 ? dur : 100;
      bar.value = Math.min(pos, Number(bar.max));
    }
    const elapsedEl = el("podcast-elapsed");
    const durEl = el("podcast-duration");
    if (elapsedEl) elapsedEl.textContent = _fmtSec(pos);
    if (durEl) durEl.textContent = _fmtSec(dur);
    if (state === "playing" || state === "paused") {
      const icon = state === "playing" ? "▶" : "⏸";
      _setPodcastStatus(
        `${icon} ${s.podcast_title || "Podcast"} — ${s.episode_title || "Episode"} · ${_fmtSec(pos)} / ${_fmtSec(dur)}`,
        true
      );
    } else {
      _setPodcastStatus("No podcast playback");
      if (bar && !_podcastScrubbing) {
        bar.max = 100;
        bar.value = 0;
      }
      if (elapsedEl) elapsedEl.textContent = "0:00";
      if (durEl) durEl.textContent = "0:00";
    }
  } catch (_e) {
    _setPodcastStatus("Podcast status unavailable.", false);
  }
}

function _renderPodcastSearchResults(results) {
  const out = el("podcast-search-results");
  if (!out) return;
  if (!results || !results.length) {
    out.textContent = "No Apple Podcasts results.";
    return;
  }
  out.innerHTML = results.slice(0, 5).map(r =>
    `<div style="margin:2px 0">• <b>${esc(r.title || "")}</b>${r.author ? ` — ${esc(r.author)}` : ""}</div>`
  ).join("");
}

async function podcastSearch() {
  const q = (el("podcast-query")?.value || "").trim();
  if (!q) {
    _renderPodcastSearchResults([]);
    return;
  }
  try {
    const d = await fetch(`/api/podcasts/search?q=${encodeURIComponent(q)}&limit=10`).then(r => r.json());
    _renderPodcastSearchResults(d.results || []);
  } catch (_e) {
    _setPodcastStatus("Podcast search failed.", false);
  }
}

async function podcastSubscribe() {
  const q = (el("podcast-query")?.value || "").trim();
  if (!q) return;
  try {
    const r = await fetch("/api/podcasts/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query_or_url: q }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      throw new Error(d.detail || d.error || `HTTP ${r.status}`);
    }
    _setPodcastStatus(`Subscribed: ${d.subscription?.title || "podcast"}`);
    await loadPodcasts();
  } catch (_e) {
    _setPodcastStatus("Subscribe failed.", false);
  }
}

function _renderPodcastEpisodes(episodes) {
  const sel = el("podcast-episode-select");
  if (!sel) return;
  sel.innerHTML = "";
  if (!episodes.length) {
    const opt = document.createElement("option");
    opt.value = "0";
    opt.textContent = "— latest episode —";
    sel.appendChild(opt);
    return;
  }
  episodes.forEach((ep, idx) => {
    const opt = document.createElement("option");
    opt.value = String(idx);
    opt.textContent = `${idx}. ${ep.title || "Episode"}`;
    sel.appendChild(opt);
  });
}

async function podcastLoadEpisodes() {
  const pid = el("podcast-sub-select")?.value || "";
  if (!pid) {
    _renderPodcastEpisodes([]);
    return;
  }
  try {
    const d = await fetch(`/api/podcasts/${encodeURIComponent(pid)}/episodes?limit=25`).then(r => r.json());
    _renderPodcastEpisodes(d.episodes || []);
  } catch (_e) {
    _setPodcastStatus("Failed loading episodes.", false);
  }
}

async function podcastPlay() {
  const pid = el("podcast-sub-select")?.value || "";
  if (!pid) return;
  const idx = parseInt(el("podcast-episode-select")?.value || "0", 10);
  try {
    const r = await fetch("/api/podcasts/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ podcast_id: pid, episode_index: idx }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadPodcastStatus();
  } catch (_e) {
    _setPodcastStatus("Play failed.", false);
  }
}

async function podcastPause() {
  try {
    await fetch("/api/podcasts/pause", { method: "POST" });
    await loadPodcastStatus();
  } catch (_e) {
    _setPodcastStatus("Pause failed.", false);
  }
}

async function podcastResume() {
  try {
    await fetch("/api/podcasts/resume", { method: "POST" });
    await loadPodcastStatus();
  } catch (_e) {
    _setPodcastStatus("Resume failed.", false);
  }
}

async function podcastStop() {
  try {
    await fetch("/api/podcasts/stop", { method: "POST" });
    await loadPodcastStatus();
  } catch (_e) {
    _setPodcastStatus("Stop failed.", false);
  }
}

function podcastPreviewSeek(value) {
  _podcastScrubbing = true;
  const val = Math.max(0, Number(value || 0));
  const elapsedEl = el("podcast-elapsed");
  if (elapsedEl) elapsedEl.textContent = _fmtSec(val);
}

async function podcastCommitSeek(value) {
  _podcastScrubbing = false;
  const target = Math.max(0, Number(value || 0));
  try {
    const r = await fetch("/api/podcasts/seek", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position_sec: target }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadPodcastStatus();
  } catch (_e) {
    _setPodcastStatus("Seek failed.", false);
  }
}

async function podcastSkip(deltaSec) {
  try {
    const r = await fetch("/api/podcasts/skip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delta_sec: Number(deltaSec) }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadPodcastStatus();
  } catch (_e) {
    _setPodcastStatus("Skip failed.", false);
  }
}

async function podcastRefresh() {
  const pid = el("podcast-sub-select")?.value || "";
  if (!pid) return;
  try {
    const r = await fetch(`/api/podcasts/${encodeURIComponent(pid)}/refresh`, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await podcastLoadEpisodes();
    _setPodcastStatus("Episodes refreshed.");
  } catch (_e) {
    _setPodcastStatus("Refresh failed.", false);
  }
}

async function podcastUnsubscribe() {
  const pid = el("podcast-sub-select")?.value || "";
  if (!pid) return;
  try {
    const r = await fetch(`/api/podcasts/${encodeURIComponent(pid)}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadPodcasts();
    _setPodcastStatus("Subscription removed.");
  } catch (_e) {
    _setPodcastStatus("Unsubscribe failed.", false);
  }
}

// ── Utils ─────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function num(v, decimals = 0) {
  const n = parseFloat(v);
  return isNaN(n) ? "—" : n.toFixed(decimals);
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString();
}

function fmtAge(ts) {
  const age = Math.floor(Date.now() / 1000 - ts);
  if (age < 60)   return `${age}s ago`;
  if (age < 3600) return `${Math.floor(age/60)}m ago`;
  if (age < 86400) return `${Math.floor(age/3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

// ── FPS counters ─────────────────────────────────────────────────
// FPS is computed server-side and delivered via WebSocket status updates.
// Chrome does not fire img load events per MJPEG frame, so we avoid that approach.
function updateFpsOverlays(status) {
  const b1 = el("fps-overlay-1");
  const b2 = el("fps-overlay-2");
  if (b1) b1.textContent = status.cam1_fps != null ? `${status.cam1_fps} fps` : "– fps";
  if (b2) b2.textContent = status.cam2_fps != null ? `${status.cam2_fps} fps` : "– fps";
}

function initFpsCounters() {
  // No-op: fps updates now come from the WebSocket status push (updateFpsOverlays).
}

// ── Head Tracking Tuning ────────────────────────────────────────

const _TRACKING_LABELS = {
  tracking_gain:        ["Gain",          ""],
  dead_zone_frac:       ["Dead Zone",     "frac"],
  max_speed_deg_s:      ["Max Speed",     "°/s"],
  kalman_r:             ["Kalman R",      "px²"],
  kalman_q_pos:         ["Kalman Q-pos",  ""],
  kalman_q_vel:         ["Kalman Q-vel",  ""],
  lookahead_s:          ["Look-ahead",    "s"],
  replan_threshold_deg: ["Replan",        "°"],
  move_base_s:          ["Move base",     "s"],
  move_scale_s_per_deg: ["Move scale",    "s/°"],
  move_max_s:           ["Move max",      "s"],
};

let _trackingRanges = {};
let _trackingDebugWS = null;
let _trackingSamples = []; // ring buffer of {t, face_x_raw, face_x_smoothed, target_angle, servo_angle}
let _trackingChartRAF = null;
let _trackingPanelOpen = false;

function toggleTuningPanel() {
  const panel = el("tuning-panel");
  const btn = el("tuning-toggle-btn");
  _trackingPanelOpen = !_trackingPanelOpen;
  panel.style.display = _trackingPanelOpen ? "block" : "none";
  btn.textContent = _trackingPanelOpen ? "▴ Hide" : "▾ Show";
  if (_trackingPanelOpen) {
    connectTrackingDebugWS();
    _startTrackingChartLoop();
  } else {
    if (_trackingDebugWS) { try { _trackingDebugWS.close(); } catch (e) {} _trackingDebugWS = null; }
    if (_trackingChartRAF) { cancelAnimationFrame(_trackingChartRAF); _trackingChartRAF = null; }
  }
}

async function loadTrackingParams() {
  try {
    const r = await fetch("/api/tracking/params");
    if (!r.ok) return;
    const data = await r.json();
    _trackingRanges = data.ranges || {};
    _buildTrackingSliders(data.params || {}, _trackingRanges);
  } catch (e) {
    console.warn("loadTrackingParams failed", e);
  }
}

function _buildTrackingSliders(params, ranges) {
  const wrap = el("tuning-sliders");
  if (!wrap) return;
  wrap.innerHTML = "";
  const names = Object.keys(_TRACKING_LABELS);
  for (const name of names) {
    if (!(name in ranges)) continue;
    const [min, max] = ranges[name];
    const val = params[name] !== undefined ? params[name] : (min + max) / 2;
    const [label, unit] = _TRACKING_LABELS[name];
    const step = _stepFor(min, max);
    const decimals = step < 0.01 ? 4 : step < 0.1 ? 3 : step < 1 ? 2 : 0;
    const row = document.createElement("div");
    row.className = "tuning-slider-row";
    row.innerHTML = `
      <label title="${name}">${label}${unit ? " (" + unit + ")" : ""}</label>
      <input type="range" id="ts-r-${name}" min="${min}" max="${max}" step="${step}" value="${val}">
      <input type="number" id="ts-n-${name}" min="${min}" max="${max}" step="${step}" value="${Number(val).toFixed(decimals)}">
    `;
    wrap.appendChild(row);
    const r = el(`ts-r-${name}`);
    const n = el(`ts-n-${name}`);
    let timer = null;
    const onChange = (src) => {
      const v = Number(src.value);
      if (src === r) n.value = v.toFixed(decimals);
      else r.value = v;
      clearTimeout(timer);
      timer = setTimeout(() => _putTrackingParam(name, v), 120);
    };
    r.addEventListener("input", () => onChange(r));
    n.addEventListener("change", () => onChange(n));
  }
}

function _stepFor(min, max) {
  const span = max - min;
  if (span <= 0.3) return 0.005;
  if (span <= 2) return 0.01;
  if (span <= 20) return 0.1;
  return 1;
}

async function _putTrackingParam(name, value) {
  try {
    await fetch("/api/tracking/params", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, value }),
    });
  } catch (e) { console.warn("set param failed", name, e); }
}

async function saveTrackingParams() {
  const status = el("tuning-save-status");
  try {
    const r = await fetch("/api/tracking/save", { method: "POST" });
    status.textContent = r.ok ? "✓ saved" : "✗ failed";
  } catch (e) { status.textContent = "✗ " + e; }
  setTimeout(() => { status.textContent = ""; }, 3000);
}

async function resetTrackingParams() {
  await fetch("/api/tracking/reset", { method: "POST" });
  setTimeout(loadTrackingParams, 200);
}

async function applyTrackingPreset() {
  const sel = el("tuning-preset-select");
  const name = sel.value;
  if (!name) return;
  await fetch("/api/tracking/preset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  setTimeout(loadTrackingParams, 250);
}

async function startAutoTune() {
  el("autotune-start-btn").style.display = "none";
  el("autotune-cancel-btn").style.display = "inline-block";
  el("autotune-status").textContent = "starting…";
  await fetch("/api/tracking/autotune/start", { method: "POST" });
}

async function cancelAutoTune() {
  await fetch("/api/tracking/autotune/cancel", { method: "POST" });
  _autoTuneFinish();
}

function _autoTuneFinish() {
  el("autotune-start-btn").style.display = "inline-block";
  el("autotune-cancel-btn").style.display = "none";
}

function connectTrackingDebugWS() {
  if (_trackingDebugWS) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/tracking-debug`;
  try {
    _trackingDebugWS = new WebSocket(url);
  } catch (e) { return; }
  _trackingDebugWS.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    _onTrackingEvent(msg.topic, msg.payload);
  };
  _trackingDebugWS.onclose = () => {
    _trackingDebugWS = null;
    if (_trackingPanelOpen) setTimeout(connectTrackingDebugWS, 1500);
  };
}

function _onTrackingEvent(topic, payload) {
  if (topic === "tracking.debug") {
    _trackingSamples.push({ t: Date.now(), ...payload });
    const cutoff = Date.now() - 5500;
    while (_trackingSamples.length && _trackingSamples[0].t < cutoff) _trackingSamples.shift();
  } else if (topic === "tracking.autotune_progress") {
    const s = el("autotune-status");
    if (s) s.textContent = `[${payload.stage}] ${payload.msg || ""} (${(payload.t_remaining || 0).toFixed(1)}s left)`;
  } else if (topic === "tracking.autotune_done") {
    const s = el("autotune-status");
    if (s) {
      if (payload.ok) {
        s.textContent = `✓ done — gain=${(payload.tracking_gain||0).toFixed(2)} R=${Math.round(payload.kalman_r||0)} lag=${(payload.lag_s||0).toFixed(2)}s overshoot=${(payload.overshoot_deg||0).toFixed(1)}°`;
      } else {
        s.textContent = `✗ ${payload.reason || "failed"}`;
      }
    }
    _autoTuneFinish();
    setTimeout(loadTrackingParams, 300);
  } else if (topic === "tracking.param_changed") {
    // Update slider silently to reflect server-side change (e.g. autotune)
    const name = payload.name;
    const v = payload.value;
    const r = el(`ts-r-${name}`);
    const n = el(`ts-n-${name}`);
    if (r) r.value = v;
    if (n) n.value = Number(v).toFixed(3);
  } else if (topic === "tracking.preset_applied" || topic === "tracking.save_params_done") {
    setTimeout(loadTrackingParams, 200);
  }
}

function _startTrackingChartLoop() {
  const cv = el("tracking-chart");
  if (!cv) return;
  const draw = () => {
    _drawTrackingChart(cv);
    _trackingChartRAF = requestAnimationFrame(draw);
  };
  if (!_trackingChartRAF) _trackingChartRAF = requestAnimationFrame(draw);
}

function _drawTrackingChart(cv) {
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#181818";
  ctx.fillRect(0, 0, W, H);
  if (_trackingSamples.length < 2) return;

  const now = Date.now();
  const t0 = now - 5000;
  const samples = _trackingSamples.filter(s => s.t >= t0);
  if (samples.length < 2) return;

  // Two y-axes: face_x in px (0..1280) on left; angle in deg (0..360) on right
  // Map both to 0..1 normalized vertical space
  const FACE_MAX = 1280;
  const ANGLE_MAX = 360;
  const xOf = (t) => ((t - t0) / 5000) * W;
  const yFace = (v) => H - (v / FACE_MAX) * H;
  const yAngle = (v) => H - (v / ANGLE_MAX) * H;

  const plot = (vals, yFn, color, dashed) => {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    if (dashed) ctx.setLineDash([4, 3]); else ctx.setLineDash([]);
    let started = false;
    for (const s of samples) {
      const v = vals(s);
      if (v === null || v === undefined || Number.isNaN(v)) { started = false; continue; }
      const x = xOf(s.t), y = yFn(v);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  };

  // Grid
  ctx.strokeStyle = "#2a2a2a";
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  for (let i = 1; i < 5; i++) {
    const x = (i / 5) * W;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }

  plot(s => s.face_raw,      yFace,  "#888", true);
  plot(s => s.face_smoothed, yFace,  "#4cc", false);
  plot(s => s.target,        yAngle, "#4f4", true);
  plot(s => s.servo_angle,   yAngle, "#fa4", false);
}

// ── Drag-and-drop card reordering ────────────────────────────────────────────
// Cards are reordered by dragging the ⠿ handle in each card's header.
// Order is persisted in localStorage (per tab-pane) so it survives page refresh.

const _CARD_ORDER_KEY = "da-tab-order";
let _dragSrcCard = null;
let _dragHandleDown = false;

function initCardDragDrop() {
  // Migrate away from old single-key order (pre-tab layout)
  localStorage.removeItem("da-card-order");

  // Restore saved card order for each pane independently
  document.querySelectorAll('.tab-pane').forEach(pane => {
    const key = `${_CARD_ORDER_KEY}-${pane.id}`;
    const saved = JSON.parse(localStorage.getItem(key) || "null");
    if (saved && Array.isArray(saved)) {
      saved.forEach(id => {
        const card = document.getElementById(id);
        if (card && card.closest('.tab-pane') === pane) pane.appendChild(card);
      });
    }
  });

  // Attach mousedown on every drag handle to gate which drags are allowed
  document.querySelectorAll(".drag-handle").forEach(handle => {
    handle.addEventListener("mousedown", () => { _dragHandleDown = true; });
    handle.addEventListener("mouseup",   () => { _dragHandleDown = false; });
  });

  document.querySelectorAll(".tab-pane > .card").forEach(card => {
    // Only allow drag when initiated from the handle
    card.addEventListener("dragstart", e => {
      if (!_dragHandleDown) { e.preventDefault(); return; }
      _dragSrcCard = card;
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.id);
    });

    card.addEventListener("dragend", () => {
      _dragHandleDown = false;
      if (_dragSrcCard) _dragSrcCard.classList.remove("dragging");
      _dragSrcCard = null;
      document.querySelectorAll(".drag-over-top, .drag-over-bottom")
              .forEach(c => c.classList.remove("drag-over-top", "drag-over-bottom"));
    });

    card.addEventListener("dragover", e => {
      if (!_dragSrcCard || _dragSrcCard === card) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const rect = card.getBoundingClientRect();
      const isTop = e.clientY < rect.top + rect.height / 2;
      card.classList.toggle("drag-over-top",    isTop);
      card.classList.toggle("drag-over-bottom", !isTop);
    });

    card.addEventListener("dragleave", e => {
      // Only clear if we actually left the card (not just entered a child)
      if (!card.contains(e.relatedTarget)) {
        card.classList.remove("drag-over-top", "drag-over-bottom");
      }
    });

    card.addEventListener("drop", e => {
      e.preventDefault();
      card.classList.remove("drag-over-top", "drag-over-bottom");
      if (!_dragSrcCard || _dragSrcCard === card) return;
      // Only allow drop within the same tab-pane
      const pane = card.closest('.tab-pane');
      if (!pane || !pane.contains(_dragSrcCard)) return;
      const rect = card.getBoundingClientRect();
      const insertBefore = e.clientY < rect.top + rect.height / 2;
      pane.insertBefore(_dragSrcCard, insertBefore ? card : card.nextSibling);
      _saveCardOrder(pane);
    });
  });

  // Make cards draggable (needed for HTML5 DnD API)
  document.querySelectorAll(".tab-pane > .card").forEach(c => c.setAttribute("draggable", "true"));
}

function _saveCardOrder(pane) {
  const order = Array.from(pane.querySelectorAll(':scope > .card')).map(c => c.id);
  localStorage.setItem(`${_CARD_ORDER_KEY}-${pane.id}`, JSON.stringify(order));
}

// ── Depth Estimation ──────────────────────────────────────────────

async function loadDepthSettings() {
  try {
    const d = await fetch("/api/settings/depth").then(r => r.json());
    el("depth-dense-enabled").checked = !!d.dense_enabled;
    el("depth-mono-enabled").checked  = !!d.mono_enabled;
    const calBadge = el("depth-dense-cal");
    calBadge.textContent = d.calibrated ? "✓ calibrated" : "⚠ not calibrated";
    calBadge.style.color = d.calibrated ? "var(--green)" : "var(--yellow)";
    const hwBadge = el("depth-mono-hw");
    hwBadge.textContent = d.mono_hardware_ready ? "✓ hardware ready" : "⚠ no hardware";
    hwBadge.style.color = d.mono_hardware_ready ? "var(--green)" : "var(--yellow)";
    // Show stats row if either is enabled
    el("depth-stats-row").style.display = (d.dense_enabled || d.mono_enabled) ? "" : "none";
    // Refresh images if already enabled at page load
    if (d.dense_enabled) refreshDepthMap();
    if (d.mono_enabled)  refreshMonoMap();
    if (d.dense_enabled || d.mono_enabled) refreshDepthStats();
  } catch (e) { /* ignore */ }
}

async function saveDepthSettings() {
  const dense = el("depth-dense-enabled").checked;
  const mono  = el("depth-mono-enabled").checked;
  try {
    await fetch("/api/settings/depth", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dense_enabled: dense, mono_enabled: mono }),
    });
    el("depth-stats-row").style.display = (dense || mono) ? "" : "none";
    if (!dense) { el("depth-map-img").style.display = "none"; el("depth-map-placeholder").style.display = ""; }
    if (!mono)  { el("depth-mono-img").style.display = "none"; el("depth-mono-placeholder").style.display = ""; }
    // Kick off immediate refresh on enable
    if (dense) refreshDepthMap();
    if (mono)  refreshMonoMap();
    refreshDepthStats();
  } catch (e) { /* ignore */ }
}

async function refreshDepthMap() {
  const img = el("depth-map-img");
  const ph  = el("depth-map-placeholder");
  try {
    const resp = await fetch("/api/depth/map");
    if (!resp.ok) { img.style.display = "none"; ph.style.display = ""; ph.textContent = "(not available — enable dense depth first)"; return; }
    const blob = await resp.blob();
    img.src = URL.createObjectURL(blob);
    img.style.display = "";
    ph.style.display = "none";
  } catch (e) { ph.textContent = "(error loading map)"; }
}

async function refreshMonoMap() {
  const img = el("depth-mono-img");
  const ph  = el("depth-mono-placeholder");
  try {
    const resp = await fetch("/api/depth/mono");
    if (!resp.ok) { img.style.display = "none"; ph.style.display = ""; ph.textContent = "(not available — enable mono depth first)"; return; }
    const blob = await resp.blob();
    img.src = URL.createObjectURL(blob);
    img.style.display = "";
    ph.style.display = "none";
  } catch (e) { ph.textContent = "(error loading map)"; }
}

async function refreshDepthStats() {
  try {
    const d = await fetch("/api/depth/query").then(r => r.json());
    el("depth-nearest").textContent  = d.nearest_m != null  ? `${d.nearest_m.toFixed(2)} m`  : "—";
    el("depth-mean").textContent     = d.mean_m    != null  ? `${d.mean_m.toFixed(2)} m`     : "—";
    el("depth-farthest").textContent = d.farthest_m != null ? `${d.farthest_m.toFixed(2)} m` : "—";
  } catch (e) { /* ignore */ }
}

async function saveSnapshot(cam) {
  const endpoint = cam === 2 ? "/api/snapshot2" : "/api/snapshot";
  const btnId    = `snap${cam}-btn`;
  const statusId = `snap${cam}-status`;
  const btn    = el(btnId);
  const status = el(statusId);

  btn.disabled = true;
  status.textContent = "Saving…";
  status.style.opacity = "1";

  try {
    const resp = await fetch(endpoint);
    if (!resp.ok) {
      const err = await resp.text().catch(() => resp.statusText);
      status.textContent = `Error: ${err}`;
      status.style.color = "var(--red, #e05)";
      return;
    }
    const blob = await resp.blob();
    const ts   = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const filename = `vera-cam${cam}-${ts}.jpg`;
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    status.textContent = `✓ ${filename}`;
    status.style.color = "var(--green)";
    setTimeout(() => { status.style.opacity = "0"; }, 3000);
    setTimeout(() => { status.textContent = ""; status.style.opacity = "1"; }, 3400);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
    status.style.color = "var(--red, #e05)";
  } finally {
    btn.disabled = false;
  }
}
