/* Desktop Assistant Dashboard — app.js */

const WS_URL = `ws://${location.host}/ws`;
let ws = null;
let wsRetryMs = 1000;

// ── WebSocket connection ──────────────────────────────────────────

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
    el("stat-temp").textContent = `${c} °C`;
    el("stat-temp").title = `${f} °F`;
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
      const cls = state === "running" ? "pill-ok" : "pill-warn";
      return `<span class="pill ${cls}">${esc(name)}</span>`;
    }).join("");
  }

  // Event log
  const events = data.events || [];
  renderEventLog(events);

  // Music state/song updates from bus events
  const musicState = last["music.state_changed"];
  if (musicState) _applyMusicState(musicState.state || "stopped");
  const musicSong = last["music.song_changed"];
  if (musicSong) _applyMusicSong(musicSong);
  const musicStations = last["music.stations_updated"];
  if (musicStations) _applyMusicStations(musicStations.stations || []);
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

function makeFaceRow(face) {
  const tr = document.createElement("tr");
  tr.dataset.faceId = face.id;
  const firstSeen = face.first_seen ? fmtTime(face.first_seen) : "—";
  const lastSeen  = face.last_seen  ? fmtAge(face.last_seen)   : "—";
  const count     = face.seen_count ?? "—";

  const thumbHtml = face.has_thumb
    ? `<img src="/api/faces/${esc(face.id)}/thumb" class="face-thumb face-thumb-clickable"
           alt="face"
           data-lightbox-src="/api/faces/${esc(face.id)}/photo"
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

async function mergeFaces() {
  const rows = [...document.querySelectorAll("#face-tbody tr[data-face-id]")];
  const checked = rows.filter(r => r.querySelector(".face-merge-cb")?.checked);
  if (checked.length !== 2) return;
  const ids = checked.map(r => r.dataset.faceId);
  const names = ids.map(id => {
    const inp = document.getElementById(`name-${id}`);
    return inp ? inp.value : id.slice(0, 8);
  });
  const choice = confirm(
    `Merge two faces into one.\n\n` +
    `Keep: "${names[0]}" (${ids[0].slice(0,8)}…)\n` +
    `Absorb: "${names[1]}" (${ids[1].slice(0,8)}…)\n\n` +
    `The second entry will be deleted. OK?`
  );
  if (!choice) return;
  try {
    const r = await fetch("/api/faces/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_id: ids[0], absorb_id: ids[1] }),
    });
    if (r.ok) {
      loadFaces();
    } else {
      const d = await r.json().catch(() => ({}));
      alert("Merge failed: " + (d.detail || r.status));
    }
  } catch (e) { alert("Merge error: " + e); }
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

async function restartDaemon() {
  if (!confirm("Restart the desktop-assistant-core service?")) return;
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
  el("say-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSay();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
  });
  loadFaces();
  loadQuietHours();
  loadServoEnabled();
  loadServoLimits();
  loadFaceTrackingEnabled();
  loadRandomMotionEnabled();
  loadGreetingSettings();
  loadCamRotation();
  loadCam2Rotation();
  loadMusicStatus();
  connectWS();
  // Refresh face registry every 30s; music status every 2s
  setInterval(loadFaces, 30000);
  setInterval(loadMusicStatus, 2000);
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
