# MJPEG streaming

`MjpegSession` writes one multipart MJPEG response through an injected
`std::io::Write` implementation and an injected, fixed-capacity frame buffer.
The owner supplies monotonic milliseconds and calls `poll` with a byte budget;
each poll makes at most eight write calls. Use a nonblocking writer when the
owner must remain responsive.

Offers are copied only while the response is ready for a frame. While any
response or frame bytes remain pending, offers return `Busy` and increment the
skip counters. The producer may offer its newest frame after the current part
completes. A send deadline covers the initial HTTP header and each accepted
frame. Expiration, a backwards clock, zero writes, and fatal writer errors
close the session; a truncated multipart part is never followed by another.

`Server` adds a nonblocking HTTP listener and one active client. It serves
`/stream` (also `/stream.mjpg`), a minimal viewer at `/`, and a timestamp at
`/clock`. Each poll does bounded accept/read/write work. Request size, request
timeout, send deadline and frame age are bounded; extra clients are rejected.
The same outgoing buffer is reused across disconnects.

Neither layer starts a thread or owns camera hardware. The application supplies
time and polls the server from its existing event loop. See the
[portable composition and device test](../../tests/nuttx-std/stream/README.md).

`Server::with_transport` wraps an admitted nonblocking TCP socket in an owned
`Read + Write` transport, so authenticated TLS can reuse the same HTTP/MJPEG
parser, buffers, deadlines and reconnect behavior. Busy sockets close before the
factory runs. A transport must return `WouldBlock` while its handshake is pending
and admit no application bytes before peer authentication succeeds. The request
deadline includes transport setup and handshake. Expensive crypto belongs on the
network owner's worker, outside capture. `Server::new` selects the existing
cleartext development/protected-boundary path explicitly. See
[device TLS constraints and evidence](../../docs/device-identity.md#device-tls-server-and-streaming-transport).

`StreamWorker` constructs its network/credential owner in a dedicated Rust
thread with platform-supplied stack/poll budgets. Its one latest-frame exchange
uses try_lock; `FramePublish::Busy` drops a capture handoff rather than waiting
for cryptography or slow I/O. It starts offline. `WorkerReceiver::serve` uses
the capture clock's monotonic epoch and drives the same bounded server. Capture
and handoff buffers are separate injected resources. An explicit finish joins
and returns final counters after native retirement; Drop requests stop while
the worker retains resources until exit. `camera_stream::ThreadedCamera` wires
this handoff into the actual camera service and preserves retryable camera stop.
Native calls remain cooperative, and MCU resources require separate measurement.

`StreamWorker::spawn_with_metadata` additionally accepts caller-owned publication
storage, bounded by 4096 bytes. `publish_with_metadata` copies a JPEG and its
optional publication together under the same try-lock. Busy or invalid offers
leave the prior pair intact; a JPEG-only offer clears the previous publication.
The worker emits metadata only for `/stream?metadata=1`, preserving per-connection
revision suppression and the existing JPEG-only endpoints. There is no allocation
in this handoff. `spawn` keeps its JPEG-only storage budget.

`ThreadedCamera::attach_detection` requires the full publication capacity before
capture starts. Inference remains on its independently owned worker; failure and
expiry use the existing unavailable-state policy. Stop joins detection even if
camera stop fails, while `finish_worker` remains the separate network retirement
barrier. Product wiring must budget the publication storage and detector worker
separately from TLS and capture resources.


`WorkerReceiver::serve_with_control` runs a typed owner-local control closure on
its network worker before each I/O poll, including while offline and before
locking the latest-frame exchange. This can apply authenticated trust updates
or refresh the thread-confined entropy owner without blocking capture handoffs.
Control must bound its work and native calls; a bounded channel with one
`try_recv` per poll is suitable. `WorkerAction::Disconnect` retires even an idle stream before further I/O and
keeps the listener for fresh admission under the updated policy. Control errors retire the active stream and
close the worker permanently; explicit `finish` remains the SDK/key/source
retirement barrier. Stop is rechecked after a control operation returns.
This supplies an execution point, not an authenticated control transport,
protected clock/floor source, persistent custody or key-rotation procedure.
