# Camera detection delivery

The camera Rust application publishes detector observations and confirmed person
state. The gateway authenticates publishers and viewers. The iOS view displays
`Person detected`, `No person detected`, or `Detection unavailable` independently
of playback status. A stale video view never displays a current person state.

## Contract and ownership

[detection-protocol.schema.json](detection-protocol.schema.json) is the version-one
wire contract. [monitoring-api.json](monitoring-api.json) describes the public
subscription. The shared golden fixture is
`clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json`; Rust, Swift and
JSON Schema tests consume those same bytes. `service/detection-wire` owns the
Rust wire types and bounded serializer/parser. It depends on no HAL or model.
The application maps its typed detection snapshot into that protocol; the HAL
continues to own only inference and its common capability API.

`Source` carries a publication ID, owner-clock emission time, confirmed presence,
localization capability, optional latest observation, and eight retained events.
Each observation identifies its original camera frame and contains at most ten
person boxes. Scores are quantized integers in 0..1000, not calibrated intrusion
probabilities. Bounds use 0..1000 coordinates relative to the entire source image,
with left < right and top < bottom. Grayscale/presence-only providers advertise
localization false and send no boxes. During temporal confirmation, presence can
be unavailable while a fresh raw observation is present.

`person_entered`, `person_left` and `unavailable` event IDs increase within a
source session. They describe policy transitions, separately from observations.
The eight-event ring is not durable alert storage or a push-notification service.

## Camera to gateway

The existing caller-polled camera HTTP server admits one viewer. A gateway opts
into `/stream?metadata=1`; `/stream` and `/stream.mjpg` retain their JPEG-only
format. A new publication is a MIME part with content type
`application/vnd.nxrs.detection+json`, a bounded Content-Length and up to 4096
payload bytes, immediately before a JPEG part. There is at most one metadata
part per JPEG, sent once per publication per connection. New connections receive
the current complete state, including retained events. JPEG sequence and
`X-Capture-Ms` semantics remain unchanged. Metadata never waits for inference.

Serialization and multipart control storage are allocated during setup and
reused. The application adds a 4096-byte serialization buffer; the stream server
adds 4480 bytes of reusable multipart header storage. No per-frame allocation,
additional MCU listener, thread or camera owner is introduced.
The camera fixture supplies a 32 KiB owner-task stack after measurement showed
only 1312 bytes free in the former 16 KiB budget. This product resource choice
does not enter the common protocol, HAL or controller algorithms.

The ingest parser validates lengths, version, arrays, geometry, clocks and
sequences before forwarding unchanged JPEG bytes to the encoder. A separate
latest-value task forwards metadata and observed video progress at most four
times per second; its HTTP requests have a 500 ms deadline. Video encoding never
awaits that task. Publication failure or absent metadata expires detection state.
The documented trusted-LAN constraints of the camera HTTP link still apply.

Enable the ingest configuration explicitly for compatible camera firmware:

```json
"detections": { "control": "127.0.0.1:18081", "camera_id": "cam_reference" }
```

`gateway/deploy/strict_vpn.py render --camera-detections` renders this option
alongside an explicit camera. Legacy camera configuration remains unchanged.

The separate loopback control listener accepts:

- `POST /internal/cameras/{id}/detection-sessions`: mint a new source-session ID.
- `POST /internal/cameras/{id}/detections`: publish an `Update` for that session.

Both require the configured camera publisher's bearer token and
`X-Nxrs-Publisher` identity. Viewer and media-management credentials cannot
publish. Camera identity comes from enrollment, not a device-supplied ID. Each
source connection gets a fresh gateway-minted session; prior-session updates
are rejected. The publisher task is stopped and joined before another source
connection/encoder is created. Gateway restart invalidates all old sessions.

## Gateway to iOS

`GET /v1/cameras/{id}/detections` is an HTTPS SSE subscription with the existing
viewer credential in Authorization. Playback credentials are not reused for
metadata. Every emission rechecks viewer/site/camera authorization; revocation
ends the stream. Limits are 64 subscriptions globally and four per viewer.
Slow readers have no application queue. Shutdown ends streaming bodies before
HTTP listeners drain. The nginx route disables buffering and preserves
Last-Event-ID. The optional local HTTPS fixture proxy also streams incrementally.

