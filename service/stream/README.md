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
