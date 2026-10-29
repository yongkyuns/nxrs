# Camera output

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
The worker retains these two bounded strings once, with no extra frame buffer.
Its bounded time callback supplies an authenticated UTC lease on admission and
once per second;
it must account for the original observation's age. This API does not implement
enrollment, persistent credentials, rollback protection or trusted-time discovery.

Offline transitions retire the connection, including a complete offline/online
cycle during a native call. Stop and Drop join TLS before releasing camera
storage. Explicit shutdown can wait for an in-progress cryptographic/time call;
it is not a capture-loop operation. A stopped TLS output cannot restart.

`tests/tls/qualify.py` exercises the product composition with synthetic JPEGs,
actual SDK peers, rejected clients, reconnect, time failure, blocked-worker
capture progress and teardown. Its debug/sanitizer host worker uses 256 KiB;
the target compile fixture requests 64 KiB. Neither is a device peak measurement.
Product profiles remain disabled pending board
entropy, provisioning and physical flash/heap/stack/latency qualification.
