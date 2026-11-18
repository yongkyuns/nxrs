# Credentialed RTSP publisher

This single-use Rust helper separates image encoding from authenticated
publication (#37). The encoder sends compressed MPEG-TS, Annex-B or Matroska to ingest;
ingest relays it through bounded storage to this helper. Only the helper's stdin
receives the 64-byte lowercase-hex token preamble. Its arguments are public:

```sh
nxrs-rtsp-publisher 127.0.0.1:8554 publisher_reference reference [annexb|mpegts-av|matroska-av]
```

The default input is H264 in MPEG-TS. The optional `annexb` mode uses a monotonic
receipt clock in the output time base; frame identity remains in the unchanged
SEI payload. The opt-in `mpegts-av` input requires exactly one H.264 track
and one mono Opus track. Either can appear first. Supply timestamps from a
common source clock in MPEG-TS. The helper rescales each packet to its own
RTSP track clock and shares a single publication. Regular-file stdin, invalid credentials/targets and unsupported media
are refused. The helper consumes exactly the preamble without buffered prefetch.
It uses the installed FFmpeg SDK for demuxing and RTSP-over-TCP remuxing. An empty
decoder whitelist prevents image decoding during stream probing. Native logs
are disabled before credential handling; only fixed errors and numeric packet
progress leave the helper. [Generated audio/video qualification](../tests/matter/README.md#timestamped-audiovideo-publication).

`matroska-av` accepts the output of the authenticated A/V ingest encoder. An
internal text track carries bounded frame annotations. A missing, repeated or
mismatched timestamp retires the publication. The publisher inserts the matched
annotation as H.264 SEI, omits the text track from RTSP and reports packet progress
independently for H.264 and Opus.

FFmpeg can report the ingest encoder's 16 kHz input rate in its Matroska
Opus track. The publisher accepts that rate for this track and advertises
the required 48 kHz Opus RTP clock, preserving the packet timestamps through
rescaling. [Opus RTP clock](https://www.rfc-editor.org/rfc/rfc7587.html#section-4.1).

Build explicitly from `gateway/` with installed FFmpeg development headers,
libraries, libclang and pkg-config:

```sh
cargo build --locked -p nxrs-rtsp-publisher
```

`FFMPEG_DIR` can select an installed SDK prefix. Default controller/ingest builds
need no SDK. Install the helper root-owned at the required absolute `publisher`
config path. Existing configs must add that field while preserving all current
tokens, TLS enrollment and denial state; there is no credentialed-encoder fallback.

Ingest supervises both children, source/TLS leases and encoder/packet progress.
Initial output has the existing ten-second budget; subsequent progress and pipe
I/O are bounded. Failed spawn, child death, cancellation and source failure reap
both children before another admission. Use the generated service for whole-group
SIGKILL cleanup; it retains the 256 MiB/128-task limits with three processes.

The existing media job runs actual SDK/authentication controls and a decoder-open
observer with an independent positive control, then ordinary/annotated media and
service recovery. Packet-size checks occur after SDK allocation. This is not a
native memory-safety, physical camera or resource-peak qualification. Keep native
dependency/advisory review and release provenance current. Shared-UID file/memory
access and native credential copies still need isolation/custody qualification;
Rust preamble/URL buffers are zeroized, native copies are not proven zeroized.

Both production launch sites (`ingest/src/runner.rs` and `video.rs`) use this
helper. Isolated synthetic publishers in `reference_integration.py`,
`revocation_reference.py`, `vpn_reference.py`, `systemd_reference.py` and
`clients/ios/ci/native_reference.py` still put disposable fixture tokens in
FFmpeg arguments; they are test-only launchers, not production configuration
templates.
