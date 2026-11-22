# Cross-platform service logging

Services emit diagnostics with the Rust `log` macros. Host startup installs
one logger before creating services. The observation service's configuration
record is the same in the desktop, browser and NuttX tests. Tracker
measurements continue through the typed observation stream.

## Windows and desktop

Call `logging::start_file(path, limit, level)` from host startup with a
writable path and byte limit. A worker drains up to 32 queued records to
the active file and keeps one rotated `.1` file. Call `FileLog::finish()` at
shutdown to drain pending records and obtain any writer error or the number
of dropped records. The native CI matrix runs the same check on Windows
and other desktop hosts.

```sh
cargo test --locked -p nxrs-log-qualification --test file
```

The Windows job also runs the production headless tracker with one mock IMU.
It checks fresh samples in the JSON stream and a clean exit. The rotating-file
test calls the observation service directly.

```sh
python tests/native/motion_tracker.py
```

## Browser

The browser test runs the Rust service as `wasm32-unknown-unknown` in a
dedicated worker. Open an Origin Private File System directory with
`openArchive`, then give the Wasm instance the `nxrs_logging.emit` import
shown in [worker.mjs](worker.mjs). The import copies each record into a
32-record queue. The worker writes and flushes the queue after Wasm returns.
`close()` finishes pending writes and reports drops or a storage error.
The origin owns the stored files, so clearing its site data removes them.

With Playwright and its Chromium browser installed, run the verifier below.
It reads both files from a fresh worker after a page reload.

```sh
rustup target add wasm32-unknown-unknown
cargo build --locked --release --lib -p nxrs-log-qualification --target wasm32-unknown-unknown
node tests/logging/verify-web.mjs \
  target/wasm32-unknown-unknown/release/nxrs_log_qualification.wasm target/logging-web.json
```

## NuttX

The headless QEMU profile starts a separate archive task at boot. Rust
records enter NuttX `syslog` and are saved alongside native OS and driver
messages. The profile uses a volatile filesystem. [QEMU build and verifier](../nuttx-std/tracker-sim/README.md#route-system-logs-to-a-rotating-file)
