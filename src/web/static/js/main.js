// Entry point for the dashboard's ES modules. Sub-modules are imported with
// this script's ?v= query so a release bump busts every module's cache.
const version = new URL(import.meta.url).search;

const [router, mode, home] = await Promise.all([
  import(`./core/router.js${version}`),
  import(`./core/mode.js${version}`),
  import(`./pages/home.js${version}`),
]);

mode.initMode();
home.initHome();
router.initRouter();
