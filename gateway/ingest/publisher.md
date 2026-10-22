# Encoder and publication credential boundary

The image encoder has no publisher token in its arguments, environment or input
pipe. Ingest feeds it validated JPEG envelopes. FFmpeg returns H264/MPEG-TS on
stdout and numeric frame progress on stderr, with SDK diagnostics disabled.
Ingest relays compressed bytes through a bounded 4096-byte buffer to a separate
single-use `nxrs-rtsp-publisher`. Only that publisher's stdin receives the exact
64-byte lower-case hexadecimal token before the MPEG-TS bytes. No secret-bearing
temporary file, URL argument, shell, environment variable or unauthenticated
loopback publisher exception is introduced.

The publisher's public arguments are a literal loopback IPv4:port, enrolled
username and stream identifier. It validates these before accepting its pipe
preamble. It then uses FFmpeg's installed SDK to demux one H264 video stream and
remux packets to the existing authenticated RTSP-over-TCP endpoint. It never
decodes/encodes pixels. Public dimensions and source bounds are checked; packet
sizes are checked after SDK allocation. These checks do **not** prove bounded
internal native allocations or native parser safety. Service memory/task limits,
dependency patching and native sanitizer qualification remain necessary.

Private Rust SDK bindings own native contexts/dictionaries/packets. They call
actual FFmpeg operations through `ffmpeg-sys-next =9.0.1`, generated against the
installed headers. No new C/POSIX wrapper or cross-platform HAL is added. Rust
owns the process/packet lifecycle; there are no Rust native callbacks or callback
contexts to free. RTSP uses `AVFMT_NOFILE`, so its output context must not be
opened with ordinary `avio_open`. All native diagnostics are disabled before
processing a credential-bearing URL; only fixed error categories reach logs.

The protocol's initial read uses an unbuffered inherited `/dev/stdin` descriptor
and consumes exactly 64 bytes. Using Rust's buffered `Stdin` there could prefetch
and lose MPEG-TS bytes before the SDK reads `pipe:0`. Regular file stdin is
refused. This helper requires its supervising ingest process: blocking pipe/SDK
calls alone do not provide the supervisor's cancellation guarantee.

The supervisor watches source deadlines, TLS credential leases, encoder frame
progress, successful publication packet progress and both child exits. Initial
encoder/publication progress has a ten-second deadline; subsequent progress is
bounded by at least three seconds. Relay read/write progress has a whole-operation
deadline. Failure or cancellation drops both pipes and kills/reaps **both**
children, including partial-spawn failures. Both retirement attempts execute even
if one fails; a reap failure prevents another admission. Normal RTSP retry remains
one to eight seconds. The unprivileged service cgroup now owns three processes;
`KillMode=control-group`, 256 MiB and 128 tasks remain in force. Standalone SIGKILL
has no whole-group guarantee; use the reviewed service for that behavior.

## Build and qualify

From `gateway/`, build normal ingest and the optional native publisher:

```sh
cargo build --locked -p nxrs-camera-ingest --bin nxrs-camera-ingest
cargo build --locked -p nxrs-camera-ingest --features publisher-sdk --bin nxrs-rtsp-publisher
cargo test --locked -p nxrs-camera-ingest --features publisher-sdk --bin nxrs-rtsp-publisher
```

The publisher needs the host FFmpeg development headers/libraries and libclang.
Linux pkg-config discovers them; an explicit `FFMPEG_DIR` can identify an already
installed SDK prefix. This does not download/build an SDK or install system
packages. Default firmware/controller/ingest builds do not enable this feature.
Install the root-owned reviewed helper at the new config's absolute `publisher`
path, normally `/usr/local/libexec/nxrs-rtsp-publisher`. Older configs without that
field fail closed and require an explicit migration preserving all enrollment,
keys, denials and TLS state. No automatic live migration is performed.

From the repository root:

```sh
python3 gateway/tests/publisher_reference.py --publisher gateway/target/debug/nxrs-rtsp-publisher --mediamtx /path/to/verified/mediamtx
```

This executes the real SDK and MediaMTX with synthetic credentials/media:
successful authenticated publication, exact path/password rejection, truncated
or malformed credential framing, malformed/non-H264 media, regular-input refusal,
clean EOF and fixed diagnostics. The TLS process fixture separately retains
authentication/expiry/cancellation controls and checks cross-child retirement
after either child dies. `ingest_reference.py` tests the actual Rust camera
framing, encoder, native publisher, two decoded WebRTC viewers, source stall and
recovery, actual argv/environment/log secret absence, and hosted service crash
cleanup. A test assertion is not evidence until that exact build passes it.

## Remaining isolation and release gates

These processes still share a service UID. A compromised encoder may read
same-UID runtime credentials or inspect same-UID processes. Root, ptrace/core
access and a malicious installed encoder/publisher are outside this boundary.
The original token and TLS key files still need protected storage and stronger
process/UID separation. Rust-owned preamble/URL buffers are zeroized on drop;
native SDK copies and process memory are not proven zeroized.

No native SDK distribution is certified, artifact-pinned or covered by RustSec
alone. Record actual SDK/executable hashes, package dependencies, advisories,
licensing, native fuzz/sanitizer evidence and service deployment qualification
before release. This change does not implement camera TLS on a board, hardware
key custody, publisher revocation/rotation, recording encryption or FIPS
validation. Those gates remain open in #33/#35–#38.
