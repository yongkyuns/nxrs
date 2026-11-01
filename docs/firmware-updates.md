# Signed firmware updates

The camera's `platform/nuttx/home-cam/build.sh` defaults to
`esp32s3-camera-stream-startup`: a signed MCUboot camera/TLS/PICO image with
TLS autostart and trial confirmation. Unsigned qualification builds require an
explicit `--platform`; missing signing inputs fail before building.
Its two 2 MiB slots and 256 KiB scratch region fit the 16 MiB camera flash.
It inherits the existing camera
configuration without copying its HAL implementations or resource budgets.
The pinned Espressif port reserves swap status for 512 sectors; signing includes
that trailer budget and rejects an image that no longer fits.

Install the NuttX-pinned upstream tool with `pip install -r tools/mcuboot-requirements.txt`.
The PyPI 2.2.0 release lacks the pinned source's EC public-key verification fix. Supply
`NXRS_FIRMWARE_SIGNING_KEY`, `NXRS_FIRMWARE_VERIFY_KEY` (the expected public root),
`NXRS_FIRMWARE_VERSION` and `NXRS_FIRMWARE_SECURITY_COUNTER`, then run:

```sh
bash platform/nuttx/home-cam/build.sh --out /private/tmp/camera-mcuboot
```

The resulting `nuttx/nuttx.signed.bin` is signed and independently verified
before publication. NuttX's intermediate `nuttx.bin` has no signature and is
not the release artifact. Signing keys remain operator-owned inputs; no key is
generated, embedded in source or included in the firmware. Update images contain
only the signed container, without slot padding or boot-state trailers. The
updater must bound writes before the trailer and request a trial through MCUboot
after staging completes. The standalone `tools/mcuboot_image.py` supports
`--confirm` to pad an initial factory image and initialize its confirmed trailer.
Product profiles supply a stable `image-class` UUID, independent of release
version. Signing places it in MCUboot's protected class-ID TLV; the loader uses
upstream parsing/authentication and a fixed product-ID callback. Missing or
different classes are refused, even with a valid signature from the same root.
Protected and unprotected builds have distinct classes because their flash
formats differ. Factory preparation also requires a matching class. Older,
classless images need a newly signed matching factory/candidate set before using
this loader. Keep signing authorities separate where products have different
trust requirements.

MCUboot compares the candidate's signed security counter with the active image's
counter before a trial swap. With a readable active counter, missing or lower
candidate counters are refused; equal counters allow an older recovery release
within the same security epoch. Factory preparation requires one protected
32-bit counter, avoiding upstream's counterless-primary migration behavior.
An unconfirmed trial may still revert. Confirmation retains the newer image and
its counter for subsequent admissions. This uses authenticated image state, not
an eFuse counter; it does not prevent replay of an entire external-flash snapshot.

MCUboot owns image authentication, swapping and recovery. `hal/boot` exposes
`state`, `stage_update`, `request_trial` and `confirm`. Shared Rust admits staging
only from a stable image, stable-to-pending trial requests, and trial-to-confirmed
transitions. Failures remain sticky for that owner, including a staging reader
unwind. The NuttX
provider holds one exclusive lease while calling synchronous upstream SDK
operations. Application control links MCUboot's flash/trailer helpers, without
another verifier or crypto library. Its NuttX flash descriptors use direct,
synchronous NOR writes so setting a trailer flag does not erase neighboring
recovery state through the default MTD translation layer. The host regression
compiles NuttX's actual direct-write function against a NOR/power-cut model;
whole-device interrupted-update qualification is still required.
The SDK erase helper uses the native partition erase ioctl, with aligned,
bounded ranges and cached-read invalidation even after failure. Writing `0xff`
through a direct descriptor is not a physical erase.
Native flash partitions follow the product
layout; filesystem auto-mounting stays disabled.
Encrypted OTA selects NuttX's encryption-capable flash driver independently of
PSRAM and partition-table use. Ordinary unencrypted builds keep their existing
driver selection; no extra memory or flash operation is introduced.

