# Motion Tracker browser demo

The static demo boots the production Motion Tracker in two-core NuttX on
patched ESP-EMU. Drag down from the top bar of the guest display and tap
**PAIR**. The browser worker decodes the guest's panel pixels with jsQR, connects
through its simulated H4 controller, discovers the GATT service, and runs a
Mbed TLS Wasm client using the QR-derived PSK. The page shows samples received
through the authenticated link. **Restart** creates a fresh guest and peer.
Open **Serial console**, enter an NSH command such as `help`, and press
**Enter** or select **Send**. The demo starts the tracker in a separate
NuttX task so the shell stays available over UART0.

Serve the generated directory through HTTPS or `127.0.0.1` for Web Crypto.
The included static server runs on Node.js. The ESP-EMU machine, GNSS model,
H4 controller, GATT central, QR decoder and TLS client run in the page's worker.
The board uses the QEMU qualification image's guest-side sensor and panel
models. The simulated link covers the tracker protocol with one central.
Physical BLE behavior and CoreS3 peripheral timing remain unqualified.

## Build and serve

Use the two-core build and ESP-EMU patch from the
[ESP-EMU pairing qualification](../esp32s3-preemption/README.md#motion-tracker-ble-pairing).
Build the demo image with `NXRS_TARGET_C_FLAGS=-DTRACKER_DEMO_AUTOSTART` and
`dashboard_init.c` so the tracker starts alongside NSH.
Use Emscripten, CMake and the Xtensa `nm` in `PATH`. The build verifies the
existing Mbed TLS 4.2.0 archive pin and the jsQR 1.4.0 npm archive pin. All
runtime dependencies are copied into the generated directory. [Mbed TLS 4.2.0](https://github.com/Mbed-TLS/mbedtls/tree/mbedtls-4.2.0) · [jsQR](https://github.com/cozmo/jsQR)

```sh
out=target/tracker-esp-emu
www=/path/to/esp-emu-1.2.0-wasm/www
curl -fL https://registry.npmjs.org/jsqr/-/jsqr-1.4.0.tgz -o "$out/jsqr-1.4.0.tgz"
bash tools/build-tracker-browser-demo.sh "$www" "$out/patched" \
  "$out/firmware/nuttx/nuttx" "$NXRS_TLS_ARCHIVE" "$out/jsqr-1.4.0.tgz" "$out/standalone"
node "$out/standalone/serve.mjs" "$out/standalone" 8785
```

Open `http://127.0.0.1:8785/`. Static hosting of the same directory also works.

## Verify in Chromium

With Node.js and Playwright installed, run:

```sh
node tests/browser/tracker-demo/verify.mjs "$out/standalone" "$out/standalone-chromium.json"
```

Pass a Chromium executable as the third argument to use an installed browser.
The test uses real pointer drags and clicks to switch guest tabs and start
pairing. It checks three advancing samples from both IMUs and GNSS with two
cores online, restarts and repeats the checks. It also
checks a command and its response over NSH UART0 during live telemetry,
then corrupts a decoded key and verifies that pairing fails with zero samples.
The verifier serves local static assets with Node.js and rejects external
requests and WebSockets. It writes a JSON report and a screenshot after
pairing. The pairing key stays in the worker and guest memory.

The optional final `SITE_ARCHIVE` argument to the packager writes the generated
dependencies used by the [NXRS tutorial](../../../website/guide/motion-tracker.md).
To test a rendered site, pass its base path as the fourth argument to
`verify.mjs`, for example `/nxrs/`.

To test the published tutorial, pass the HTTPS site root instead of a
directory. Chromium loads the deployed firmware and assets directly.

```sh
node tests/browser/tracker-demo/verify.mjs https://yongkyuns.github.io/nxrs/ "$out/pages-chromium.json"
```
