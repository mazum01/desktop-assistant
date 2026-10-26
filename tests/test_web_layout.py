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
def test_rail_stardate_uses_year_and_day_of_year():
    source = (STATIC / "app.js").read_text().split("// ── Software updates")[0]
    script = source + r"""
const assert = require('node:assert/strict');
assert.equal(railStardate(new Date(2026, 9, 23, 14, 0)), '2026.296');
assert.equal(railStardate(new Date(2026, 0, 1, 0, 1)), '2026.001');
assert.equal(railStardate(new Date(2024, 11, 31, 23, 59)), '2024.366');
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_tabs_have_accessible_matching_panels(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    tabs = [node for node in layout if node["attrs"].get("role") == "tab"]
    panels = [node for node in layout if node["attrs"].get("role") == "tabpanel"]
    assert len(tabs) == len(panels) == 7
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
    source = (STATIC / "app.js").read_text()
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
    assets = [node["attrs"].get("src", node["attrs"].get("href", "")) for node in layout]
    assert f"/static/style.css?v={version}" in assets
    assert f"/static/app.js?v={version}" in assets
    external = [url for url in assets if url.startswith(("https://", "http://"))]
    assert all(url.startswith(("https://fonts.googleapis.com", "https://fonts.gstatic.com"))
               for url in external)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for navigation regression")
def test_navigation_migrates_old_audio_tab_and_supports_keyboard(tmp_path):
    source = (STATIC / "app.js").read_text().split("// ── Software updates")[0]
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ids = ['overview', 'perception', 'controls', 'voice', 'smart-home', 'system', 'settings'];
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
const document = {
  body: {dataset: {}},
  getElementById: id => [...panes,...buttons,title,description,target].find(n=>n.id===id),
  querySelectorAll: q => q==='.tab-pane' ? panes : q==='.tab-btn' ? buttons
    : q==='.frame-code' ? [frameCode] : [frameCode, fixedCode],
  querySelector: q => q==='.tab-pane.active' ? panes.find(p=>p.classList.contains('active'))
    : {addEventListener: (_,handler) => {keydown=handler;}},
};
const values = {'vera-active-tab':'audio'};
const localStorage = {getItem:k=>values[k],setItem:(k,v)=>{values[k]=v;}};
const context = vm.createContext({document, localStorage, setInterval() {}, window:{scrollTo() {}}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'), context);
vm.runInContext('initTabs()', context);
assert.equal(values['vera-active-tab'], 'controls');
assert.equal(document.body.dataset.workspace, 'controls');
assert.equal(panes.filter(p=>p.classList.contains('active')).length, 1);
assert.equal(buttons[2].attrs['aria-selected'], 'true');
assert.equal(title.textContent,'controls');
assert.match(frameCode.textContent, /^\d{2}-\d{4} \/ \d{4}$/);
assert.notEqual(frameCode.textContent, '47-0821 / 1701');
const initialCode = frameCode.textContent;
vm.runInContext("switchTab('controls')", context);
assert.equal(frameCode.textContent, initialCode);
keydown({key:'ArrowRight',target:buttons[2],preventDefault(){}});
assert.notEqual(frameCode.textContent, initialCode);
assert.equal(fixedCode.textContent, '308-72');
assert.equal(values['vera-active-tab'], 'voice');
assert.equal(document.body.dataset.workspace, 'voice');
assert.equal(focused,buttons[3]);
keydown({key:'End',target:buttons[3],preventDefault(){}});
assert.equal(values['vera-active-tab'],'settings');
assert.equal(document.body.dataset.workspace,'settings');
assert.equal(buttons[6].attrs['aria-selected'],'true');
assert.equal(title.textContent,'settings');
keydown({key:'ArrowDown',target:buttons[6],preventDefault(){}});
assert.equal(values['vera-active-tab'],'overview');
assert.equal(document.body.dataset.workspace,'overview');
keydown({key:'Home',target:buttons[0],preventDefault(){}});
assert.equal(values['vera-active-tab'],'overview');
vm.runInContext("switchTab('no-longer-exists')",context);
assert.equal(values['vera-active-tab'],'overview');
vm.runInContext("openWorkspace('voice','card-quiet')",context);
assert.equal(focused,target);
assert.equal(buttons.filter(b=>b.tabIndex===0).length,1);
"""
    navigation = tmp_path / "navigation.js"
    navigation.write_text(source)
    runner = tmp_path / "test.cjs"
    runner.write_text(script)
    subprocess.run(["node", str(runner), str(navigation)], check=True, capture_output=True, text=True)


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