`stage_update` erases only the inactive slot and streams an exact declared length,
using two 256-byte transfer buffers. It reserves the upstream-calculated trailer,
pads the last write to the provider's program unit, and verifies each write. Short,
extra or failed input cannot complete staging or activate an image through that
owner. Staging leaves the running image stable; the caller separately requests a
trial after success. The source owns I/O deadlines; MCUboot enforces the signed
target class before execution. Startup health remains caller policy. Confirm
only after startup health passes.
The stream snapshot records the latest fully written image's sequence, capture
time and completion time, so startup policy can reject stale delivery. It
proves transport write completion, not receipt or decoding on the phone.
The separate `hal/watchdog` resource can adopt an already-running native reset
watchdog without feeding or restarting its deadline. Drop and startup failure
leave it running; explicit cancellation verifies stopped state and retains
uncertain failures. The unencrypted `esp32s3-camera-stream-startup`
profile adopts the inherited deadline; other profiles retain
their existing handoff. Protected runtime control remains closed pending joined
startup/recovery and physical quiescence qualification.
The native `boot-control` fixture exercises SDK state, trial and confirmation,
exclusive acquisition and repeated-query descriptor cleanup; it is not an OTA
service or automatic startup confirmation policy. Product wiring owns layout.
The packager uses upstream signing/verification and explicit geometry, independent
of the ESP32-S3 boot ROM.

Build the matching signature-enforcing second stage with:

```sh
python3 tools/build-mcuboot.py --platform esp32s3-camera-stream-startup \
  --mcuboot-repo /path/to/mcuboot --hal-repo /path/to/esp-hal-3rdparty \
  --root /path/to/public-root.pem --out /private/tmp/camera-bootloader
```

The source repositories must contain the NuttX-pinned commits declared in
`platform/nuttx/mcuboot.py`. The builder exports those exact Git objects into
a fresh output directory and builds upstream's EC P-256 verifier with TinyCrypt.
Only a public key is accepted; the signing key is unnecessary on the boot build
host. `bootloader.json` records the pins, geometry, public-root and binary hashes.
The bootloader and startup camera diagnostics use UART0; explicit legacy USB
qualification profiles retain their USB console.
The bootloader arms the SDK's startup watchdog with a nine-second default.
A product can set `boot-watchdog-ms` from 1 through 300000; the builder records
that fixed budget and the SDK honors it. This changes bootloader arming, not the application's
ability to feed or extend the timer. NuttX normally disables and relocks it
before kernel initialization. Opt-in `ESP32S3_RWDT_INHERIT` keeps
the deadline and its slow-clock source unchanged through the SDK's clock
initialization, calibrates that source for SDK time/radio consumers without
feeding, and relocks it. The SDK's user-owned cancellation setting also
leaves the timer running. The startup profile selects a fixed 180-second budget
and direct `home_cam` application entry through its typed `[init]`
table. Board late initialization registers devices; the entry adopts first, then
invokes synchronous network initialization. Stable boots cancel before USB
credential/enrollment input; trial boots retain the deadline while waiting.
The register model checks both modes, locked/unlocked entry,
preserved bits and repeated handoff; it does not qualify reset timing.

