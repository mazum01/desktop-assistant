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
        "camera-stream", "camera-stream2", "pan-slider", "servo-enabled",
        "face-tracking-enabled", "random-motion-enabled", "servo-travel-min",
        "servo-travel-max", "cam-rotation-slider", "cam2-rotation-slider",
        "cam-capture-resolution-select", "cam2-capture-resolution-select",
        "cam-stream-resolution-select", "cam2-stream-resolution-select",
        "face-table", "privacy-enabled", "object-detection-enabled",
        "room-checking-enabled", "tuning-panel", "depth-mono-enabled",
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


def test_tabs_have_accessible_matching_panels(layout):
    by_id = {node["attrs"]["id"]: node for node in layout if "id" in node["attrs"]}
    tabs = [node for node in layout if node["attrs"].get("role") == "tab"]
    panels = [node for node in layout if node["attrs"].get("role") == "tabpanel"]
    assert len(tabs) == len(panels) == 6
    for tab in tabs:
        panel = by_id[tab["attrs"]["aria-controls"]]
        assert panel["attrs"]["aria-labelledby"] == tab["attrs"]["id"]
        assert panel["attrs"]["data-title"]
        assert panel["attrs"]["data-description"]


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
    assert not any(url.startswith(("https://", "http://")) for url in assets)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for navigation regression")
def test_navigation_migrates_old_audio_tab_and_supports_keyboard(tmp_path):
    source = (STATIC / "app.js").read_text().split("// ── Software updates")[0]
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ids = ['overview', 'perception', 'controls', 'voice', 'smart-home', 'system'];
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
let keydown;
const document = {
  body: {dataset: {}},
  getElementById: id => [...panes,...buttons,title,description,target].find(n=>n.id===id),
  querySelectorAll: q => q==='.tab-pane' ? panes : buttons,
  querySelector: q => q==='.tab-pane.active' ? panes.find(p=>p.classList.contains('active'))
    : {addEventListener: (_,handler) => {keydown=handler;}},
};
const values = {'vera-active-tab':'audio'};
const localStorage = {getItem:k=>values[k],setItem:(k,v)=>{values[k]=v;}};
const context = vm.createContext({document, localStorage, window:{scrollTo() {}}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'), context);
vm.runInContext('initTabs()', context);
assert.equal(values['vera-active-tab'], 'controls');
assert.equal(document.body.dataset.workspace, 'controls');
assert.equal(panes.filter(p=>p.classList.contains('active')).length, 1);
assert.equal(buttons[2].attrs['aria-selected'], 'true');
assert.equal(title.textContent,'controls');
keydown({key:'ArrowRight',target:buttons[2],preventDefault(){}});
assert.equal(values['vera-active-tab'], 'voice');
assert.equal(document.body.dataset.workspace, 'voice');
assert.equal(focused,buttons[3]);
keydown({key:'End',target:buttons[3],preventDefault(){}});
assert.equal(values['vera-active-tab'],'system');
keydown({key:'ArrowDown',target:buttons[5],preventDefault(){}});
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
