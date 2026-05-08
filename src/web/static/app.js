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

  // Face overlay (badge) + canvas bounding boxes
  const pf = last["perception.faces"];
  if (pf != null) {
    const count = pf.count ?? 0;
    const names = (pf.faces || [])
      .filter(f => f.name)
      .map(f => f.name)
      .join(", ");
    el("face-overlay").textContent = count === 0
      ? "0 faces"
      : `${count} face${count !== 1 ? "s" : ""}${names ? ": " + names : ""}`;

    // Draw bounding boxes on the canvas overlay
    const vfr = last["vision.frame_ready"];
    const frameW = vfr?.frame_w || 1280;
    const frameH = vfr?.frame_h || 720;
    drawFaceBoxes(pf.faces || [], frameW, frameH);
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
}

// ── Face bounding box overlay ─────────────────────────────────────

function drawFaceBoxes(faces, frameW, frameH) {
  const canvas = el("face-canvas");
  if (!canvas) return;
  const img = el("camera-stream");
  const rect = img.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return;

  // Match canvas logical resolution to its CSS-displayed size for crisp drawing.
  canvas.width  = rect.width;
  canvas.height = rect.height;

  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (faces.length === 0) return;

  // Compute the rendered image area inside the <img> (accounts for object-fit: contain).
  const imgAspect    = frameW / frameH;
  const boxAspect    = rect.width / rect.height;
  let renderW, renderH, offsetX, offsetY;
  if (imgAspect > boxAspect) {
    renderW = rect.width;
    renderH = rect.width / imgAspect;
    offsetX = 0;
    offsetY = (rect.height - renderH) / 2;
  } else {
    renderH = rect.height;
    renderW = rect.height * imgAspect;
    offsetX = (rect.width - renderW) / 2;
    offsetY = 0;
  }

  const scaleX = renderW / frameW;
  const scaleY = renderH / frameH;

  for (const face of faces) {
    const bbox = face.bbox;
    if (!bbox || bbox.length < 4) continue;
    const [x1, y1, x2, y2] = bbox;
    const cx = offsetX + x1 * scaleX;
    const cy = offsetY + y1 * scaleY;
    const bw = (x2 - x1) * scaleX;
    const bh = (y2 - y1) * scaleY;

    // Box
    ctx.strokeStyle = "#00ff88";
    ctx.lineWidth   = 2;
    ctx.strokeRect(cx, cy, bw, bh);

    // Label background + text
    const label = face.name || (face.face_id ? "unknown" : null);
    if (label) {
      const pad   = 4;
      const fontSize = Math.max(11, Math.round(bw / 8));
      ctx.font = `bold ${fontSize}px monospace`;
      const tw = ctx.measureText(label).width;
      const lh = fontSize + pad * 2;
      const lx = cx;
      const ly = cy > lh ? cy - lh : cy + bh;
      ctx.fillStyle = "rgba(0,0,0,0.65)";
      ctx.fillRect(lx, ly, tw + pad * 2, lh);
      ctx.fillStyle = "#00ff88";
      ctx.fillText(label, lx + pad, ly + fontSize + pad - 2);
    }
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
    ? `<img src="/api/faces/${esc(face.id)}/thumb" class="face-thumb" alt="face" />`
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

// ── Controls ──────────────────────────────────────────────────────

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

// ── Say on enter key ──────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  el("say-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSay();
  });
  loadFaces();
  loadQuietHours();
  loadServoEnabled();
  loadFaceTrackingEnabled();
  loadRandomMotionEnabled();
  connectWS();
  // Refresh face registry every 30s
  setInterval(loadFaces, 30000);
});

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