Trial health requires capture, authenticated full-image write completion and
any configured real-frame inference within five seconds. Consuming retirement
joins TLS, camera and detection; the product then disconnects/stops/joins Wi-Fi,
acquires its quiet owner, confirms and verifies Stable, resumes, and cancels the
watchdog. Expired health or any failure prevents cancellation. A resume failure
after confirmation cannot undo Stable metadata. Successful completion recreates
the pipeline using the retained bound TCP listener; its TLS worker joins before
Wi-Fi quiescence. Retention avoids NuttX's `AddrInUse` failure on immediate
rebinding. Later recreation failures are outside the trial watchdog window.
The startup profile has a separate signed-image class to exclude legacy
candidates that cancel the deadline. Reversible unprotected ESP32-S3 qualification
has passed Stable autostart and a changed-code Trial: joined confirmation, fresh
TLS enrollment, camera/inference reacquisition and 200 seconds of sampled
progress, with confirmed primary-image readback. A credential-input failure also
led to restart and reversion to the previous confirmed image, verified by primary
readback and 200 seconds of recovered TLS/inference progress. That observation
included one USB reopen and no explicit host reset command; the reset cause was
not measured. A separate attribution witness observed USB reset causes and failed
the RTC-watchdog check; it does not establish physical watchdog causation.
Separately, after joining camera/TLS/detection and stopping Wi-Fi,
the unprotected target staged a 2,000,000-byte prefix of its own signed image
from the primary device into the inactive slot. Pending readback, subsequent
Trial confirmation, confirmed primary readback and 200 seconds of recreated
TLS/inference progress passed. A reset after one completed 256-byte staging
write and readback left the rest of the checked prefix and trailer erased and
the confirmed primary unchanged; TLS/inference recovered. Full 16 MiB restoration,
digest verification and ordinary-camera recovery passed after each experiment.
These tests do not qualify network delivery, changed-code staging, physical bus
quiescence, torn writes, power loss or encrypted-write timing.
The separate `joined-update` qualification binary reuses the camera composition,
then stages a complete externally supplied image after successful resource
retirement. Build it with the startup profile and `build.sh --bin joined-update`;
the ordinary camera binary contains no update receiver. Its trusted-USB frame is
`NXUP`, a big-endian 32-bit length, the signed container, then `DONE`. Wait for
`NXRS_UPDATE_READY` before the header and `NXRS_UPDATE_ACK offset=0` before data.
Send at most 32 bytes before each `NXRS_UPDATE_RX` acknowledgement; each
`NXRS_UPDATE_ACK` separately confirms the previous stager write/readback.
Send `DONE` after the final write acknowledgement. Input waits have ten-second
fragment and 300-second overall deadlines; native flash calls are synchronous.
Successful staging requests a trial but does not authenticate the image;
MCUboot checks signatures, class and counter on reboot. This is a temporary
qualification tool, not network OTA delivery.
The USB qualification reader returns at `NXRS_STARTUP_CONFIRMED`, preserving
the next pipeline's CSR for a new enrollment reader on the same serial connection.

## OTA delivery

The signed startup camera accepts one bounded firmware upload from its approved
mutual-TLS gateway peer on port 8443. Firmware reception allocates at most the
product's 2 MiB minus 8 KiB limit in RAM; it never writes flash while receiving.
After the sender acknowledges receipt, the camera joins capture, TLS, detection
and Wi-Fi, quiesces the radio, stages through `hal/boot`, requests a trial and
checks Pending. MCUboot authenticates the image after reset. A failed transfer
does not request a trial; an unauthentic image is rejected by MCUboot.

The default startup profile uses network enrollment below. Stop and join the
normal ingest owner, then run `nxrs-camera-ingest INGEST_JSON --require-camera-tls
--update SIGNED_IMAGE` with the same bootstrap configuration. This replacement
process owns the bootstrap listener before uploading, supplies trusted time
during transfer, and continues enrollment/media after the camera resets.
An admitted upload keeps its fixed 120-second budget across media reconnects.
The network mode needs no USB reboot acknowledgement. Explicit `tls`/`boot-tls`
qualification modes retain the existing USB supervisor and `NXRB` handoff.

The Rust operation is `nxrs-camera-ingest INGEST_JSON --require-camera-tls
--update SIGNED_IMAGE`. Use it with the current enrollment and no active video
connection; the supervised command above coordinates those lifetimes. Its success
means RAM delivery, with installation/confirmation still pending. Use a regular
local image file: the network deadline does not bound stalled filesystem I/O.
The versioned
wire contract is shared in `service/firmware-update`: `NXU1`, receiver `NXRD` plus
maximum length, sender length/image/`DONE`, receiver `NXOK`, then sender `CMIT`.
No hardware protection setting or eFuse is changed.

