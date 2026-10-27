# Recorder RTSP credential separation

The activity worker gives `nxrs-rtsp-reader` exactly 64 lowercase hexadecimal
credential bytes through an inherited pipe. Its arguments contain only a literal
loopback IPv4 endpoint, username and stream identifier. It clears the inherited
environment. The reader uses the installed FFmpeg SDK to demux one H.264 video
stream and remux compressed packets to MPEG-TS stdout; it never opens a decoder
or encoder. SDK diagnostics are disabled. Stderr contains only counted packet
progress or a fixed failure category.

The activity worker relays compressed bytes through a separate pipe to FFmpeg.
The decoder receives neither the token nor a credentialed URL/playlist. Reader
and decoder exits, packet progress, forwarding, startup, decoded-frame progress,
cancellation and reaping are supervised on the worker's existing Tokio runtime.
First progress is bounded by 15 seconds, later progress by eight; cleanup must
confirm both children have exited before reconnect admission. A failed cleanup
remains terminal. Shared gateway process supervision lives in `media-process`;
the single-threaded `media-sandbox` launcher has no Tokio dependency or runtime.

The SDK may initially lack RTP decode timestamps. The Rust reader discards at
most 32 initial packets without both PTS and DTS. It never manufactures source
timestamps. Timestamp loss after output starts, an exhausted startup allowance,
an empty stream, invalid media or a native error retires the reader. Existing
stream limits remain: one H.264 stream, even dimensions up to 1920x1080, bounded
extradata and packets. Only RTSP-over-TCP input is selected, with an empty decoder
whitelist verified before stream-info probing. The reader is single-use; its
supervisor's process barrier also bounds blocked native operations.

## Required Linux recording isolation

The encrypted activity overlay installs explicit reader, FFmpeg and sandbox
paths and requires `--require-media-isolation`. The activity worker probes the
same installed helper before storage/source startup. Every motion decoder and
clip/thumbnail finalizer runs through the launcher, with filesystem writes
restricted to a fresh owned 0700 per-attempt or per-job directory. The activity
root, token, controller configuration and other recordings are outside the grant.
Parent reads/copies native outputs through bounded, no-follow regular-file
descriptors, rejecting symlinks, hardlinks, FIFOs and other special files. It
retains the opened inode across path replacement. The sandbox rejects fork and
process-creating clone calls; clone3 returns ENOSYS, permitting libc to fall back
to the inspected thread-only clone ABI. FFmpeg threads stay in the supervised
thread group ([native clone contract](https://man7.org/linux/man-pages/man2/clone.2.html)).
Hosted controls require 14 successful unconfined operations, then deny all but
the intended private work write while actual multithreaded encoding still works.
Known credential placement in executable/library/read grants is refused. Missing
or unsupported enforcement cannot silently fall back. Development configurations
may explicitly omit `media_sandbox`; they still require the separate reader and
provide no decoder confinement claim.

Build both remux helpers against reviewed installed SDK headers/libraries:

```sh
cargo +1.90.0 build --manifest-path gateway/Cargo.toml --locked \
  -p nxrs-camera-ingest --features publisher-sdk --bins
cargo +1.90.0 build --manifest-path gateway/Cargo.toml --locked \
  -p nxrs-camera-activity -p nxrs-media-sandbox
```

Install the reviewed reader and launcher at `/usr/local/libexec/nxrs-rtsp-reader`
and `/usr/local/libexec/nxrs-media-sandbox` when installing the encrypted overlay.
Keep the binaries, loader and system library hierarchy administrator-controlled.
The Linux kernel must support fully enforced Landlock ABI3 and seccomp. Do not
change a live system's security policy to turn an unsupported probe into a pass.

## Evidence and remaining gates

`reader_reference.py` uses actual authenticated loopback MediaMTX, native SDK
publication and independent FFmpeg/ffprobe decoding. It requires wrong password,
wrong role/path, malformed/truncated credentials and regular-file refusal. Linux
also uses the native decoder-open observer with a positive control that opens an
actual H.264 decoder. A macOS media pass without that observer proves remux and
refusal behavior only. `activity_credentials.py` observes the two distinct input
pipes, cancellation, either-child failure, stalled status, partial-spawn cleanup,
unsafe files and required/failed enforcement.

Hosted recording and encrypted composition require the launcher. Composition
verifies an actual confinement receipt for the exact launcher and FFmpeg hashes,
then binds it to real recording, playback and authenticated backup recovery.
Fresh recorder CI evidence is required before claiming those composed gates for
this change. One earlier local full-recording attempt failed to create a motion
clip despite decoded frames; a later run with fixed stage diagnostics passed the
full recording/restart/source-recovery path. The earlier failure's cause remains
unproven; it is not erased by the later pass.

The credentialed reader/publisher still parse native media and share a service
UID. This change isolates decoders; it does not qualify native-parser memory
safety, protected credential custody or stronger broker UID separation. Native
SDK credential copies are not proven zeroized. Kernel/root compromise, malicious
installed libraries, filesystem metadata visibility, physical device/entropy/
time/boot qualifications, native dependency advisory closure, formal FIPS
validation and release/deployment permission remain separate open gates.
