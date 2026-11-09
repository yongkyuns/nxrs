# Noon visual playground

This page embeds **Move the whole diagram**, a published example from the separate [Noon animation project](https://github.com/yongkyuns/noon). It moves, scales, rotates and recolors a grouped diagram while an independent progress marker advances.

Load the player below, then use Noon’s own controls to run and inspect the scene. The page is intentionally not loaded automatically: it downloads its own WebAssembly and Python runtime and may use the browser GPU. The NXRS documentation itself does not require those facilities.

**This is a Noon browser demonstration, not NXRS firmware running in a browser, an MCU emulator, or a measurement of embedded performance.** Noon is not added to the firmware dependency graph or the documentation build.

The embed uses the published showcase URL and does not depend on a private player API. Its content follows Noon’s deployment and may evolve independently. Engine readiness is reported inside the player; an iframe load event alone does not prove that a scene rendered. A direct-open link remains available when embedding, graphics support or runtime downloads fail.

Read the [Python scene source](https://github.com/yongkyuns/noon/blob/master/web/python/examples/showcase_group_animation.py) and the [curated showcase manifest](https://github.com/yongkyuns/noon/blob/master/web/python/examples/noon_showcase_manifest.json) for the example’s declared scope. Use **Unload demo** when finished.
