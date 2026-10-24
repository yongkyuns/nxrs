# Automatic home activity

This Unix gateway component observes the existing, authenticated **loopback
MediaMTX stream**. It never acquires the camera or opens USB. The camera keeps
its single ingest viewer. Motion is pixel movement, not person/pet recognition.
The separate on-camera detector can evolve independently.

One observer process owns one authenticated remux reader, one FFmpeg decoder, a
96×72 grayscale detector at 2 Hz, a bounded original-H.264 segment ring and at most one clip finalizer. The
source is remuxed into two-second TS segments, not re-encoded for recording.
Two changed samples after warmup confirm movement; a median brightness delta
compensates uniform exposure changes. This is a baseline motion heuristic;
small/fast movement, camera shake and nonuniform lighting need real-scene tuning.
There are no inferred person labels, remote notifications or accuracy guarantees.

Defaults: six-second pre-roll, eight-second post-roll, a thirty-second recording
window, three-day automatic retention, 1 GiB / 1,000 events. Segment boundaries
are keyframe-aligned, so pre/post roll is approximate. Timestamps are gateway
observations, not sensor exposure. A source interruption can publish a playable
partial clip marked interrupted. There is no silent video duplication during an
outage. The source child is reaped before reconnect with capped 1/2/4/8-second
backoff. Unpublished data is isolated from the published event index.

The private store uses 0700 directories, 0600 event files, a std interprocess
file lock, synchronized atomic event-directory publication and atomic metadata
updates. It rejects symlinks, hard-linked media and corrupt manifests. Corruption
does not erase recordings. Startup recovers only unpublished/retired staging
directories; the observer's per-camera lifetime lock permits cleanup of its own
abandoned spool. The spool is bounded to forty files / 64 MiB, and one event is
capped at 32 MiB. A slow/full filesystem can drop an automatic event; health
reports that loss. Kept clips are excluded from both expiry and quota eviction.
When kept clips fill storage, new recordings fail visibly rather than deleting
them. User-initiated iPhone recordings remain a separate library.

## Recorder credential and process boundary

The recorder gives `nxrs-rtsp-reader` exactly 64 credential bytes through an
inherited pipe. The native SDK reader authenticates the loopback RTSP source
and remuxes compressed H.264 into a separate media-only pipe. FFmpeg decodes that
pipe without receiving the credential. Arguments and environments contain only
the public loopback endpoint and literal identifiers. The strict Linux overlay
requires isolation for both decoding and clip finalization; failed enforcement
cannot fall back. See [reader ownership and confinement](../ingest/reader.md).

Rust-owned credential buffers are zeroized on drop; native copies, shared UID
access and protected key custody remain separate concerns. Production recording
requires its independently qualified encrypted storage configuration.

At startup, controller reads may briefly hold the shared store lock. Store and
initial-health admission retry only `Busy`, within one monotonic second, and
remain cancellable before any source child starts. An occupied per-camera
worker lease still refuses immediately. Later connecting-health writes tolerate
`Busy` just like periodic health writes; protection and corruption failures
remain errors. Media progress, motion thresholds and child retirement deadlines
are unchanged.

Every post-spawn setup failure retires the reader and decoder. Missing executables
roll back their empty unpublished source directory. A failed reap stops source
admission and leaves unpublished files for controlled recovery rather than
reconnecting or deleting files a live child might still use. Normal network
retry remains bounded at 1/2/4/8 seconds. Run under the reviewed process-group or
service supervisor: whole-group cleanup before restart remains required after
worker crash/SIGKILL. Configuration/token/media reads and lock opens reject
FIFOs without waiting for a peer; regular-file I/O still assumes a trusted local
filesystem and is not given an interruptible filesystem deadline.

For a controlled recorder-key change, stop the owning worker supervisor/service
and verify its decoder/finalizer descendants are gone. Update the existing
camera-scoped recorder token and matching controller digest through the reviewed
private configuration procedure, preserving other identities and denial state.
Apply the deployment's controlled group restart; verify rejection of the old
credential and actual new decoded media. No live rotation, service installation
or denial reset is performed by this code.

For the strict Linux recording-volume configuration, see the
[encrypted storage deployment](../deploy/encrypted-activity.md). It requires a
separately unlocked LUKS2 volume covering the entire activity tree; backups and
physical qualification remain separate release gates.

## Enable on an existing gateway

Build the independent host workspace and prepare a **new** private bundle:

```sh
cargo build --manifest-path gateway/Cargo.toml --locked
python3 gateway/deploy/activity.py /private/existing-config.json /private/new-activity \
  --camera CAMERA_ID --owner VIEWER_ID \
  --worker /absolute/nxrs-camera-activity --ffmpeg /absolute/ffmpeg \
  --rtsp 127.0.0.1:8554
```

The generator does not start services, change existing files, rotate enrollment,
or write secrets to stdout. It retains viewer/publisher hashes and grants only
the named owner recording-management permission. A separate random recorder key
can only read its camera over RTSP. It cannot use the public API, publish video,
manage MediaMTX, or become an iPhone credential. Existing viewer keys default to
history read-only. Existing configuration without activity remains supported.

