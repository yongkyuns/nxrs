# Native image2pipe and JPEG decoder qualification

Issue [#38](https://github.com/yongkyuns/nxrs/issues/38) requires native parser
sanitizers in addition to Rust framing checks. This fixture builds the exact
[FFmpeg 9.0.2 release source](https://ffmpeg.org/download.html) and the already
locked libfuzzer-sys 0.4.13 engine from checksum-verified archives. The
[dependency pin](../../security/native-media-dependencies.json) agrees with the
installed Homebrew source recipe. It selects a **qualification component**,
not a new gateway runtime distribution.

The C driver under `gateway/tests/native/` calls the actual FFmpeg image2pipe
demuxer, MJPEG parser and MJPEG decoder through a fragmented, memory-only AVIO
input. It uses the documented [send/receive decoder API](https://ffmpeg.org/doxygen/trunk/group__lavc__encdec.html).
FFmpeg's native code is compiled with AddressSanitizer, UndefinedBehaviorSanitizer
and comparison/edge coverage instrumentation; assembly and unrelated codecs,
network protocols, hardware acceleration and automatic external dependencies
are disabled. This is deliberately a wider JPEG component input domain than
the production Rust admission guard, which accepts only bounded baseline JPEGs
of the exact configured dimensions. That guard and production media policy
are unchanged.

The qualifier refuses incomplete instrumentation symbols in each actual
decoder/parser/demuxer object. It compiles an additional real decoder object
without instrumentation and requires that the evidence check reject it.
Independent test-only heap-overflow and invalid-shift programs must actually
terminate with their respective ASan/UBSan diagnostics. These are activation
controls, not discovered FFmpeg defects.

Before mutations, the same native decode implementation must decode the public
synthetic 320x240 JPEG into exactly one image with the expected luma checksum
(all 76800 luma samples equal29); a separate FFmpeg raw decode established that
control. A malformed JPEG must produce no frame. The fresh corpus also includes
truncated and concatenated images. Input fragmentation varies from1 to256 bytes.
No private camera footage or vulnerability samples are downloaded.

Bounds:512KiB stream,153600-byte decoded packet,2073600 decoded pixels,
64MiB per native allocation, eight input packets,16 returned frames per receive
phase, two decoder threads,100000 mutations or60 seconds, five seconds per
input and1GiB RSS. The parent also bounds build/run time and output, preserves
the owned group leader until process-group retirement, and leaves logs, corpus
and crashes private. LeakSanitizer is disabled for cross-host consistency; no
leak-free, complete native-parser, optimized-assembly, distribution, MCU,
physical, FIPS or native-advisory-closure claim follows.

Use Python3.12+, GNU make and Clang18 on Linux or Apple Clang21 on macOS. The
test engine is built from its pinned C++ sources, so a system libFuzzer runtime
is not required. Run only after committing a clean source tree:

```sh
python3 tools/native-media/qualify.py --work /absolute/new/private-run
# Ubuntu's versioned compiler names:
python3 tools/native-media/qualify.py --cc clang-18 --cxx clang++-18 \
  --work /absolute/new/private-run
```

An optional `--archives /private/cache` reads `ffmpeg.tar` and `libfuzzer.tar`;
the same archive digests and extraction limits are mandatory. The qualifier
never installs a library, replaces a system FFmpeg, starts a service, or accesses
a camera/network media endpoint.

The sole public result is a bounded `qualification.json`: clean checkout and
complete local source inventory, upstream/configuration/compiler identities,
actual library/object/executable hashes, executed coverage/RSS counts, activation
controls and explicit scope flags. Build paths are redacted. CI uploads this
file alone for seven days. Review [upstream advisories](https://ffmpeg.org/security.html)
and qualify the actual product distribution separately; a passing fuzz budget
does not establish absence of vulnerabilities.