Each `event: detection` contains a complete `Snapshot`, including confirmed
presence, current source data, known observation age and a 1500 ms presentation
lease. The gateway emits every 500 ms. Source updates older than 1500 ms, or
observations older than 5000 ms, produce unavailable. Age uses source-owner time
plus monotonic time since receipt; it is not UTC or sensor-exposure freshness.

SSE IDs have the form `source_session_id:event_count`. Last-Event-ID asks for a
current snapshot and gap assessment, not unlimited historical replay. A session
change, future cursor or event-ring overrun sets event_gap. Clients can also
detect local drops by comparing their last event ID with the retained ring.

The Swift transport has an 8192-byte line limit and a one-element newest-value
buffer. A subscription and its iterator own cancellation, including early loop
exit. Camera switching, stop and backgrounding cancel the request and clear the
indicator; stale callbacks cannot update the new camera. Authorization failures
stop retry; transient failures have bounded backoff.

`stream_elapsed_ms` is monotonic elapsed time since the gateway subscription
began. The client subtracts it from elapsed time since starting its request;
the nonnegative difference conservatively includes setup and delivery delay.
Delayed buffered SSE therefore cannot renew its presentation lease merely by
arriving. No cross-host UTC synchronization is assumed. The indicator also
requires advancing video and expires without further network input.

## Video box alignment

Source-frame boxes reach iOS, but `frame_synchronized` remains false. The current
JPEG-to-H.264 encoder discards source-frame identity and assigns arrival-based
video timestamps; the native renderer exposes its decoder clock. Those clocks
have no qualified source-to-display mapping. The client displays confirmed
person status and does not draw boxes over a different live frame. A future
overlay requires carrying and qualifying that mapping, including delay,
reordering, reconnect and aspect-fit/orientation transforms. Static contract
fixtures and native decoding alone do not establish frame alignment.

## Qualification

The physical ESP32-S3 camera delivered 17.77 decoded video FPS through authenticated
WHEP over 600 seconds while the metadata subscription received 798 distinct
observations (1.33/s). Inference failures and camera discards were zero. The
same final firmware's 60-second detection-disabled control delivered 17.78 FPS.
The 32 KiB owner stack used 14,784 of 32,480 usable bytes, leaving 17,696 bytes.
The model, threshold and numerical implementation are unchanged from
[PICO performance qualification](camera-pico-performance.md).

The native iOS Simulator ran `DetectionProbe` with the production
`DetectionSession` and system-trusted TLS for 120 seconds: 239 snapshots, 160
distinct observations, one source session and no reconnect. Metadata contained
person boxes; this does not qualify live-frame alignment. The updated Home card
and full-screen navigation passed the UI workflow, including unavailable status.
Core and native lifecycle tests, strict Rust Clippy, gateway/schema tests and
Simulator/iPhone SDK builds pass. These are development checks, not a new
physical-iPhone acceptance result.

A separate 60-second run stopped gateway control for five seconds while an
existing WHEP viewer continued. Video held 17.77 FPS; the native iOS controller
expired status, retried, accepted the new source session and reported the history
gap. Gateway SSE bodies drained before shutdown. Existing camera enrollment,
certificates and viewer/publisher credentials were preserved.

Run the same native subscription controller against an existing private
enrollment with a matching catalog camera ID:

```sh
swift run --package-path clients/ios/Native DetectionProbe /private/path/probe.json 60
```

The private JSON contains `gateway` (HTTPS URL), `credentialFile` (existing viewer
credential file), and `cameraID` (opaque catalog ID). The probe uses OS trust and
does not install a CA or bypass TLS. iOS execution additionally requires an iOS
SDK build and an explicitly owned test Simulator/device with the existing trust
configuration. Keep credentials and runtime images outside the repository.

Numeric evidence, firmware/source hashes and measurement boundaries are in
[`tests/detection/results/2026-10-22-delivery`](../tests/detection/results/2026-10-22-delivery).
Simulator execution does not establish physical-iPhone UI, background operation
or notification delivery.
