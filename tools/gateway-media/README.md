# Minimized gateway media runtime

The audited Ubuntu FFmpeg distribution loads multimedia, desktop and font
libraries beyond nxrs's selected media functions. This build uses the exact
FFmpeg source pin already used by the native parser qualification and an exact
VideoLAN x264 stable revision, with automatic dependency discovery disabled.
It retains JPEG/H.264 codecs, the gateway's RTSP/MPEG-TS and annotated Annex-B paths, motion sampling,
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
separate. The x264 source carries an exact source-hash-bound parameter cleanup backport;
its narrow constructor-failure controls do not establish full CVE/native coverage.
Ubuntu's source-package advisory ranges do not qualify this custom distribution.
Full upstream/native advisory closure, authenticated release provenance,
physical/FIPS qualification and deployment permission remain false.

The workflow exercises the same native publisher/reader ownership and decoder
refusal suites and actual process-isolation controls with this runtime.
The isolation witness encodes and independently decodes sixteen frames in each
of MPEG-TS and raw H.264, requiring sixteen Annex-B access-unit delimiters. Both
the H.264 muxer and those executed counters are required for runtime admission.
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

The payload inventory includes library symlinks as well as regular files.
Retargeted/missing aliases, extra files, changed bytes or links escaping the
owned runtime refuse admission before or after the integration. Compact proofs
bind the combined file-and-link inventory.

Deployment rendering also preserves resource selection: the strict VPN renderer
accepts `--ffmpeg /absolute/installed/ffmpeg`, TLS staging retains that field,
and the encrypted-volume overlay retains the source activity bundle’s selected
encoder. Renderers prepare new private files only; they do not install the
runtime or change running services. The qualification prefix is a CI resource,
not an instruction to deploy an unapproved release.

The local activity supervisor retains an unreaped process-group leader through
all retirement signals, observes exit with the host kernel, and joins its
diagnostic reader before replacement. Cleanup attempts every owned role and
reports failure when retirement is unproven. Its Mac/Linux synthetic controls
are independent of the device and full encrypted-volume qualifications.

The x264 backport follows [upstream proposal !183](https://code.videolan.org/videolan/x264/-/merge_requests/183),
which is closed and unmerged, and retains its exact proposal/base identities.
It frees the encoder's copied parameter strings when encoder initialization
fails. The recipe pins the original archive, patch bytes, and complete encoder
source before and after application; changed inputs or reapplication refuse.
The build receipt and runtime admission require the applied patch identity.
This is a local backport, not a claim of an upstream release fix.

The hosted native workflow separately builds the original and patched static
libraries with Clang ASan, UBSan and LeakSanitizer. An owned synthetic API
witness exercises 64 refused constructors across 8/10-bit paths, including
caller-owned and callback-freed parameter lifetimes, missing-matrix refusal,
and two successful one-frame encodes. The original must actually leak, exit
with the required sanitizer status and finish the controls; the patched build
must finish with no sanitizer error. Raw sanitizer output stays private. Only
bounded counters, source/compiler/library/binary identities and scope flags
are retained. These controls disable assembly and do not qualify every later
constructor failure, production assembly, full native advisories or hardware.

Reader/publisher receipts and the aggregate runtime receipt share one Rust
source inventory, including the actual local detection-wire Cargo dependency.
A regression walks the ingest crate's local Cargo dependencies and requires
every manifest and Rust source in the executed reader receipt. Missing protocol
sources still refuse aggregate qualification before guest or storage admission.
