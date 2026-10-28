// Entry point for the dashboard's ES modules. Sub-modules are imported with
// this script's ?v= query so a release bump busts every module's cache.
const version = new URL(import.meta.url).search;

const [router, mode, units, home, eggs] = await Promise.all([
  import(`./core/router.js${version}`),
  import(`./core/mode.js${version}`),
  import(`./core/units.js${version}`),
  import(`./pages/home.js${version}`),
  import(`./core/eggs.js${version}`),
]);

mode.initMode();
units.initUnits();
// Classic-script app.js reads this for Overview telemetry.
window.veraUnits = units;
home.initHome({ units });
// Before the router, which rewrites unknown slugs (e.g. #/holodeck) to Home.
eggs.initEggs();
router.initRouter();
