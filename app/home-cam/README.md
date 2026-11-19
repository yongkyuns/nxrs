# Home camera

`home-cam` owns streaming, commissioning, TLS enrollment, detection, trial
confirmation and OTA policy. Its entry and lifecycle use only portable HAL APIs;
console access, scheduling and device admission stay below the HAL boundary.
Target configuration and generic entry wiring belong to `platform/`.

Build through `cargo nxrs build --app home-cam --platform wroom`; supply
operator signing inputs as described in [signed updates](../../docs/firmware-updates.md).
Saved records are loaded before network bring-up. USB opens only for missing
first-time commissioning records; enrollment, trusted time and OTA then use
Wi-Fi. Qualification controls remain in the
[stream fixture](../../tests/nuttx-std/stream/README.md).

On the WROOM, capture and the TLS writer run above the PICO inference worker.
Detection still receives bounded copies and may skip samples under load.

## SSH maintenance

The normal WROOM image has SSH disabled. For a maintenance image, reserve an
IPv4 address for the camera in DHCP and build with the same signing inputs:

```sh
cargo nxrs build --app home-cam --platform wroom \
  --profile esp32s3-camera-stream-ssh \
  --kconfig-set 'NETUTILS_DROPBEAR_BIND_ADDRESS="192.168.1.42"'
```

Install it locally with a [matching MCUboot loader and confirmed factory
image](../../docs/firmware-updates.md). Its image class differs from the regular
camera's, so the regular loader and updater will reject it. The maintenance
image runs on both cores and uses unbuffered TCP for the interactive shell.

Provision a unique native Dropbear ECDSA host key and NuttX password database
on the 64 KiB LittleFS volume at `0xf00000` using the
[local provisioning procedure](../../tests/nuttx-std/ssh/README.md#verify-persistence-on-esp32-s3).
Use the same test board and keep a verified full flash backup. When switching
from the provisioner to the camera, preserve the verified credential volume;
the camera mounts it read-only and will not format it. No credential files
are built into the image. SSH starts only on a confirmed boot with Wi-Fi
addressed and the provisioned files present. Pin the provisioned public key
and connect using the account created during provisioning:

```sh
ssh -tt -p 2222 -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS" maint@192.168.1.42
```

This account has administrative access through one interactive NuttX shell.
See [the Dropbear scope](../../docs/dropbear-ssh.md#ownership-and-supported-scope)
for what the shell supports.

## Streaming API

`LiveCamera::new` retains the caller-polled plaintext development path.
`LiveCamera::new_tls` uses the same HTTP/MJPEG implementation over Mbed TLS,
without a plaintext fallback. Its output type is selected statically.

TLS adds one worker and one injected frame handoff buffer to the three existing
camera/output buffers. Product code supplies the stack budget and polling
interval. Capture only tries the mailbox lock, copies outside it, and skips busy
handoffs; TLS never borrows camera storage. HTTP/header storage is reused across
connections. Detection metadata crosses the same bounded owned handoff.

The initializer runs before capture on the TLS thread, where it must create the
non-Send identity and supply approved DER certificates/CRL. Its certificate policy
supplies the independently approved issuer pin and expected own/peer URI names.
Startup validates the enrollment, key binding and trusted-time lease before
reporting ready; each connection still revalidates them with fresh time.
The worker retains these two bounded strings once, with no extra frame buffer.
Its bounded time callback supplies an authenticated UTC lease at startup, on
admission and once per second while connected. Before admitting live video, the
camera opens an exact-pin TLS connection to the gateway clock. It reuses that
connection for time renewal every 15 seconds and services video between I/O
steps on the same owner. Each exchange is limited to five seconds, and the
short-lived lease still accounts for the original observation's age and fails
closed on expiry.

After 45 minutes, the same TLS worker requests new one-hour credentials for
the existing camera and gateway keys. The camera and gateway validate the
renewal before using its deadlines. The active media transport, camera, PICO,
and SSH keep their owners.

Offline transitions retire the connection, including a complete offline/online
cycle during a native call. Stop and Drop join TLS before releasing camera
storage. Explicit shutdown can wait for an in-progress cryptographic/time call;
it is not a capture-loop operation. A stopped TLS output cannot restart.

The `esp32s3-camera-microphone` qualification profile authorizes its enrolled
gateway to request `/stream?av=2`. Each connection starts with capture muted.
An unmute command starts one microphone worker with a 16 KiB stack and receives
an acknowledgement once the first PCM block is available. Mute stops capture
and drops pending blocks before acknowledging. JPEG-only requests leave the
microphone idle.
The worker waits for DMA completions independently of TLS and keeps one latest
block. Sample offsets identify blocks skipped by a slow writer. Disconnect
cancels the native wait and joins the worker before accepting another session.
PCM timestamps estimate the sample start from the completion observation on the
camera's monotonic clock. Hardware timing and the capture-side mute control are
tracked in the [Matter microphone plan](../../gateway/tests/matter/conformance.md#microphone-delivery).

Camera I/O errors, capture stalls and temporary TLS startup failures trigger
recovery within the existing Wi-Fi session and SSH lifetime. The app retires
the stream, capture and detector before reopening them on the same listener.
The first retry is immediate. Repeated failures back off to at most 30 seconds,
and 32 captured frames reset the delay. A retirement failure prevents reopening.

`tests/tls/qualify.py` exercises the product composition with synthetic JPEGs,
actual SDK peers, rejected clients, reconnect, time failure, blocked-worker
capture progress and teardown. Its debug/sanitizer host worker uses 256 KiB;
the target compile fixture requests 64 KiB. Neither is a device peak measurement.