## Network enrollment and time

One-time commissioning stores the gateway's literal private IPv4 endpoint, exact
certificate SHA-256 pin, per-camera token and approved camera/site/gateway names
in `hal/config`, beside the saved Wi-Fi profile. Device signing keys stay volatile
and non-exportable. No USB certificate or clock service runs after commissioning.
The existing Rust ingest process owns one TLS1.3 bootstrap listener, signs a
verified P-256 CSR with server-assigned names/usages, and joins old ingest workers
before publishing a replacement grant. No second daemon or device worker is added.

Replace the ingest JSON's `camera_tls` block with, for example:

```json
"bootstrap": {
  "listen": "192.168.1.2:8444",
  "directory": "/var/lib/nxrs/camera-authority",
  "site_id": "home", "camera_id": "camera", "gateway_id": "gateway"
}
```

Keep `allow_insecure_camera_http: false` and the other media settings. The parent
directory must already exist. Run `nxrs-camera-ingest INGEST_JSON --init-bootstrap`
once as the gateway user; it creates a private authority directory, stable keys,
token and `commission.bin`, refusing a nonempty directory. The token-authenticated
bootstrap peer supplies the camera IPv4 address, allowing DHCP changes; the
configured camera port and approved names stay fixed. Back up this directory
privately. Start the gateway normally with `--require-camera-tls`. Stop and join
any prior USB supervisor; the commissioning tool must be the sole serial owner.
Before the camera's first network-enabled boot run:

```sh
python3 tests/nuttx-std/stream/commission.py --port SERIAL_PORT \
  --commission /var/lib/nxrs/camera-authority/commission.bin --wifi-file WIFI_JSON
```

This bounded USB step supplies commissioning and Wi-Fi only once. Subsequent
boots use saved records and renew through the gateway. The startup profile routes
normal diagnostics/syslog to UART0, with flow control disabled; USB transmit can
otherwise block without a reader. Rust opens `/dev/ttyACM0` only for missing
commissioning/Wi-Fi input, restores its termios and closes it before networking.
The tool also resumes Wi-Fi-only input after an interrupted first setup.
Firmware erasure of the
reserved config partition resets both records; keep ROM recovery available.
With hardware flash encryption off, stored Wi-Fi/token data is readable from
flash. This change performs no eFuse or protection provisioning.

Pinned TLS proves the commissioned endpoint without requiring UTC. Its bounded
exchange returns a 30-second trusted-time lease; the existing TLS worker refreshes
it after 15 seconds while serving media/OTA. Media refresh runs between complete
multipart frames, then returns the queued handoff so capture can supply a fresh
image. This keeps crypto/network delay out of a partially sent JPEG without
relaxing the gateway's freshness limit. Requests have a 5-second total budget
and measured delivery uncertainty. Cloning a lease preserves its original expiry;
backward disjoint observations fail closed. Exact-peer mutual TLS still guards
media and updates. The gateway host's UTC must be trustworthy. Camera sessions
renew before their one-hour grant expires and retry unavailable enrollment after
joined Wi-Fi cleanup. Losing time closes the authenticated stream.

`gateway/tests/network_enrollment.py` tests the actual SDK camera and Rust gateway
with synthetic media: enrollment, large JPEGs with a bounded clock-refresh stall,
time beyond the first lease, restart, paced OTA
and joined cleanup. Host results do not qualify ESP32-S3 heap/stack, physical flash,
phone playback or power-loss recovery. Generated static systemd ingest units do
not yet configure this writable authority directory; use the explicit Rust
process until its deployment policy is supplied.

