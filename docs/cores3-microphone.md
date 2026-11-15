# CoreS3 microphone bring-up

This is an opt-in **native NuttX hardware-qualification path** for the original
M5Stack CoreS3 K128. It is not a portable Rust audio HAL, speaker implementation,
or a claim that physical capture has passed. Existing CoreS3 navigation and
LCD profiles are unchanged. `std-demo` is reused as the firmware build entry;
NuttX's `nxrecorder` provides the recording interface, so there is no duplicate
recording application.

## Wiring and ownership

| Signal | Connection |
| --- | --- |
| ES7210 control | I2C0, address `0x40`, SDA GPIO12, SCL GPIO11 |
| I2S master clock | GPIO0 |
| I2S bit clock | GPIO34 |
| I2S word select | GPIO33 |
| I2S data into ESP32-S3 | GPIO14 |
| Microphone/codec supply | AXP2101 ALDO2, `VDDA_3V3`, 3.3 V |

The two physical microphones feed ES7210 MIC1/MIC2. Do not interpret the native
codec's four-channel capability report as four onboard microphones. Qualification
uses **two channels, signed 16-bit little-endian PCM, 16 kHz**.

`0124-esp32s3-cores3-microphone.patch` adds the board registration, selected only
by `CONFIG_ESP32S3_M5_CORES3_MIC`. It requires the physical pins and RX-only I2S0
master mode with MCLK both in Kconfig dependencies and C compile-time guards.
It enables only ALDO2 using masked updates: AXP2101 address `0x34`, register
`0x93` low five bits = 28, register `0x90` bit 1. An already-enabled rail at a
conflicting voltage is rejected rather than changed. AW9523 P0_2 is the speaker
reset, not a microphone enable; the patch does not touch it, charging settings,
or the 5 V boost.

After the supply settles, an ES7210 register read verifies address
acknowledgement before I2S/codec allocation and registration of
`/dev/audio/pcm_in0`. This is not a chip-ID, clock, or PCM-validity test.
Board initialization does not start a recording. The microphone supply remains
enabled for the successful endpoint's boot lifetime, including between sessions.

Native NuttX owns the audio codec, DMA, interrupts and recorder buffers.
The pinned I2S/ES7210 APIs have no board-level destruction operation. Initialization
is called only from the boot thread and caches both success and failure: no retry
allocation loop. On failure, changed ALDO2 fields are restored where I2C permits;
restoration errors are logged, and the voltage is not changed if disabling the
rail fails. Bus references retained by an allocated codec are kept until reset,
even if registration failed. Reboot after a failed bring-up.

## Build and first checks

```sh
python3 -m unittest discover -s tests/host -p test_cores3_microphone.py -v
cargo nxrs build --app std-demo --platform cores3 \
  --profile cores3-microphone --out target/cores3-microphone
```

Use the [CoreS3 flashing procedure](m5stack-cores3.md) for the produced image.
The new profile keeps Wi-Fi, BLE, speaker transmission, LCD and PSRAM disabled
to isolate initial audio qualification. It uses internal RAM and includes tmpfs.
Verify the resolved `.config` requirements, not merely the TOML requests, before
claiming the firmware build passed. A dropped microphone option is a build error.

First check that boot has no microphone registration error and that
`/dev/audio/pcm_in0` exists. Then exercise start/stop without retaining audio:

```text
nsh> ls /dev/audio
nsh> nxrecorder
nxrecorder> device /dev/audio/pcm_in0
nxrecorder> recordraw /dev/null 2 16 16000
nxrecorder> stop
nxrecorder> q
```

Wait briefly between `recordraw` and `stop`. A successful `/dev/null` run checks
control flow only: it does not prove either microphone produced valid sound.
Do not substitute `cat /dev/audio/pcm_in0` for the native audio ioctl/buffer
protocol. A future Rust adapter must wrap that protocol, not invent `Read` PCM
semantics for this endpoint.

## Retain a short sample

Check `free` first. With at least 128 KiB free, mount a fresh tmpfs and retain
**no more than one second** initially. Stereo 16-bit 16 kHz costs 64,000 bytes/s
before filesystem and recorder overhead. tmpfs is not a bounded recording sink;
never leave the recorder running. With less free memory, stay with `/dev/null`
or use an already-qualified external writable filesystem.

```text
nsh> free
nsh> mkdir /tmp
nsh> mount -t tmpfs /tmp
nsh> nxrecorder
nxrecorder> device /dev/audio/pcm_in0
nxrecorder> recordraw /tmp/mic.pcm 2 16 16000
nxrecorder> stop
nxrecorder> q
nsh> ls -l /tmp/mic.pcm
```

Skip creating/mounting `/tmp` when it is already mounted. Speak or clap during
the short recording and issue `stop` within one second. Copy the file using an
existing file-transfer setup; this profile does not add a network transfer
service. On a host with FFmpeg, convert the raw bytes for listening:

```sh
ffmpeg -f s16le -ar 16000 -ac 2 -i mic.pcm mic.wav
```

Delete the RAM recording before repeating. Do not claim channel identity or
independent microphone response from identical/nonzero buffers alone.

## Qualification gates

The added host test compiles the actual C source extracted from the patch using
mock native declarations. It exercises 10 bring-up scenarios and 14 I2C failure
cases, including writes which reach the PMIC but return an error. It also checks
profile constraints and rejects a wrong MCLK pin or enabled TX at compile time.
These tests do not validate the NuttX ABI, Kconfig resolution, DMA, scheduling,
physical power, acoustic response, or long-running recording.

Before promoting this path from draft/bring-up support, retain evidence for:

1. The exact-head firmware build and resolved `.config`, map and image; also
   build the unchanged default navigation profile to verify audio stays absent.
2. Cold boot and warm reset on CoreS3, with microphone endpoint registration,
   correct power and I2S clocks, and no speaker output.
3. Real PCM with frame-aligned length, meaningful per-channel level changes,
   no all-zero/stuck data, acceptable clipping, and independent stimulus near
   each microphone. Keep the actual recording and measured duration.
4. Repeated record/stop and restart, bounded stop latency, stable free memory,
   and timeout/fault behavior. Test a second attempted recorder session.

The pinned upstream ES7210 reset routine ignores some register-write failures;
a successful boot-time probe does not repair runtime failure reporting. Its
channel/format capability claims also exceed this board's two-microphone,
16-bit qualification scope. Do not expose broader claims through a Rust HAL
without fixing and testing the corresponding native behavior.

### Evidence for this change

Executed locally: the four host unittest methods (24 runtime scenarios plus
two expected compile failures), strict host C compilation, TOML parsing and
patch application against the reconstructed post-0123 CoreS3 board files. The
three reconstructed existing board files match the postimage Git hashes from
patch 0123. No Rust/Xtensa toolchain or connected CoreS3 was available in that
environment. Full SDK patch-series application, profile resolution, firmware
linking, CI qualification and physical recording remain unverified.

### Primary references

- [M5Stack CoreS3 documentation](https://docs.m5stack.com/en/core/CoreS3).
- [CoreS3 v1.0 schematic](https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/490/Sch_M5_CoreS3_v1.0.pdf), power and audio sheets.
- [M5Unified AXP2101 voltage/enable implementation](https://github.com/m5stack/M5Unified/blob/master/src/utility/power/AXP2101_Class.inl).
- [Pinned NuttX ES7210 driver](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/drivers/audio/es7210.c).
- [Pinned NuttX ES7210 board example](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/boards/xtensa/esp32s3/lckfb-szpi-esp32s3/src/esp32s3_board_es7210.c).
