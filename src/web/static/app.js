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

  // Face overlay
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
  }

  // Services — we derive running state from service.started signals
  // The WS data doesn't include service status directly, so we fetch from /api/status
  // on first load and cache it; services section updated via separate poll.

  // Event log
  const events = data.events || [];
  renderEventLog(events);
}

// ── Services polling ──────────────────────────────────────────────

async function loadServices() {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const data = await r.json();
    const last = data.last || {};
    // We don't get services from this endpoint — leave pills as-is
    // (services come from the daemon's IPC status, not the web service's bus view)
  } catch (e) { /* ignore */ }
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
  const firstSeen = face.first_seen ? fmtTime(face.first_seen) : "—";
  const lastSeen  = face.last_seen  ? fmtAge(face.last_seen)   : "—";
  const count     = face.seen_count ?? "—";
  const shortId   = (face.id || "").slice(0, 8);

  tr.innerHTML = `
    <td class="name-cell">
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
