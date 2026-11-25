# Motion Tracker demo assets

`assets.tar.gz` supplies the generated files for the
[Motion Tracker tutorial](../../guide/motion-tracker.md). The site build
extracts a fixed set of assets and copies the current browser demo source.
It records the archive and firmware hashes in `build-report.json`.

The firmware was built with `esp32s3-esp-emu-tracker` and the H4 dashboard
startup, with `NXRS_TARGET_C_FLAGS=-DTRACKER_DEMO_AUTOSTART` to start the
tracker alongside NSH. The bundle records these inputs.

| Input | Revision or SHA-256 |
| --- | --- |
| NXRS application source revision | `982dd787381998021f718d0bb0316e870e2bc367` |
| NuttX | `2b5c0de5e933281c209f954a63b99cf10c132346` |
| NuttX Apps | `3c772ea35b9245972e18999828f7b533e6fc57cc` |
| Merged flash image | `fce48a7c625a31f1c289a5effdd1892159341fcfc3b8e8488560fc437cf793e9` |
| Patched ESP-EMU 1.2.0 Wasm | `e135483ec20bee5794b6aaa6f242642dd17dedf6a9a7cd2f8532959c3c9ac39d` |

Follow the [firmware and emulator build](../../../tests/browser/esp32s3-preemption/README.md#motion-tracker-ble-pairing)
and [browser package instructions](../../../tests/browser/tracker-demo/README.md).
Pass `website/examples/motion-tracker/assets.tar.gz` as the last argument to
`tools/build-tracker-browser-demo.sh` to refresh the assets. Verify the static
bundle and the generated site before publishing changes to the demo.
