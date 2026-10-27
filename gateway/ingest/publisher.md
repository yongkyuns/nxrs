# Encoder and publication credential boundary

The image encoder has no publisher token in its arguments, environment or input
pipe. Ingest feeds it validated JPEG envelopes. FFmpeg returns H264/MPEG-TS, or AUD-delimited Annex-B for annotations, on
stdout and numeric frame progress on stderr, with SDK diagnostics disabled.
Ingest relays compressed bytes through a bounded 4096-byte buffer to a separate
single-use `nxrs-rtsp-publisher`. Only that publisher's stdin receives the exact
64-byte lower-case hexadecimal token before the compressed bytes. No secret-bearing
temporary file, URL argument, shell, environment variable or unauthenticated
loopback publisher exception is introduced.

The publisher's public arguments are a literal loopback IPv4:port, enrolled
username and stream identifier. It validates these before accepting its pipe
preamble. Annotated publication adds exactly one public `annexb` argument; unknown modes and extra arguments fail closed. The reader retains its three-argument contract. It then uses FFmpeg's installed SDK to demux one H264 video stream and
remux packets to the existing authenticated RTSP-over-TCP endpoint. Its non-null
empty decoder whitelist blocks codec opens during SDK stream-info probing; the
metadata must be available from MPEG-TS/H264 headers. The context setting is checked
before probing. It does not decode/encode pixels. Public dimensions and source bounds are checked; packet
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
and lose compressed bytes before the SDK reads `pipe:0`. Regular file stdin is
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

Annotated ingest uses the same authenticated camera connection and credential/CRL
lease as ordinary ingest. Its bounded Rust stage inserts source-frame identity
into each encoded access unit before passing it to the same SDK publisher.
Annex-B has no container timestamps: Rust supplies a monotonic receipt clock in
the SDK output's actual time base, with strictly increasing ticks even for burst
input. This clock supplies media timing only; it never identifies a decoded
image or claims a capture timestamp. MPEG-TS and reader inputs retain their
native clock-loss refusal. Decoder annotations still require the exact in-band
identity and the independent alternating-pixel witness. Missing/ambiguous/stale
identity remains unavailable.

## Build and qualify

From `gateway/`, build normal ingest and the optional native publisher:

```sh
cargo build --locked -p nxrs-camera-ingest --bin nxrs-camera-ingest
cargo build --locked -p nxrs-camera-ingest --features publisher-sdk --bin nxrs-rtsp-publisher
cargo test --locked -p nxrs-camera-ingest --features publisher-sdk --all-targets
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

The Linux CI additionally builds a test-only native observer that terminates a
process if the real SDK successfully opens any decoder. Its positive control
opens an installed H264 decoder: normal execution must succeed and observed
execution must terminate with 91. All real publisher controls then run with that
observer loaded and must pass without opening a decoder. This is SDK behavior
evidence, not a sanitizer or proof of native parser memory safety. The observer
and control live under tests and are never linked into product executables.

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

These processes share a service UID. The strict Linux ingest service now requires
`nxrs-media-sandbox` for its encoder; the helper closes inherited descriptors,
applies mandatory Landlock ABI3 filesystem restrictions and seccomp, and then
executes FFmpeg. Only the selected executable, system library directories,
loader/time files and `/dev/null` are readable. Sockets, io_uring, cross-process
memory/FD access and signal injection are denied. Credential configuration inside
those read grants is refused. A failed runtime probe or sandbox launch cannot
fall back to an unconfined encoder. Omission of `media_sandbox` is retained for
development execution outside the strict service.

Prior ingest-only hosted evidence is linked from security issue #37; refreshed
recorder composition evidence is required for this change. File metadata remains
visible; kernel/root compromise and malicious installed launcher/library code
remain outside it. The publisher still consumes credentials. The activity worker now uses a
separate [RTSP remux reader](reader.md); its token never enters the motion decoder
or thumbnail finalizer. Strict Linux recording requires per-attempt/per-job
confinement. Fresh composed evidence and native reader/publisher custody
qualification remain required before claiming whole media-process isolation.
The original token and TLS key files still need protected storage and stronger
process/UID separation. Rust-owned preamble/URL buffers are zeroized on drop;
native SDK copies and process memory are not proven zeroized.

No native SDK distribution is certified, artifact-pinned or covered by RustSec
alone. Record actual SDK/executable hashes, package dependencies, advisories,
licensing, native fuzz/sanitizer evidence and service deployment qualification
before release. This change does not implement camera TLS on a board, hardware
key custody, publisher revocation/rotation, recording encryption or FIPS
validation. Those gates remain open in #33/#35–#38.
