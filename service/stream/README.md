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

`HttpSession` runs the same bounded HTTP state machine over an admitted
`Read + Write` transport, including `tls::Connection`. Its owner completes TLS
authentication before admission and isolates synchronous crypto from capture.
Disconnect retires the transport; `into_buffer()` returns the owned frame buffer.

`Server` adds a plaintext nonblocking TCP listener and one active client. It serves
`/stream` (also `/stream.mjpg`), a minimal viewer at `/`, and a timestamp at
`/clock`. Each poll does bounded accept/read/write work. Request size, request
timeout, send deadline and frame age are bounded; extra clients are rejected.
The same outgoing buffer is reused across disconnects.

These types start no thread and own no camera hardware. The application supplies
time and polls the server from its existing event loop. See the
[portable composition and device test](../../tests/nuttx-std/stream/README.md).
