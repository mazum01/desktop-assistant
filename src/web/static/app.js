/* VERA Dashboard — app.js */

// ── API key management ────────────────────────────────────────────────────
// The dashboard requires an API key (set via VERA_API_KEY in secrets.env).
// On first visit the login overlay is shown; the key is stored in
// localStorage so subsequent loads connect automatically.

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

if (!VERA_API_KEY) _showKeyOverlay();

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
    } catch (e) { /* ignore */ }
  };
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
    const c = num(temp.celsius, 1);
    const f = num(temp.fahrenheit, 1);
    el("stat-temp").textContent = `${c} °C / ${f} °F`;
    el("stat-temp").style.color = temp.ok === false ? "var(--red)" : "var(--blue)";
  }

  const fan = last["thermal.fan"];
  const rpm = last["thermal.rpm"];
  if (fan) {
    const duty = Math.round(fan.duty ?? 0);
    const rpmVal = rpm ? rpm.rpm : "—";
    el("stat-fan").textContent = `${duty}%`;
    el("stat-fan").title = `${rpmVal} RPM · backend: ${fan.backend ?? "?"}`;
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

  const spoke = last["av.spoke"];
  if (spoke && spoke.text) {
    const snip = spoke.text.length > 60 ? spoke.text.slice(0, 60) + "…" : spoke.text;
    el("stat-spoken").textContent = `"${snip}"`;
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

  // FPS overlays — driven by server-side frame counters (Chrome-compatible)
  updateFpsOverlays(data);

  // System resource graphs
  if (data.cpu_history != null) {
    el("stat-cpu").textContent = `${Math.round(data.cpu_percent ?? 0)}%`;
    drawSparkline("cpu-graph", data.cpu_history, "#58a6ff");
  }
  if (data.mem_history != null) {
    el("stat-mem").textContent = `${Math.round(data.mem_percent ?? 0)}%`;
    drawSparkline("mem-graph", data.mem_history, "#3fb950");
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
}

// ── Room detection visualisation ──────────────────────────────────

function updateRoomDetail(d) {
  if (!d) return;

  // Scene stability bar
  const sim = d.last_similarity;
  const thresh = d.similarity_thresh != null ? d.similarity_thresh : 0.85;
  const fillEl = el("room-stability-fill");
  const pctEl  = el("room-stability-pct");
  if (sim != null) {
    const pct = Math.round(sim * 100);
    fillEl.style.width = pct + "%";
    if (sim >= thresh)           fillEl.style.background = "var(--green)";
    else if (sim >= thresh - 0.1) fillEl.style.background = "var(--yellow)";
    else                          fillEl.style.background = "var(--red)";
    pctEl.textContent = pct + "%";
  } else {
    fillEl.style.width = "0%";
    fillEl.style.background = "var(--text-dim)";
    pctEl.textContent = "—";
  }

  // Drift counter dots
  const count = d.consec_diverged || 0;
  const max   = d.consec_diverged_threshold || 3;
  let dotsHtml = "";
  for (let i = 0; i < max; i++) {
    dotsHtml += `<span class="room-drift-dot${i < count ? " filled" : ""}"></span>`;
  }
  const dotsEl = el("room-drift-dots");
  if (dotsEl) dotsEl.innerHTML = dotsHtml;
  const labelEl = el("room-drift-label");
  if (labelEl) {
    labelEl.textContent = count > 0 ? `${count}/${max} diverged` : "stable";
    labelEl.style.color = count > 0 ? "var(--yellow)" : "var(--text-dim)";
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


function drawSparkline(canvasId, values, color) {
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

  // Fill area under the line
  ctx.beginPath();
  ctx.moveTo(pad, pad + graphH);
  for (let i = 0; i < values.length; i++) {
    const x = pad + i * step;
    const y = pad + graphH - (Math.min(values[i], 100) / 100) * graphH;
    i === 0 ? ctx.lineTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.lineTo(pad + (values.length - 1) * step, pad + graphH);
  ctx.closePath();
  ctx.fillStyle = color + "28";  // ~16% opacity fill
  ctx.fill();

  // Draw line
  ctx.beginPath();
  for (let i = 0; i < values.length; i++) {
    const x = pad + i * step;
    const y = pad + graphH - (Math.min(values[i], 100) / 100) * graphH;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();

  // Draw 50% and 100% reference lines
  ctx.setLineDash([2, 4]);
  ctx.strokeStyle = "rgba(139,148,158,0.25)";
  ctx.lineWidth = 1;
  [50, 100].forEach(pct => {
    const y = pad + graphH - (pct / 100) * graphH;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(pad + graphW, y);
    ctx.stroke();
  });
  ctx.setLineDash([]);
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
  if (btn) { btn.disabled = true; btn.textContent = "Describing…"; }
  try {
    await fetch("/api/vision/describe", { method: "POST" });
  } catch (e) { /* ignore */ }
  setTimeout(() => {
    if (btn) { btn.disabled = false; btn.textContent = "Describe What I See"; }
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
  // Set camera stream src with API key so MJPEG streams authenticate.
  const _streamKey = encodeURIComponent(VERA_API_KEY);
  const _cam1 = document.getElementById('camera-stream');
  const _cam2 = document.getElementById('camera-stream2');
  if (_cam1) _cam1.src = `/stream?key=${_streamKey}`;
  if (_cam2) _cam2.src = `/stream2?key=${_streamKey}`;

  el("say-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSay();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
  });
  _initPanSlider();
  loadFaces();
  loadQuietHours();
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
  loadMusicStatus();
  loadDepthSettings();
  connectWS();
  initFpsCounters();
  loadTrackingParams();
  initCardDragDrop();
  // Refresh face registry every 30s; music status every 2s; depth maps every 3s
  setInterval(loadFaces, 30000);
  setInterval(loadMusicStatus, 2000);
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
    // EQ
    if (d.eq_preset) {
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
// Order is persisted in localStorage so it survives page refresh.

const _CARD_ORDER_KEY = "da-card-order";
let _dragSrcCard = null;
let _dragHandleDown = false;

function initCardDragDrop() {
  const main = document.querySelector("main");

  // Restore saved order from localStorage
  const saved = JSON.parse(localStorage.getItem(_CARD_ORDER_KEY) || "null");
  if (saved && Array.isArray(saved)) {
    saved.forEach(id => {
      const el = document.getElementById(id);
      if (el) main.appendChild(el);
    });
  }

  // Attach mousedown on every drag handle to gate which drags are allowed
  document.querySelectorAll(".drag-handle").forEach(handle => {
    handle.addEventListener("mousedown", () => { _dragHandleDown = true; });
    handle.addEventListener("mouseup",   () => { _dragHandleDown = false; });
  });

  document.querySelectorAll("main > .card").forEach(card => {
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
      const rect = card.getBoundingClientRect();
      const insertBefore = e.clientY < rect.top + rect.height / 2;
      main.insertBefore(_dragSrcCard, insertBefore ? card : card.nextSibling);
      _saveCardOrder();
    });
  });

  // Make cards draggable (needed for HTML5 DnD API)
  document.querySelectorAll("main > .card").forEach(c => c.setAttribute("draggable", "true"));
}

function _saveCardOrder() {
  const order = Array.from(document.querySelectorAll("main > .card")).map(c => c.id);
  localStorage.setItem(_CARD_ORDER_KEY, JSON.stringify(order));
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
