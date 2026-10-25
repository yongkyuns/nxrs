# Minimized gateway media runtime

The audited Ubuntu FFmpeg distribution loads multimedia, desktop and font
libraries beyond nxrs's selected media functions. This build uses the exact
FFmpeg source pin already used by the native parser qualification and an exact
VideoLAN x264 stable revision, with automatic dependency discovery disabled.
It retains JPEG/H.264 codecs, the gateway's RTSP/MPEG-TS paths, motion sampling,
clip concatenation/MP4 and JPEG thumbnails. Resolved container/CLI prerequisites
are retained explicitly, including the upstream RTSP dependency on ASF/RM
demuxer code and MPEG-TS dependency on ADTS/LATM packet muxers. This does not enable other video/audio decoders: Rust retains its
H.264-only bounded stream admission and explicit RTSP/TCP whitelist. Hardware,
device and audio codec admission is refused.

`prepare.py` runs only as an ordinary user on disposable GitHub-hosted Ubuntu
24.04. It refuses personal hosts before any changes. It creates a fresh owned
`/usr/lib/nxrs-gateway-media-qualification` directory and installs only there.
Existing system binaries/libraries remain the independent fixture generators.
Explicit rpaths use the owned public library prefix under the existing sandbox
read grants. No loader environment variable or widened sandbox grant is needed.
The publisher/reader retain shared-library linkage so their independent actual
native-decoder-open witness continues to work. Rust keeps all media lifecycle,
credential handling, retry, resource ownership and cancellation behavior.

The bounded public build receipt records archive digests, exact source inputs,
resolved configuration, actual compiler/tool images and package revisions,
runtime file hashes and actual loader graphs. A build
alone always has `qualified=false`; integration and advisory admission are
separate. The x264 source revision is **not** claimed to fix CVE-2025-25467.
Ubuntu's source-package advisory ranges do not qualify this custom distribution.
Full upstream/native advisory closure, authenticated release provenance,
physical/FIPS qualification and deployment permission remain false.

The workflow exercises the same native publisher/reader ownership and decoder
refusal suites and actual process-isolation controls with this runtime. Full
The full SDK mutual-TLS and encrypted-recording workflows explicitly select
this runtime for production encoding, motion decoding, clip and thumbnail work.
Independent distribution FFmpeg remains the fixture generator and decode oracle.
The complete new composition must pass before deployment-candidate replacement.
No production host installation or deployment is performed by this fixture.

`qualify.py` joins the exact executed build, reader and isolation receipts,
requires the SDK binaries to resolve all four multimedia libraries to the
hashed custom runtime, then encodes and fully decodes a 96-frame finalized
MP4 clip and creates its JPEG thumbnail. It exports hashes/counts only. This
functional scope does not replace the outstanding full encrypted composition.

The independent native parser fuzz fixture does not qualify every parser or
optimized assembly path in this runtime. Both coverage flags remain false.

`gateway/tools/gateway_media_evidence.py` verifies the complete native build and
executed qualification source inventories, source pins, runtime payloads, system
images and exact SDK executables before returning a compact receipt. The same
check runs before and after SDK media and full encrypted-volume composition.
The unprivileged staged fixture checks the same public source and runtime bytes;
the outer fixture additionally requires the original clean checkout and exact
isolation receipt. Missing/changed receipts, source, library or executable bytes
refuse admission. The full build/native qualification JSON remains in CI artifacts;
the composition carries their digest joins without duplicating full inventories.