These builds do **not** enable hardware secure boot or flash encryption,
install the bootloader or advance a hardware monotonic counter.
Remaining physical flash-operation quiescence
and encrypted interruption qualification keep protected runtime admission closed
in [#34](https://github.com/yongkyuns/nxrs/issues/34).
No eFuse or debug/download setting is changed by these build commands.

The opt-in `esp32s3-camera-stream-protected-build` profile is **build-only
factory qualification**, not a production provisioning procedure. Its chain is
ESP32-S3 ROM → RSA-3072-signed MCUboot → EC P-256-signed application. The builder
requires separate `--rom-key` and `--rom-root` inputs, uses `espsecure` from
`esptool==4.8.1`, verifies against the expected public root and checks the padded
ROM signature fits the reserved boot region. `bootloader.signed.bin` is the ROM
artifact; `bootloader.json` describes build configuration, never device state.
Application packaging uses 32-byte trailer alignment and encrypted native OTA
partitions. Factory images require `mcuboot_image.py --confirm`.

Protected builds require both protections to exist before SDK initialization;
unprovisioned hardware halts instead of entering automatic provisioning. A pinned
SDK patch rejects eFuse programming and clears staged writes. The builder records
the patch hashes and rejects an ELF that retains the physical programmer.
Factory preparation refuses older builds without this guard. These are software
safeguards, not physical qualification or permission to flash the working camera.
Build commands never flash or provision a device. Production keys, encrypted factory flashing,
ROM rejection tests and recovery/debug policy need qualification before hardware
activation. Keep disposable CI roots off devices.

Protected builds preserve MCUboot's NOR contract: explicit sector erases,
aligned writes only to erased ciphertext, and read-back verification. They remove
the upstream sector rewrite on each encrypted metadata write. Both SDK adapters
read physically erased 16-byte AES blocks as logical `0xff`; programmed blocks
use hardware decryption. Writes verify actual decrypted bytes, including `0xff`,
so a dropped program cannot pass through erased-value normalization. NuttX keeps
its existing flash mutex and internal-stack worker context.
The NuttX application helper uses the same 32-byte trailer layout and reports
32-byte writes. Its MTD proxy block size is also 32 bytes, so writing one flag
cannot reprogram neighboring ciphertext; incompatible block sizes fail the SDK
build. This does not enable protected runtime boot control.

Offline integration also compiles the camera/TLS/detector/Wi-Fi startup path and
inherited watchdog with encrypted OTA and 32-byte MTD writes. Independently
verified 32-byte factory artifacts match the guarded loader's roots, fixture
class and 180-second budget. This is build/package evidence; encrypted camera
runtime remains unqualified.

The pre-activation software and bounded qualification milestone is complete.
Protected runtime admission and a shipping startup cohort remain deferred pending
separate configuration review and physical qualification. ROM enforcement,
encrypted torn-write/power-loss recovery, cache/AES timing, a comprehensive
DMA/MSPI fence and physical watchdog attribution remain unproved. Hardware
protections remain OFF; no permanent device setting has been changed.

Prepare those files offline from the protected boot build and a confirmed
factory application:

```sh
python3 tools/prepare-protected-factory.py \
  --platform esp32s3-camera-stream-protected-build --boot-build /path/to/boot-build \
  --application /path/to/factory.signed.bin --rom-root /path/to/rom-public.pem \
  --app-root /path/to/app-public.pem --flash-key /path/to/device-xts-key.bin \
  --output /path/to/new-factory-bundle
```

The operator supplies a 32-byte `XTS_AES_128_KEY` input that must match the
device's factory-provisioned key. The tool requires the matching guarded
write-once boot build, both public signature roots and a fresh confirmed trailer.
Espressif's SDK encrypts image bytes at their flash addresses, including padding
needed for direct execution. Unwritten trailer blocks and empty secondary/scratch
remain physically erased. The tool verifies the exact logical read-back before
publishing the complete bundle.
Only ciphertext and an offset/size/hash manifest are published. This neither
provisions keys/eFuses nor qualifies hardware protection or runtime recovery;
the encrypted-update gate remains closed.
