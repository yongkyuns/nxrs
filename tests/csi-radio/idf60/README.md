# IDF 6.0 radio reference

This isolated fixture runs on the official IDF 6.0 SDK/runtime, using the same
Rust capture admission, SPSC handoff, RF progress and TCP echo implementations
as the NuttX fixture. It links the pinned 6.0 ESP32-S3 gain-control archive.
It does not run a presence classifier or force gain.

Rust owns station acquisition/rollback, event observations, capture epochs,
bounded copying, deadlines, native numerical calls, traffic and teardown.
`main/sdk.c` translates target headers, registers actual SDK callbacks and calls
native SDK operations. The controller uses `std::net` directly. SDK event and
capture callback storage survives late delivery; uncertain callback retirement
poisons the session and retains SDK resources. The global IDF network runtime
survives between trials; `sdk_deinitialized` refers to the Wi-Fi driver, not
every SDK system task.

## Build only

Use a separate checkout of IDF commit
`662a3be354759d9487bf4b1a629fadb766cb1800` (tag `v6.0`) with its pinned
submodules. Install its recommended tools with `tools/idf_tools.py` into a
private `IDF_TOOLS_PATH`; do not replace the running camera's toolchain or
Python environment. This release selects Xtensa `esp-15.2.0_20251204`.
Its Python environment needs the release-constrained IDF requirements and
`pyelftools`; `cmake` and `ninja` must be on `PATH`.

```sh
python tests/csi-radio/idf60/build.py \
  --idf /private/scratch/esp-idf-v6.0 \
  --tools /private/scratch/idf60-tools \
  --python /private/scratch/idf60-python/bin/python \
  --rust-sysroot /private/scratch/qualified-rust-toolchain \
  --gain-package /private/scratch/esp_csi_gain_ctrl-0.1.5.zip \
  --out "$PWD/target/csi-radio-idf60"
```

The build verifies all four reference radio archives, stages only the pinned
gain archive/header/license, checks resolved resources, compares 51 compiled
Rust/C ABI values, and records source/configuration/compiler/image provenance.
It creates `reference-flash.bin` at offset zero and `reference-build.json`.
It never opens a device or flashes firmware.

The reference uses an isolated copy of Rust standard-library sources. The
selected libc's newlib bindings over-align Xtensa pthread mutex/condition/rwlock
storage to eight bytes; IDF 6.0 declares four-byte handles. `libc.py` corrects
only the copied ESP-IDF bindings and supplies the missing `POLLNVAL` constant.
The build rejects disagreement with native synchronization layouts,
initializers, clock, socket, poll and error constants. Neither the shared NuttX
sysroot nor production Nxrs sources are modified. These checks qualify the
fixture's used ABI surface, not all IDF libc APIs.

## Bounded physical protocol

`run.py` supplies `Client(console).qualify(host_ip, ssid, password, seconds,
capture)` for an explicitly opened, already-flashed device. It does not reset,
discover or flash hardware. The caller owns board identification, backup and
restoration. USB serial settings must keep DTR/RTS low and take exclusive
ownership. Credentials stay in memory and are sent only after an exact mode,
duration and destination-port acknowledgement, using a raw USB driver reader
without terminal echo or shell history. They are never build inputs.

Each trial lasts 30–120 seconds. `capture=None` is transport-only; a capture
path must be private and outside the repository. The same fixed 16-byte TCP
records are scheduled every 100 ms, with 32 outstanding slots and two-second
echo/RF deadlines. Capture samples raw output at no more than 10 Hz and drains
at most eight owned frames per controller iteration. Excess/malformed callback
work is a failure. Numerical calls run outside the Wi-Fi callback.

The host requires complete duration/count evidence, no echo or callback queue loss, a confirmed TCP
half-close/peer EOF, callback retirement, station disconnect and native Wi-Fi
deinitialization. A transport-only pass cannot qualify CSI. Private AP identity
evidence lets a maintenance harness select the same explicit BSSID for NuttX,
check its reported association relation, and reject reference AP/channel
changes. Remove `private_peer` and raw RF from every public result.

For a camera maintenance window, verify/copy the full original flash before
the first write and restore it on every outcome. Verify restored hashes and
decode fresh real frames through the existing gateway. Preserve failed trials.
Use adjacent NuttX / IDF / NuttX captures on the same board and AP/channel to
observe temporal drift; independent over-the-air frames are not bytewise pairs.
Automatic-gain distribution agreement is diagnostic evidence. Without calibrated
signal levels or vendor confirmation, it does not prove private gain units,
physical force-gain behavior or presence accuracy.

The [2026-10-23 physical comparison](../results/esp32s3-idf60-reference-2026-10-23.json)
passed 60- and 120-second IDF capture windows bracketed by 60-second NuttX
controls. All captured 128-byte CSI samples on channel 6; no callback queue
drop or missing echo occurred. The full original firmware and live real camera
frames were verified after each maintenance attempt. The report retains an
initial host credential-format failure before command exchange. Full presence
processing remains disconnected.

## Host controls

```sh
cargo test --manifest-path tests/csi-radio/idf60/Cargo.toml --offline --lib
python -m unittest discover -s tests/host -p test_csi_idf60.py -v
```