After reviewing paths and permissions, run the controller with the generated
`control.json` and the observer with `activity.json`. Keep the existing MediaMTX
authorization callback pointed at the controller's original internal address.
The iPhone retains its existing home enrollment. A rendered launch agent supports
start at macOS login and restart after failure; the Linux unit is an installation
template requiring its explicit `/etc` and `/var/lib` mapping. Neither is a claim
that a physical camera automatically boots, retains credentials or survives power
loss. Existing USB inspection supervision remains separate.

## API and iPhone

All history, metadata and media use the enrolled control origin and account
Bearer authorization. Camera/site scope and durable viewer denials apply to
every request, including each media piece. Backend recorder, publisher and media
grant keys cannot read history. Kept/unkept and delete require explicit
`manage_recordings: true` in static viewer enrollment. This grants management of
that viewer's authorized cameras, not all cameras.

- `GET /v1/cameras/{camera}/activity`: latest fifty events, cursor, retention,
  storage bounds and observed worker health.
- `GET /v1/cameras/{camera}/activity/before/{event}`: older events.
- `GET /v1/cameras/{camera}/activity/kept` and `/kept/before/{event}`:
  filter kept clips before pagination, including footage beyond the latest page.
- `GET /v1/cameras/{camera}/activity/{event}/thumbnail`: private JPEG thumbnail.
- `GET /v1/cameras/{camera}/activity/{event}/clip`: complete private MP4.
- `GET /v1/cameras/{camera}/activity/{event}/clip/parts/{part}`: zero-based 512 KiB
  pieces. The final piece is shorter. The iPhone verifies every length and
  reconstructs the declared ≤32 MiB clip under one ninety-second budget. This
  also works through the existing 1 MiB control proxy without trusting a new
  origin, embedding credentials in URLs or passing credentials to AVPlayer.
- `POST /v1/cameras/{camera}/activity/{event}/keep`, `{"kept":true|false}`.
- `DELETE /v1/cameras/{camera}/activity/{event}`: explicit owner deletion.
- `POST /v1/cameras/{camera}/activity/{event}/delete`, `{}`: the same explicit
  deletion for existing control proxies admitting only GET/POST.

No route accepts query credentials; responses are no-store. Filesystem work has
four admitted blocking operations whose permits survive handler cancellation.
The iPhone caches at most one downloaded clip privately with complete file
protection and backup exclusion, clears it on scope changes, and plays locally.
It polls while Activity is foreground; a stale check cannot keep the monitoring
badge green. A gateway without this optional feature shows an unavailable state.

## Qualification

```sh
cargo test --manifest-path gateway/Cargo.toml --locked
cargo clippy --manifest-path gateway/Cargo.toml --all-targets --locked -- -D warnings
python3 -m unittest discover -s gateway/tests -p 'test_activity_deployment.py'
python3 gateway/tests/activity_credentials.py --worker /absolute/nxrs-camera-activity
python3 gateway/tests/activity_reference.py --gateway /absolute/nxrs-gateway-control \
  --worker /absolute/nxrs-camera-activity --mediamtx /absolute/verified-mediamtx
```

The reference uses separate processes/ports and synthetic footage, verifies TLS
with its own scoped ephemeral CA, reconstructs and fully decodes a motion clip,
checks permissions and restart persistence, then removes all fixture secrets and
footage. It additionally rejects wrong/cross-role RTSP credentials and inspects
the actual source/finalizer arguments, environments, logs and saved media for
credential strings. Native metadata detector positive controls deliberately place
a synthetic token in argv/environment; raw metadata and keys are never printed.
The separate process witness tests private-pipe EOF, cancellation/early-exit
cleanup, spawn rollback and special-file startup rejection. It is not a decoder
or physical camera witness. It does not establish physical motion accuracy, endurance or iPhone
rendering. Device evidence must be recorded separately.

`gateway/tools/activity_soak.py` observes a real enrolled physical source and
owned process counters for the full requested interval. It never opens USB or
repairs/replaces a source. It retains missing observations, stale frames and
resource-growth failures. A shorter run cannot become a 72-hour pass. Keep its
numeric logs and all real footage private; publish only a reviewed summary after
the complete qualification. A Mac must remain powered and awake; temporary HTTPS
certificate lifetime is a separate deployment constraint.
Required owner processes and their decoder/finalizer descendants are observed.
`--observe-pid media=PID` includes an existing MediaMTX process without managing
or stopping it. RSS includes shared pages and is not total physical RAM.

Decoder health records monotonic frame gaps and reconnect durations from the
last decoded frame before a retired source to the first frame after recovery,
including cleanup, retry backoff and failed attempts. Initial acquisition is
not a reconnect. Maxima cover the current worker lifetime; the observer retains
the starting counters so earlier gaps do not fail a later qualification. A new
gap over 30 seconds fails even if recovery completes between HTTP samples.
Worker restarts open a new timing interval and fail the unattended observation
rather than silently discarding an unmeasured gap. Restart recovery is exercised
separately by the isolated reference test.
