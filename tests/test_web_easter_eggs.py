"""Star Trek easter eggs in the web UI (src/web/static/js/core/eggs.js)."""

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "web" / "static"
EGGS = STATIC / "js" / "core" / "eggs.js"


def _run_module_test(tmp_path, script):
    copy = tmp_path / "eggs.mjs"
    copy.write_text(EGGS.read_text())
    runner = tmp_path / "test.cjs"
    runner.write_text(script)
    result = subprocess.run(["node", str(runner), str(copy)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for the eggs module")
def test_konami_and_phrase_matching(tmp_path):
    script = r"""
const assert = require('node:assert/strict');
globalThis.document = {getElementById: () => null};
globalThis.window = {};
import(require('node:url').pathToFileURL(process.argv[2]).href).then(m => {
  const seq = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
    'ArrowLeft', 'ArrowRight', 'B', 'a'];
  let p = 0;
  for (const key of seq) p = m.konamiStep(p, key);
  assert.equal(p, m.KONAMI_LENGTH);
  // A wrong key resets; a fresh ArrowUp restarts at one.
  assert.equal(m.konamiStep(3, 'x'), 0);
  assert.equal(m.konamiStep(5, 'ArrowUp'), 1);

  assert.equal(m.matchPhrase('xxengage'), 'engage');
  assert.equal(m.matchPhrase('teaearlgreyhot'), null);
  assert.equal(m.matchPhrase('teaearlgrey'), 'earlgrey');
  // No phrase may contain another, or the shorter one fires first.
  const keys = Object.keys(m.PHRASES);
  for (const a of keys) for (const b of keys) {
    if (a !== b) assert.ok(!b.includes(a), `${a} shadows ${b}`);
  }
  assert.ok(keys.every(k => /^[a-z]+$/.test(k)));

  assert.match(m.holidayGreeting(new Date(2063, 3, 5)), /First Contact/);
  assert.match(m.holidayGreeting(new Date(2026, 8, 8)), /Star Trek Day/);
  assert.equal(m.holidayGreeting(new Date(2026, 9, 27)), null);
  assert.equal(m.matchPhrase('hello'), null);

  assert.equal(m.isTypingTarget({tagName: 'INPUT'}), true);
  assert.equal(m.isTypingTarget({tagName: 'DIV', isContentEditable: true}), true);
  assert.equal(m.isTypingTarget({tagName: 'BODY'}), false);
  assert.ok(m.STARDATE_LOG.includes('%s'));
}).catch(e => { console.error(e); process.exit(1); });
"""
    _run_module_test(tmp_path, script)


def test_route_eggs_never_shadow_real_pages():
    import re
    router = (STATIC / "js" / "core" / "router.js").read_text()
    block = router[router.index("const SLUG_TO_TAB"):router.index("};", router.index("const SLUG_TO_TAB"))]
    real = set(re.findall(r"^\s*'?([a-z-]+)'?:", block, re.M))
    eggs = (EGGS).read_text()
    egg_block = eggs[eggs.index("ROUTE_EGGS"):eggs.index("};", eggs.index("ROUTE_EGGS"))]
    egg_routes = set(re.findall(r"^\s*'?([a-z-]+)'?:", egg_block, re.M))
    assert egg_routes and not (egg_routes & real)


def test_eggs_are_loaded_before_router_and_styled():
    main = (STATIC / "js" / "main.js").read_text()
    assert "./core/eggs.js" in main
    assert main.index("eggs.initEggs()") < main.index("router.initRouter()")
    css = (STATIC / "style.css").read_text()
    for rule in (".lcars-comm {", "body.is-red-alert::after", "body.is-warp::after",
                 "body.is-beaming #main-content", "body.is-shields::after",
                 "body.is-cloaked #main-content", "body.is-torpedo::after", ".lcars-tribble {"):
        assert rule in css, rule
    eggs = EGGS.read_text()
    # Eggs stay cosmetic: no API calls, speech or settings changes.
    assert "fetch(" not in eggs and "/api/" not in eggs
