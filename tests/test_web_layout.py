"""Protect task-based control placement and LCARS navigation without a browser dependency."""

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "web" / "static"
JS = STATIC / "js"


def _run_module_test(tmp_path, script, module, *args):
    """Run a CommonJS harness that dynamically imports an ES module copy."""
    copy = tmp_path / (module.stem + ".mjs")
    copy.write_text(module.read_text())
    runner = tmp_path / "test.cjs"
    runner.write_text(script)
    result = subprocess.run(["node", str(runner), str(copy), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


class LayoutParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "ancestors": self.stack.copy()}
        self.elements.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img",
                       "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break


@pytest.fixture
def layout():
    parser = LayoutParser()
    parser.feed((STATIC / "index.html").read_text())
    return parser.elements


def test_ids_remain_unique(layout):
    counts = Counter(node["attrs"]["id"] for node in layout if "id" in node["attrs"])
    assert all(count == 1 for count in counts.values())


def test_servo_handlers_are_not_shadowed_by_duplicate_declarations():
    source = (STATIC / "app.js").read_text()
    for name in ("loadServoChannels", "saveServoChannels", "saveTiltLimits", "_sendServoSlider"):
        assert len(re.findall(r"async function " + name + r"\(", source)) == 1


def test_servo_sliders_do_not_require_go_buttons(layout):
    assert not any(
        node["attrs"].get("onclick") in {"doPan()", "doTilt()"} for node in layout
    )
    for axis in ("pan", "tilt"):
        status = next(node for node in layout if node["attrs"].get("id") == f"{axis}-command-status")
        assert status["attrs"]["role"] == "status"
    source = (STATIC / "app.js").read_text()
    assert '_updateServoSlider("pan", motion.angle)' in source
    assert '_updateServoSlider("tilt", motion.tilt_angle)' in source
    assert "_initServoSliders();" in source
    assert "function doPan(" not in source
    assert "function doTilt(" not in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for servo slider regression")
def test_servo_sliders_command_immediately_and_follow_position_updates(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const elements = {};
for (const axis of ['pan', 'tilt']) {
  for (const suffix of ['slider', 'angle-display', 'command-status']) {
    elements[`${axis}-${suffix}`] = {
      value: '90', textContent: '', style: {}, listeners: {},
      addEventListener(event, fn) { this.listeners[event] = fn; },
      setPointerCapture(id) { this.pointerId = id; },
    };
  }
}
const requests = [];
const context = vm.createContext({
  el: id => elements[id],
  servoRequest: (path, method, body) => new Promise((resolve, reject) => {
    requests.push({path, method, angle: body.angle, resolve, reject});
  }),
});
vm.runInContext(source.slice(source.indexOf('const _servoSliders ='),
  source.indexOf('function connectWS()')), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  context._initServoSliders();
  for (const axis of ['pan', 'tilt']) {
    const slider = elements[`${axis}-slider`];
    const display = elements[`${axis}-angle-display`];
    const status = elements[`${axis}-command-status`];
    context._updateServoSlider(axis, 92.3);
    assert.equal(slider.value, 92);
    assert.equal(display.textContent, '92°');
    slider.listeners.pointerdown({pointerId: 7});
    assert.equal(slider.pointerId, 7);
    slider.value = '95';
    const start = requests.length;
    slider.oninput();
    assert.equal(requests.length, start + 1); // No debounce/timer.
    assert.equal(requests[start].path, `/api/${axis}`);
    assert.equal(requests[start].method, 'POST');
    assert.equal(requests[start].angle, 95);
    context._updateServoSlider(axis, 85);
    assert.equal(slider.value, '95'); // Telemetry must not fight the drag.
    slider.value = '96'; slider.oninput();
    slider.value = '97'; slider.oninput();
    assert.equal(requests.length, start + 1); // No concurrent commands per axis.
    requests[start].resolve({ok: true}); await flush();
    assert.equal(requests.length, start + 2);
    assert.equal(requests[start + 1].angle, 97); // Intermediate position coalesced.
    slider.listeners.pointerup();
    context._updateServoSlider(axis, 88);
    assert.equal(slider.value, '97'); // Still waiting for the final request.
    requests[start + 1].resolve({ok: true}); await flush();
    context._updateServoSlider(axis, 94);
    assert.equal(slider.value, 94);
    assert.equal(display.textContent, '94°');
    for (const angle of [null, undefined, NaN, Infinity]) context._updateServoSlider(axis, angle);
    assert.equal(slider.value, 94); // No tilt reading on pan-only hardware.
    for (const release of ['change', 'pointercancel', 'lostpointercapture', 'blur']) {
      slider.listeners.pointerdown({pointerId: 8});
      slider.listeners[release]();
      context._updateServoSlider(axis, 91);
      assert.equal(slider.value, 91);
    }
    slider.value = '93'; slider.oninput(); // Keyboard input without pointerdown.
    slider.listeners.change();
    requests.at(-1).reject(new Error('Servo offline')); await flush();
    assert.equal(status.textContent, 'Servo offline');
    assert.equal(status.style.color, 'var(--red)');
    context._updateServoSlider(axis, 90);
    assert.equal(slider.value, 90); // Failure does not lock out telemetry.
    slider.value = '92'; slider.oninput();
    requests.at(-1).resolve({ok: true}); await flush();
    slider.listeners.change();
    assert.equal(status.textContent, '');
  }
  // Pan and tilt have independent command queues and drag state.
  elements['pan-slider'].value = '100'; elements['pan-slider'].oninput();
  context._updateServoSlider('tilt', 89);
  assert.equal(elements['tilt-slider'].value, 89);
  elements['tilt-slider'].value = '95'; elements['tilt-slider'].oninput();
  assert.equal(requests.at(-2).path, '/api/pan');
  assert.equal(requests.at(-1).path, '/api/tilt');
  requests.at(-2).resolve({ok: true}); requests.at(-1).resolve({ok: true});
  await flush();
}
run().catch(error => {console.error(error); process.exitCode = 1;});
"""
    runner = tmp_path / "servo-sliders.cjs"
    runner.write_text(script)
    subprocess.run(
        ["node", str(runner), str(STATIC / "app.js")],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for auto-recovery regression")
def test_dashboard_recovers_stale_websocket_and_frozen_streams(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
let clock = 1000000;
const sockets = [];
class FakeWS {
  constructor(url) { this.url = url; this.readyState = FakeWS.CONNECTING; sockets.push(this); }
  close() { this.readyState = FakeWS.CLOSED; if (this.onclose) this.onclose(); }
}
FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSED = 3;
let pixel = 1;
const canvasCtx = {
  drawImage() {},
  getImageData() { return {data: [pixel, pixel, pixel, 255]}; },
};
const listeners = {};
const imgs = {
  'camera-stream': {src: '', naturalWidth: 640, offsetParent: {}, style: {}, closest: () => null},
};
const timers = [];
let reloads = 0;
const context = vm.createContext({
  WebSocket: FakeWS,
  WS_URL: 'ws://vera/ws',
  VERA_API_KEY: 'k',
  Date: {now: () => clock},
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  setInterval: () => 0,
  console: {warn() {}, error() {}},
  location: {reload: () => { reloads += 1; }},
  setHealth: () => {},
  updateDashboard: () => {},
  document: {
    hidden: false,
    createElement: () => ({getContext: () => canvasCtx}),
    getElementById: id => imgs[id],
    addEventListener: (event, fn) => { listeners[event] = fn; },
  },
  window: {addEventListener: (event, fn) => { listeners[event] = fn; }},
});
vm.runInContext(
  'let ws = null; let wsRetryMs = 1000;\n' +
  source.slice(source.indexOf('let _wsLastMessageMs'), source.indexOf('// ── Room status polling')) +
  '\nthis.state = () => ({ws, wsRetryMs});', context);

// Fresh socket is left alone; a silent open socket is replaced.
context.connectWS();
const first = sockets[0];
first.readyState = FakeWS.OPEN; first.onopen();
first.onmessage({data: JSON.stringify({version: '1.0.0'})});
clock += 2000; context.ensureWSFresh();
assert.equal(sockets.length, 1);
clock += 6000; context.ensureWSFresh();
assert.equal(sockets.length, 2);
assert.equal(timers.length, 0); // replaced socket's onclose must not schedule a retry
assert.equal(context.state().ws, sockets[1]);

// A server restart onto a new version reloads the page to pick up new assets.
sockets[1].readyState = FakeWS.OPEN;
sockets[1].onmessage({data: JSON.stringify({version: '1.0.0'})});
assert.equal(reloads, 0);
sockets[1].onmessage({data: JSON.stringify({version: '1.0.1'})});
assert.equal(reloads, 1);

// Unexpected close backs off instead of hammering; resuming the tab reconnects now.
sockets[1].onclose();
assert.equal(timers.length, 1);
context.ensureWSFresh();
assert.equal(sockets.length, 2);

// A frozen MJPEG picture is reconnected with a cache-busting URL.
context._registerStream('camera-stream', '/stream');
const initial = imgs['camera-stream'].src;
assert.match(initial, /^\/stream\?key=k&t=\d+$/);
context.checkStreams();
clock += 10000; pixel = 2; context.checkStreams();
assert.equal(imgs['camera-stream'].src, initial);
clock += 16000; context.checkStreams();
assert.notEqual(imgs['camera-stream'].src, initial);

context.initAutoRecovery();
const beforeResume = imgs['camera-stream'].src;
clock += 1;
listeners.visibilitychange();
assert.notEqual(imgs['camera-stream'].src, beforeResume);
assert.equal(sockets.length, 3); // pending backoff skipped on resume
"""
    runner = tmp_path / "auto-recovery.cjs"
    runner.write_text(script)
    subprocess.run(
        ["node", str(runner), str(STATIC / "app.js")],
        check=True, capture_output=True, text=True,
    )


def test_decorative_codes_are_sparse_and_hidden_from_assistive_technology(layout):
    codes = [
        node for node in layout
        if "lcars-code" in node["attrs"].get("class", "").split()
    ]
    assert 3 <= len(codes) <= 6
    assert all(node["attrs"].get("aria-hidden") == "true" for node in codes)
    assert any("frame-code" in node["attrs"]["class"] for node in codes)
    assert any("section-code" in node["attrs"]["class"] for node in codes)


@pytest.mark.parametrize("workspace,controls", [
    ("controls", [
        "music-volume-slider", "music-eq-select", "audio-mute-btn",
        "audio-voice-gain-slider", "custom-eq-panel", "custom-eq-table",
        "music-station-select", "music-progress-bar", "podcast-query",
        "podcast-sub-select", "podcast-episode-select", "podcast-progress-bar",
    ]),
    ("perception", [
        "camera-stream", "camera-stream2", "pan-slider", "tilt-slider",
        "cam-rotation-slider", "cam2-rotation-slider",
        "cam-capture-resolution-select", "cam2-capture-resolution-select",
        "cam-stream-resolution-select", "cam2-stream-resolution-select",
        "face-table", "privacy-enabled", "object-detection-enabled",
        "room-checking-enabled", "tuning-panel", "depth-mono-enabled",
    ]),
    ("settings", [
        "servo-enabled", "face-tracking-enabled", "random-motion-enabled",
        "servo-travel-min", "servo-travel-max", "servo-pan-channel",
        "servo-tilt-channel", "tilt-travel-min", "tilt-travel-max",
    ]),
    ("voice", [
        "say-input", "record-seconds", "record-progress", "audio-input-gain-slider",
        "audio-spectrum-canvas", "voice-enabled", "voice-stt-backend",
        "voice-stt-command", "voice-wake-threshold", "voice-wake-cooldown",
        "greeting-cooldown", "greeting-confidence", "qh-enabled", "qh-start",
        "qh-end", "skills-panel", "stt-transcript-log",
    ]),
    ("system", [
        "update-settings", "audio-backend-select", "audio-devices-list",
        "xvf-tunable-rows", "fan-curve-tbody", "event-log",
        "btn-reboot", "btn-shutdown", "help-panel",
    ]),
    ("overview", [
        "stat-temp-case", "stat-cpu", "services-container", "processes-table",
        "stat-room", "stat-spoken", "repeat-spoken-btn",
    ]),
    ("smart-home", ["iot-toolbar", "iot-add-btn"]),
    ("home", [
        "card-home-alerts", "home-room", "home-faces", "home-alerts", "home-camera",
        "home-media-play", "home-media-next", "home-volume", "home-mute-btn",
        "home-toggle-privacy", "home-toggle-quiet", "home-toggle-mute",
        "home-toggle-face", "home-toggle-idle", "home-say-input", "home-repeat-btn",
        "home-iot", "home-temp-hailo", "home-fan", "home-scene-confidence", "home-eng-line",
    ]),
])
def test_controls_live_with_their_related_tasks(layout, workspace, controls):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    for control in controls:
        assert control in by_id, f"Missing control: {control}"
        assert any(
            ancestor["attrs"].get("id") == "tab-" + workspace
            for ancestor in by_id[control]["ancestors"]
        ), f"{control} is not in {workspace}"


def test_sidebar_frames_icon_navigation_like_lcars_elbow(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    tabs = [node for node in layout if node["attrs"].get("role") == "tab"]
    for tab in tabs:
        classes = [node["attrs"].get("class", "") for node in tab["ancestors"]]
        assert "sidebar-frame" in classes and "lcars-sidebar" in classes
        icons = [node for node in layout if node["tag"] == "svg" and tab in node["ancestors"]]
        assert len(icons) == 1 and icons[0]["attrs"]["aria-hidden"] == "true"
    assert by_id["rail-stardate"]["tag"] == "time"
    nav = next(node for node in layout if node["attrs"].get("role") == "tablist")
    assert nav["attrs"]["aria-labelledby"] == "rail-workspaces-label"
    css = (STATIC / "style.css").read_text()
    assert "border-left-width: var(--lcars-spine-width);" in css
    assert ".tab-btn.active { box-shadow: inset -6px 0 #fff; }" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for stardate regression")
def test_rail_stardate_uses_year_and_day_of_year(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
globalThis.window = {};
import(require('node:url').pathToFileURL(process.argv[2]).href).then(({railStardate}) => {
  assert.equal(railStardate(new Date(2026, 9, 23, 14, 0)), '2026.296');
  assert.equal(railStardate(new Date(2026, 0, 1, 0, 1)), '2026.001');
  assert.equal(railStardate(new Date(2024, 11, 31, 23, 59)), '2024.366');
  assert.equal(window.railStardate, railStardate);
}).catch(e => { console.error(e); process.exit(1); });
"""
    _run_module_test(tmp_path, script, JS / "core" / "router.js")


def test_tabs_have_accessible_matching_panels(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    tabs = [node for node in layout if node["attrs"].get("role") == "tab"]
    panels = [node for node in layout if node["attrs"].get("role") == "tabpanel"]
    assert len(tabs) == len(panels) == 8
    assert tabs[0]["attrs"]["id"] == "nav-home"
    for tab in tabs:
        panel = by_id[tab["attrs"]["aria-controls"]]
        assert panel["attrs"]["aria-labelledby"] == tab["attrs"]["id"]
        assert panel["attrs"]["data-title"]
        assert panel["attrs"]["data-description"]


def test_head_movement_sliders_and_configuration_are_separate(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    groups = []
    for axis in ("pan", "tilt"):
        slider = by_id[f"{axis}-slider"]
        group = slider["ancestors"][-2]
        groups.append(group)
        assert "control-group" in group["attrs"]["class"].split()
        assert "head-movement-sliders" in group["ancestors"][-1]["attrs"]["class"].split()
        assert any(node["attrs"].get("id") == "card-controls" for node in slider["ancestors"])
    assert groups[0] is not groups[1]
    settings = by_id["card-head-movement-settings"]
    assert settings["attrs"]["aria-label"] == "Head Movement Settings"
    for identity in (
        "servo-enabled", "face-tracking-enabled", "random-motion-enabled",
        "servo-travel-min", "servo-travel-max", "servo-pan-channel",
        "servo-tilt-channel", "tilt-travel-min", "tilt-travel-max",
    ):
        assert settings in by_id[identity]["ancestors"]
    assert "hidden" in by_id["tilt-controls"]["attrs"]
    assert "hidden" in by_id["tilt-limit-controls"]["attrs"]
    source = (STATIC / "app.js").read_text()
    for identity in ("tilt-controls", "tilt-limit-controls"):
        assert f'el("{identity}").hidden = !d.tilt_available;' in source
    css = (STATIC / "style.css").read_text()
    assert "#card-controls, #card-head-movement-settings { grid-column: 1 / -1; }" in css
    assert re.search(
        r"\.head-movement-sliders\s*\{[^}]*display:\s*grid;[^}]*"
        r"grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);", css,
    )


def test_existing_event_handlers_still_resolve(layout):
    source = "\n".join(path.read_text() for path in [STATIC / "app.js", *JS.rglob("*.js")])
    for node in layout:
        for name, handler in node["attrs"].items():
            if name.startswith("on") and handler:
                function = re.match(r"^(\w+)\(", handler)
                if function:
                    assert re.search(r"\bfunction\s+" + function[1] + r"\s*\(", source), handler


def test_custom_eq_and_hardware_details_are_collapsed_not_removed(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    assert by_id["custom-eq-panel"]["tag"] == "details"
    assert "open" not in by_id["custom-eq-panel"]["attrs"]
    for identity in ("audio-backend-select", "xvf-tunable-rows", "voice-stt-backend"):
        assert any(node["tag"] == "details" for node in by_id[identity]["ancestors"])


def test_asset_cache_keys_match_current_release(layout):
    version = (ROOT / "VERSION").read_text().strip()
    # Loaded assets only; plain <a> links (e.g. credits) are navigation, not assets.
    assets = [node["attrs"].get("src", node["attrs"].get("href", "")) for node in layout if node["tag"] != "a"]
    assert f"/static/style.css?v={version}" in assets
    assert f"/static/app.js?v={version}" in assets
    assert f"/static/js/main.js?v={version}" in assets
    main = (JS / "main.js").read_text()
    assert "new URL(import.meta.url).search" in main
    # Sub-modules must inherit main.js's ?v= key instead of using static imports.
    assert not re.search(r"^import ", main, re.M)
    for module in JS.rglob("*.js"):
        assert not re.search(r"^import .* from ['\"]\.", module.read_text(), re.M), module
    external = [url for url in assets if url.startswith(("https://", "http://"))]
    assert all(url.startswith(("https://fonts.googleapis.com", "https://fonts.gstatic.com"))
               for url in external)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for navigation regression")
def test_hash_router_defaults_home_migrates_legacy_routes_and_supports_keyboard(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
const ids = ['home', 'overview', 'perception', 'controls', 'voice', 'smart-home', 'system', 'settings'];
function node(id) {
  const classes = new Set();
  return {id, attrs: {}, dataset: {}, classList: {
    add: k => classes.add(k), remove: k => classes.delete(k),
    toggle: (k,v) => v ? classes.add(k) : classes.delete(k),
    contains: k => classes.has(k)},
    setAttribute(k,v) {this.attrs[k]=v;},
    focus() {focused = this;}, scrollIntoView() {}, querySelector() {return null;},
    closest() {return null;}};
}
let focused;
const panes = ids.map(id => Object.assign(node('tab-'+id), {dataset:{title:id,description:id+' description'}}));
const buttons = ids.map(id => Object.assign(node('nav-'+id), {dataset:{tab:id}}));
const title = node('workspace-title'), description = node('workspace-description'), target = node('card-quiet');
const frameCode = {textContent: '47-0821 / 1701'};
const fixedCode = {textContent: '308-72'};
let keydown;
const listeners = {};
const routes = [];
const values = {'vera-active-tab': 'voice'};
Object.assign(globalThis, {
  document: {
    body: {dataset: {}},
    getElementById: id => [...panes,...buttons,title,description,target].find(n=>n.id===id),
    querySelectorAll: q => q==='.tab-pane' ? panes : q==='.tab-btn' ? buttons
      : q==='.frame-code' ? [frameCode] : [frameCode, fixedCode],
    querySelector: q => q==='.tab-pane.active' ? panes.find(p=>p.classList.contains('active'))
      : {addEventListener: (_,handler) => {keydown=handler;}},
  },
  localStorage: {getItem:k=>values[k], setItem:(k,v)=>{values[k]=v;}, removeItem:k=>{delete values[k];}},
  location: {hash: process.argv[3]},
  history: {
    pushState(_s,_t,hash) {location.hash = hash; history.pushes++;},
    replaceState(_s,_t,hash) {location.hash = hash;}, pushes: 0,
  },
  window: {
    scrollTo() {},
    addEventListener: (type, handler) => {listeners[type] = handler;},
    dispatchEvent: event => { if (event.type === 'vera:route') routes.push(event.detail); },
  },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  setInterval() {},
});
const document = globalThis.document;
import(require('node:url').pathToFileURL(process.argv[2]).href).then(router => {
  router.initRouter();
  if (process.argv[3] === '') {
    // No hash: Home is the default and the stale last-tab preference is dropped.
    assert.equal(location.hash, '#/home');
    assert.equal(document.body.dataset.workspace, 'home');
    assert.equal(values['vera-active-tab'], undefined);
    assert.deepEqual(routes, [{tab: 'home', previous: null}]);
    return;
  }
  // Old "audio" bookmarks land on the media workspace with its public slug.
  assert.equal(location.hash, '#/media');
  assert.equal(document.body.dataset.workspace, 'controls');
  assert.equal(panes.filter(p=>p.classList.contains('active')).length, 1);
  assert.equal(buttons[3].attrs['aria-selected'], 'true');
  assert.equal(title.textContent, 'controls');
  assert.match(frameCode.textContent, /^\d{2}-\d{4} \/ \d{4}$/);
  assert.notEqual(frameCode.textContent, '47-0821 / 1701');
  const initialCode = frameCode.textContent;
  window.switchTab('controls');
  assert.equal(frameCode.textContent, initialCode);
  assert.equal(history.pushes, 0);
  keydown({key:'ArrowRight',target:buttons[3],preventDefault(){}});
  assert.notEqual(frameCode.textContent, initialCode);
  assert.equal(fixedCode.textContent, '308-72');
  assert.equal(location.hash, '#/voice');
  assert.equal(document.body.dataset.workspace, 'voice');
  assert.equal(focused, buttons[4]);
  assert.deepEqual(routes.at(-1), {tab: 'voice', previous: 'controls'});
  keydown({key:'End',target:buttons[4],preventDefault(){}});
  assert.equal(location.hash, '#/settings');
  assert.equal(buttons[7].attrs['aria-selected'], 'true');
  keydown({key:'ArrowDown',target:buttons[7],preventDefault(){}});
  assert.equal(location.hash, '#/home');
  keydown({key:'End',target:buttons[0],preventDefault(){}});
  keydown({key:'Home',target:buttons[7],preventDefault(){}});
  assert.equal(document.body.dataset.workspace, 'home');
  window.switchTab('no-longer-exists');
  assert.equal(location.hash, '#/home');
  window.switchTab('perception');
  assert.equal(location.hash, '#/vision');
  // Browser back/forward changes the hash and fires hashchange.
  location.hash = '#/smart-home';
  listeners.hashchange();
  assert.equal(document.body.dataset.workspace, 'smart-home');
  location.hash = '#/audio';
  listeners.hashchange();
  assert.equal(location.hash, '#/media');
  location.hash = '#/bogus';
  listeners.hashchange();
  assert.equal(document.body.dataset.workspace, 'home');
  assert.equal(location.hash, '#/home');
  window.openWorkspace('voice', 'card-quiet');
  assert.equal(focused, target);
  assert.equal(buttons.filter(b=>b.tabIndex===0).length, 1);
}).catch(e => { console.error(e); process.exit(1); });
"""
    for initial_hash in ("#/audio", ""):
        _run_module_test(tmp_path, script, JS / "core" / "router.js", initial_hash)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for interface mode regression")
def test_interface_mode_persists_and_engineering_requires_confirmation(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
const values = {'vera-ui-mode': 'bogus'};
const buttons = ['normal', 'advanced', 'engineering'].map(mode => ({
  dataset: {modeOption: mode}, attrs: {}, setAttribute(k, v) {this.attrs[k] = v;},
  addEventListener(_t, handler) {this.click = handler;},
}));
const events = [];
let prompts = 0, answer = false;
Object.assign(globalThis, {
  document: {body: {dataset: {}}, querySelectorAll: () => buttons},
  localStorage: {getItem: k => values[k], setItem: (k, v) => {values[k] = v;}},
  window: {confirm: () => {prompts++; return answer;}, dispatchEvent: e => events.push(e.detail.mode)},
  CustomEvent: class { constructor(type, init) { this.detail = init.detail; } },
});
import(require('node:url').pathToFileURL(process.argv[2]).href).then(mode => {
  mode.initMode();
  assert.equal(document.body.dataset.mode, 'normal');
  assert.equal(buttons[0].attrs['aria-pressed'], 'true');
  buttons[1].click();
  assert.equal(document.body.dataset.mode, 'advanced');
  assert.equal(values['vera-ui-mode'], 'advanced');
  assert.equal(prompts, 0);
  assert.ok(mode.modeAtLeast('advanced') && !mode.modeAtLeast('engineering'));
  buttons[2].click();
  assert.equal(prompts, 1);
  assert.equal(document.body.dataset.mode, 'advanced');
  assert.equal(buttons[1].attrs['aria-pressed'], 'true');
  answer = true;
  buttons[2].click();
  assert.equal(document.body.dataset.mode, 'engineering');
  assert.equal(values['vera-ui-mode'], 'engineering');
  buttons[2].click();
  assert.equal(prompts, 2);
  assert.equal(mode.setMode('bogus'), false);
  assert.deepEqual(events, ['normal', 'advanced', 'engineering', 'engineering']);
}).catch(e => { console.error(e); process.exit(1); });
"""
    _run_module_test(tmp_path, script, JS / "core" / "mode.js")
    css = (STATIC / "style.css").read_text()
    assert ('body:not([data-mode="advanced"]):not([data-mode="engineering"]) [data-min-mode="advanced"],\n'
            'body:not([data-mode="engineering"]) [data-min-mode="engineering"] { display: none !important; }') in css


def test_header_offers_three_interface_modes(layout):
    options = [node["attrs"]["data-mode-option"] for node in layout if "data-mode-option" in node["attrs"]]
    assert options == ["normal", "advanced", "engineering"]
    group = next(node for node in layout if "mode-toggle" in node["attrs"].get("class", "").split())
    assert group["attrs"]["role"] == "group"
    assert any(node["tag"] == "header" for node in group["ancestors"])


def test_mode_toggle_and_health_form_one_lcars_bar():
    css = (STATIC / "style.css").read_text()
    assert ".mode-toggle { display: flex; gap: var(--lcars-gap); }" in css
    assert ".mode-toggle button, header .badge {" in css
    assert '.mode-toggle button[aria-pressed="true"] { background: var(--lcars-peach); box-shadow: inset -6px 0 #fff; }' in css
    # Active segments use the rail buttons' end highlight, never an underline.
    assert "inset 0 -6px #fff" not in css
    assert "header .badge-ok { background: var(--green); }" in css
    assert "⬤" not in (STATIC / "app.js").read_text()


def test_home_toggles_are_lcars_lit_blocks_with_text_state(layout):
    toggles = [node for node in layout if "lcars-toggle" in node["attrs"].get("class", "").split()]
    assert len(toggles) == 5
    css = (STATIC / "style.css").read_text()
    assert ".lcars-toggle input:checked + .lcars-toggle-face { background: var(--lcars-ice); }" in css
    assert '.lcars-toggle-state::after { content: "OFF"; }' in css
    assert 'content: "ON"' in css


def test_home_flow_uses_named_grid_areas_and_title_links(layout):
    css = (STATIC / "style.css").read_text()
    assert '"camera media"' in css and '"camera toggles"' in css
    assert '#tab-home.has-alerts {' in css and '"alerts alerts"' in css
    links = {node["attrs"]["href"] for node in layout
             if "card-link" in node["attrs"].get("class", "").split()}
    assert {"#/vision", "#/media", "#/voice", "#/smart-home", "#/overview"} <= links


def test_home_status_moves_to_header_alert_frame_and_camera_top_bar(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    assert "card-home-status" not in by_id and "home-health" not in by_id
    # Health merges into the header badge.
    badge = by_id["health-badge"]
    assert badge in by_id["health-badge-value"]["ancestors"]
    # Alerts get their own frame that stays hidden until there's a message.
    frame = by_id["card-home-alerts"]
    assert "lcars-alert-frame" in frame["attrs"]["class"].split() and "hidden" in frame["attrs"]
    assert frame in by_id["home-alerts"]["ancestors"]
    # Room and people are read-only readouts in the camera's top sweep bar.
    camera = by_id["card-home-camera"]
    for value_id in ("home-room", "home-faces"):
        ancestors = by_id[value_id]["ancestors"]
        assert camera in ancestors
        assert any("lcars-sweep-bar-top" in a["attrs"].get("class", "").split() for a in ancestors)
        assert not any(a["tag"] == "button" for a in ancestors)
    css = (STATIC / "style.css").read_text()
    assert ".lcars-alert-frame[hidden] { display: none; }" in css
    assert ".home-camera { position: absolute; inset: 0;" in css
    assert "calc(var(--sweep-bar) / 2)" not in css  # sweep bars end square
    home = (JS / "pages" / "home.js").read_text()
    assert "frame.hidden = alerts.length === 0" in home and "All clear" not in home


def test_lcars_design_guide_is_referenced_by_agent_instructions():
    root = STATIC.parents[2]
    guide = root / "docs" / "ui" / "LCARS_DESIGN_GUIDE.md"
    assert guide.exists() and "## 7. Checklist" in guide.read_text()
    scoped = (root / ".github" / "instructions" / "lcars-ui.instructions.md").read_text()
    assert 'applyTo: "src/web/**"' in scoped and "LCARS_DESIGN_GUIDE.md" in scoped
    assert "docs/ui/LCARS_DESIGN_GUIDE.md" in (root / ".github" / "copilot-instructions.md").read_text()


def test_home_camera_has_lcars_bottom_sweep_with_camera_switch(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    card = by_id["card-home-camera"]
    assert "lcars-sweep-card" in card["attrs"]["class"].split()
    buttons = [node for node in layout if "data-home-cam" in node["attrs"]]
    assert [b["attrs"]["data-home-cam"] for b in buttons] == ["1", "2"]
    for button in buttons:
        assert button["tag"] == "button" and "aria-pressed" in button["attrs"]
        assert any("lcars-sweep-bar" in a["attrs"].get("class", "").split() for a in button["ancestors"])
        assert card in button["ancestors"]
    breaks = [node for node in layout if "frame-break" in node["attrs"].get("class", "").split()]
    assert breaks and all(node["attrs"].get("aria-hidden") == "true" for node in breaks)
    assert not any("image-frame" in node["attrs"].get("class", "") for node in layout)
    home = (JS / "pages" / "home.js").read_text()
    assert "2: '/stream2'" in home and "localStorage.setItem(CAMERA_KEY" in home


def test_card_drag_and_drop_is_retired(layout):
    assert not any("drag-handle" in node["attrs"].get("class", "") for node in layout)
    source = (STATIC / "app.js").read_text()
    assert "initCardDragDrop" not in source and "drag-handle" not in source
    assert "function _forgetCardOrder()" in source
    assert ".drag-handle" not in (STATIC / "style.css").read_text()


def test_home_gates_detail_by_interface_mode(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    assert by_id["card-home-thermal"]["attrs"]["data-min-mode"] == "advanced"
    assert by_id["card-home-engineering"]["attrs"]["data-min-mode"] == "engineering"
    for card in ("card-home-alerts", "card-home-camera", "card-home-media",
                 "card-home-toggles", "card-home-say", "card-home-environment"):
        assert "data-min-mode" not in by_id[card]["attrs"], card


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for Home regression")
def test_home_alerts_and_quiet_hours_window(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
globalThis.document = {getElementById: () => null};
globalThis.window = {};
import(require('node:url').pathToFileURL(process.argv[2]).href).then(({computeAlerts, quietHoursActive}) => {
  const at = (h, m) => new Date(2026, 0, 1, h, m);
  const overnight = {enabled: true, start: '21:00', end: '06:00'};
  assert.equal(quietHoursActive(overnight, at(22, 0)), true);
  assert.equal(quietHoursActive(overnight, at(5, 59)), true);
  assert.equal(quietHoursActive(overnight, at(6, 0)), false);
  assert.equal(quietHoursActive({...overnight, enabled: false}, at(22, 0)), false);
  assert.equal(quietHoursActive({enabled: true, start: '09:00', end: '17:00'}, at(12, 0)), true);
  assert.equal(quietHoursActive({enabled: true, start: '09:00', end: '17:00'}, at(18, 0)), false);

  const calm = {status: {healthy: true, services: {av: 'running'}, last: {
    'thermal.temp': {ok: true, cpu_celsius: 60}, 'thermal.hailo': {ok: true, celsius: 45}}},
    update: {installed_version: '1.0.0', available_version: '1.0.0'},
    settings: {privacy: false, quiet: overnight, muted: false}};
  assert.deepEqual(computeAlerts(calm, at(12, 0)), []);

  const busy = {status: {healthy: false, services: {av: 'running', music: 'error', iot: 'stopped'}, last: {
    'thermal.temp': {ok: true, cpu_celsius: 80}, 'thermal.hailo': {ok: false}}},
    update: {installed_version: '1.0.0', available_version: '1.1.0'},
    settings: {privacy: true, quiet: overnight, muted: true}};
  const texts = computeAlerts(busy, at(23, 0)).map(a => a.text);
  assert.deepEqual(texts, ['System degraded', 'SoC hot 80 °C', 'Hailo unavailable',
    'Service music error, iot stopped', 'Update 1.1.0 available', 'Privacy on', 'Quiet hours', 'Muted']);
  const hotHailo = {...calm, status: {...calm.status, last: {'thermal.hailo': {ok: true, celsius: 90}}}};
  assert.deepEqual(computeAlerts(hotHailo, at(12, 0)).map(a => a.text), ['Hailo hot 90 °C']);
}).catch(e => { console.error(e); process.exit(1); });
"""
    _run_module_test(tmp_path, script, JS / "pages" / "home.js")


def test_home_reuses_app_events_and_streams_camera_only_while_visible():
    source = (STATIC / "app.js").read_text()
    for event in ("vera:status", "vera:music", "vera:podcast", "vera:update"):
        assert re.search(r"CustomEvent\(['\"]" + re.escape(event) + r"['\"]", source), event
    assert "function _unregisterStream(id)" in source
    home = (JS / "pages" / "home.js").read_text()
    assert "window._registerStream('home-camera', CAMERA_STREAMS[homeCam])" in home
    assert "_unregisterStream('home-camera')" in home
    assert "innerHTML" not in home


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for Home regression")
def test_home_camera_switch_streams_selected_camera_only_while_visible(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
const listeners = {}, registered = [], store = {};
const el = id => ({id, attrs: {}, alt: '', textContent: '', dataset: {}, style: {setProperty() {}},
  replaceChildren() {}, append() {},
  setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
  addEventListener() {}, classList: {toggle() {}, add() {}, remove() {}}});
const nodes = {};
const buttons = ['1', '2'].map(n => Object.assign(el('cam' + n), {dataset: {homeCam: n}}));
globalThis.document = {
  getElementById: id => (nodes[id] ||= el(id)),
  querySelectorAll: sel => sel === '[data-home-cam]' ? buttons : [],
  createElement: () => el('x'), hidden: false,
};
globalThis.localStorage = {getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; }};
globalThis.window = {
  addEventListener: (type, fn) => { listeners[type] = fn; },
  _registerStream: (id, path) => registered.push([id, path]),
  _unregisterStream: id => registered.push([id, null]),
};
globalThis.fetch = async () => ({ok: true, json: async () => ({})});
import(require('node:url').pathToFileURL(process.argv[2]).href).then(({initHome, selectCamera}) => {
  initHome();
  selectCamera(2);
  assert.deepEqual(registered, [], 'no stream while Home is hidden');
  assert.equal(store['vera-home-cam'], '2');
  assert.equal(buttons[1].attrs['aria-pressed'], 'true');
  assert.equal(buttons[0].attrs['aria-pressed'], 'false');
  listeners['vera:route']({detail: {tab: 'home'}});
  assert.deepEqual(registered.at(-1), ['home-camera', '/stream2']);
  selectCamera(1);
  assert.deepEqual(registered.at(-1), ['home-camera', '/stream']);
  listeners['vera:route']({detail: {tab: 'media', previous: 'home'}});
  assert.deepEqual(registered.at(-1), ['home-camera', null]);
  const count = registered.length;
  selectCamera(2);
  assert.equal(registered.length, count, 'switching while hidden must not start a stream');
  process.exit(0);
}).catch(err => { console.error(err); process.exit(1); });
"""
    _run_module_test(tmp_path, script, JS / "pages" / "home.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for EQ regression")
def test_collapsed_eq_syncs_and_custom_selection_opens_editor(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const panel = {style: {display: 'block'}, open: false};
const selector = {value: 'custom'};
const elements = {'custom-eq-panel': panel, 'music-eq-select': selector};
let preset = 'bass_boost', loaded = 0;
const writes = [];
const context = vm.createContext({
  el: id => elements[id] || (elements[id] = {style: {}}),
  _applyMusicState() {}, _applyMusicSong() {}, _applyMusicStations() {},
  loadCustomEq: async () => {loaded++;},
  fetch: async (url, options) => {
    if (options) writes.push(JSON.parse(options.body));
    return {json: async () => ({eq_preset: preset})};
  },
});
vm.runInContext(source.slice(source.indexOf('async function loadMusicStatus()'),
  source.indexOf('function _applyMusicState(')), context);
vm.runInContext(source.slice(source.indexOf('async function musicSetEq('),
  source.indexOf('async function loadCustomEq(')), context);
async function run() {
  await context.loadMusicStatus();
  assert.equal(selector.value, 'bass_boost');
  assert.equal(panel.style.display, 'none');
  assert.equal(panel.open, false);
  preset = 'custom';
  await context.loadMusicStatus();
  assert.equal(selector.value, 'custom');
  assert.equal(panel.style.display, 'block');
  assert.equal(panel.open, false);
  assert.equal(loaded, 1);
  await context.musicSetEq('custom');
  assert.equal(panel.open, true);
  assert.equal(loaded, 2);
  assert.equal(writes.length, 0);
  preset = 'flat';
  await context.loadMusicStatus();
  assert.equal(selector.value, 'custom');
  assert.equal(loaded, 2);
  await context.musicSetEq('flat');
  assert.equal(panel.open, false);
  assert.equal(panel.style.display, 'none');
  assert.equal(writes[0].preset, 'flat');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
"""
    runner = tmp_path / "eq.cjs"
    runner.write_text(script)
    subprocess.run(
        ["node", str(runner), str(STATIC / "app.js")],
        check=True, capture_output=True, text=True,
    )
