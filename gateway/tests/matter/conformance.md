# Camera qualification boundaries

Issue [#388](https://github.com/yongkyuns/nxrs/issues/388) remains the release
checklist. Protocol interoperability does not establish device conformance.
This inventory uses connectedhomeip `v1.5.1.0`
(`abcc720b48c5e59c0edcfe65c516f76ca9448aa3`) and Home Assistant `2026.11.2`
(`ff5d56b825a0fb66b03d01bc297d49e206cdaba3`). Do not publish the specification
XML in Nxrs; follow the upstream source links.

| Requirement | Current fixture / remaining work |
| --- | --- |
| Camera AV Stream Management | The Camera type requires video, audio and snapshots. Host trials use generated H.264 and microphone-format mono Opus. The snapshot variant also returns bounded JPEGs through the real Matter capture command. Physical camera and microphone input, timing and client qualification remain release requirements. [Snapshot test](README.md#matter-jpeg-snapshots). |
| WebRTC | The Camera descriptor lists the required Provider server and Requestor client. Both offer directions and trickled candidates work in the independent receiver. Home Assistant's browser offer includes an audio section before video. Reject unsupported media while preserving SDP indices. |
| Root-node conditions | The root exposes the configured wired power supply and TimeZone. The clock tries selected-link DHCP, DNS-SD, a trusted Matter peer over CASE, and configured SNTP in that order. The TLS management variant exposes both mandatory root servers. Independent fabrics verified endpoint references, credential operations, and retirement. A chunked TCP certificate attribute returned both maximum-size chains. A combined invoke that exceeded the frame budget returned `ResourceExhausted` and kept the session usable. Remaining platform DHCP sources and full Matter conformance qualification stay open. [Clock tests](README.md#trusted-matter-time) · [TLS witness](README.md#tls-management-over-matter). |
| Base device requirements | The Camera implements Descriptor and the Binding server required by a simple device with a client cluster. The SDK persists fabric-scoped bindings. Startup removes bindings whose fabrics were removed before an interruption. Negotiation replies use the authenticated requestor on the active session. [Camera endpoint test](README.md#provisioned-device-attestation-and-camera-endpoints). |
| Optional camera functions | Identify, occupancy, zones, settings and push transport are optional for the Camera type. The camera omits Zone Management and digital PTZ. Occupancy is a separate sibling endpoint. |
| Occupancy Sensor type | A separate Occupancy Sensor requires Identify and Occupancy Sensing. Its availability must expire independently from live video. |
| Persistent identity | The configured bridge keeps one camera's two endpoint identities across a path change and restart. Two independent administrators also exercised selective fabric removal, process interruption and reuse of a retired fabric index without inherited sessions. Final removal also clears the installation's records in the same durable write as the last fabric. The old invitation closes and a new controller keeps the endpoint identities with default settings. On the wired host fixture, a new fabric and its allocator cursor commit together. An existing administrator can replace a secondary enrollment whose confirmation was lost. An operator can explicitly remove an abandoned sole enrollment without changing camera identity. [Commissioning interruption test](README.md#interruption-during-commissioning). [Configured bridge test](README.md#configured-camera-and-durable-endpoints) · [Access recovery test](README.md#interrupted-access-changes) · [Last-fabric test](README.md#last-fabric-removal-and-restart). |
| Access within one fabric | Two separately commissioned controller nodes decoded the same camera. Withdrawing one node’s WebRTC permission ended its active and pending sessions. The other controller kept receiving frames from the same upstream reader. Restoring the permission admitted a fresh stream. The tested path used generated video on macOS. |

The commissioning fixture accepts a private device-specific verifier and requires
an explicit local request to open the initial 180-second window. The provisioned
attestation variant loads a protected DAC, key, PAI and declaration, verifies the
declaration and identity at startup, and requires the same device identity on
restart. The qualification input is the pinned development chain, enabled
explicitly. Shipping requires commissioning with product-issued credentials and
qualification of the private-key lifecycle.
[Provisioned device attestation](README.md#provisioned-device-attestation-and-camera-endpoints).

The cluster's feature choices and the device type's requirements are different
constraints. A video-only Camera AV Stream Management cluster may work with a
client while failing the standard Camera device type's mandatory feature set.
Do not call the video-only fixture a conforming product.

## Microphone delivery

The release requires capture from a real microphone at the camera. Physical
audio qualification is waiting for microphone hardware. The Freenove OV3660
profile still needs an audio binding and Nxrs needs an audio capture HAL.
Implement and qualify these steps in order.

1. Select a microphone and verify its clock, data format and wiring against
   the actual board revision. The pinned DevKit BSP already calls
   `board_i2sdev_initialize` with independent receive and transmit flags.
   Its example audio configuration disables I2S0 RX and uses GPIO 16, 6, 5
   and 7, all occupied by the camera. Check the schematic, flash/PSRAM, USB,
   boot straps, DMA capacity and the effective NuttX configuration before
   admitting an I2S receive resource in a derived product profile.
2. Use the BSP's existing receive endpoint. By default port 0 registers
   `/dev/audio/pcm_in0` and port 1 registers `/dev/audio/pcm_in1`. Derive
   the path from the chosen BSP configuration. Rust opens the descriptor with
   `std::fs`. The generic I²S lower half has no read operation, so a plain
   `File::read` on this endpoint returns zero bytes. The NuttX provider must
   reserve and configure capture, register a notification queue, enqueue a
   fixed pool of audio buffers and process dequeues in task context. Keep the
   target-header translations private to the provider. Check the returned
   sample count, active capture generation and accepted format.
3. Negotiate a versioned audio-capable stream with the gateway. Preserve the
   existing JPEG and optional detection path for older peers. Start with signed
   16-bit mono PCM at 16 kHz. A 20 ms chunk contains 320 samples (640 bytes),
   or 256 kbit/s before framing. Decode the microphone's actual slot width and
   channel selection on the camera. Carry source generation, sample sequence,
   capture time and discontinuities. Timestamp audio and video against the same
   source clock. Encode Opus once at the gateway. A 20 ms Opus packet advances
   its 48 kHz RTP clock by 960 ticks. [RTP clock specification](https://www.rfc-editor.org/rfc/rfc7587#section-4.1).
4. Publish synchronized H.264 and Opus tracks and derive the Matter audio
   capability from the acquired format and negotiated encoding. Exercise
   allocation, deallocation, microphone mute, gain where supported, privacy
   and active-session revocation. The host fixture holds the microphone
   allocation while the WebRTC peer is active. An independent controller
   confirmed that deallocation fails while streaming and that revoking gateway
   access stops both tracks and releases the reference. The host also persists
   microphone mute and both software privacy settings. Check microphone mute
   on the real capture path, and define what each privacy setting enforces in
   the product recorder and detector. Talkback requires separate speaker
   hardware. [Host privacy test](README.md#matter-microphone-mute-and-live-privacy).
5. Use a known acoustic pattern at the physical microphone and a visible timing
   cue. Compare decoded samples and rendered video in Home Assistant and its
   iPhone app, measure A/V skew and drift, and verify mute, disconnect,
   reconfiguration and recovery. Keep private household audio out of evidence.

### Receive-only I²S candidate

On the published FNK0085 pinout, GPIO14, GPIO21 and GPIO1 are brought out
and unused by the current camera, SD card, USB and LED wiring. Try GPIO14
for I²S0 bit clock, GPIO21 for word select and GPIO1 for data input after
confirming the actual PCB revision. For an INMP441-format reference, use
Philips I²S with two 32-bit slots at 16 kHz (1.024 MHz bit clock). Select the
microphone's populated slot, then convert its signed 24-bit values to PCM16.
A 20 ms stereo DMA block is 2,560 bytes and yields 640 bytes of mono PCM16.
Verify the slot layout and discard startup samples within the microphone's
settling interval. [Freenove pinout](https://github.com/Freenove/Freenove_ESP32_S3_WROOM_Board/blob/main/ESP32S3_Pinout.png) · [INMP441 electrical and timing specifications](https://invensense.tdk.com/wp-content/uploads/2015/02/INMP441.pdf).

```sh
cargo nxrs config show --app home-cam --platform wroom \
  --kconfig-enable AUDIO --kconfig-enable DRIVERS_AUDIO --kconfig-enable AUDIO_I2S \
  --kconfig-enable ESPRESSIF_I2S0 --kconfig-enable ESPRESSIF_I2S0_RX \
  --kconfig-enable ESPRESSIF_I2S0_ROLE_MASTER \
  --kconfig-enable ESPRESSIF_I2S0_DATA_BIT_WIDTH_32BIT \
  --kconfig-disable ESPRESSIF_I2S0_TX --kconfig-disable ESPRESSIF_I2S0_MCLK \
  --kconfig-set ESPRESSIF_I2S0_SAMPLE_RATE=16000 \
  --kconfig-set ESPRESSIF_I2S0_BCLKPIN=14 --kconfig-set ESPRESSIF_I2S0_WSPIN=21 \
  --kconfig-set ESPRESSIF_I2S0_DINPIN=1
```

Native `olddefconfig` accepted every override on the current WROOM startup
profile. Camera, Wi-Fi, USB and PSRAM settings matched the default profile.
The I²S selection adds one NuttX high-priority worker with a 2 KiB stack.
The WROOM startup firmware linked with the receive driver, GDMA and board
registration present. It passed 122 Xtensa ABI witnesses. The 1,900,440-byte
image fits the 2 MiB MCUboot slot and verified against a disposable test root.
The logic-analyzer trace and device capture are still open.
[Configuration and build evidence](evidence.json).

Patch 0126 rejects incomplete receive frames and descriptor setup failures
before DMA submission. Failed submission releases any retained audio-buffer
reference and returns the container to its pool. The ESP32-S3 descriptor
builder terminates exhausted lists at the last available entry and returns
the short byte count. The host witness compiles the pinned source and
covers failure followed by successful retry. [Source regression](../../../tests/host/test_esp_i2s_rx_enqueue.py).

Patch 0127 keeps the ESP32-S3 RX interrupt route enabled on its registering
CPU. Transfer start and stop control delivery at the GDMA source. The RX
handler reads and acknowledges its completion under the queue lock. The
host witness checks that the route is enabled once during registration and
that the RX and TX handlers inspect only their own interrupt sources. The
two-core WROOM candidate linked and passed 122 ABI witnesses off-device.
[Interrupt regression](../../../tests/host/test_esp_i2s_rx_irq.py) · [Build evidence](evidence.json).

Two existing interfaces determine the work between capture and playback.

- Current camera ingest sends H.264. The publisher's raw Annex-B mode timestamps
  pictures at gateway receipt. Its new `mpegts-av` mode accepts timestamped
  H.264 and mono Opus together. The generated cue test reaches an independent
  RTSP decoder. Firmware and ingest must still preserve capture timestamps
  through the mux and retain the picture identity metadata. Exercise transport
  delays, lost audio chunks and simultaneous video-only playback. [Publisher input](../../publisher/src/sdk.rs) · [Publisher clock](../../publisher/src/main.rs).
- The pinned Matter Server intersects the offered audio codecs with the
  camera's microphone capabilities. Its default request uses 64 kbit/s. Test
  mono Opus allocation with that request, release it on the last viewer, and
  verify that two viewers share one encoder. Home Assistant enables talkback
  from the Speaker feature and duplex capability. A microphone-only profile
  should deliver camera audio with the phone microphone disabled.
  [Matter Server audio selection](https://github.com/matter-js/matterjs-server/blob/fc8aa79611aebbe3090a30813e7211f707b2131f/packages/ws-controller/src/camera/streamPolicy.ts#L543-L629) · [Home Assistant camera signaling](https://github.com/home-assistant/core/blob/ff5d56b825a0fb66b03d01bc297d49e206cdaba3/homeassistant/components/matter/camera.py#L134-L175).

Paired H.264/Opus uses Retina’s packet interface on one authenticated RTSP
session. The gateway bounds picture assembly and independent viewer queues,
maps the original 90 kHz and 48 kHz timestamps through RTCP sender reports,
and retires the source on packet loss, clock discontinuity or revocation.
The camera advertises one RTCP CNAME and MediaStream ID for its two tracks
within each peer connection. Home Assistant decoded the generated tone and
displayed the flashing image. Compare physical capture, display and acoustic
playout separately from Web Audio sampling. [Browser trial](README.md#matter-microphone-playback) · [RTP sender reports](https://www.rfc-editor.org/rfc/rfc3550#section-6.4.1) · [WebRTC synchronization](https://www.rfc-editor.org/rfc/rfc8834#section-12.2.3).

Patch 0128 makes stop retryable for receive-only ESP32-S3 I²S. It
masks the source, rejects new submissions, and retains the queued buffers
while GDMA is active or a submission or completion is in progress. Once idle,
the driver returns every buffer with zero valid bytes and releases its
references. A host witness exercises cancellation, callback re-entry, and
starting capture again after a full pool is returned. The two-core WROOM
firmware linked and passed 122 ABI witnesses off-device.
[Stop regression](../../../tests/host/test_esp_i2s_rx_stop.py) · [Build evidence](evidence.json).

The queued NuttX provider must retain its descriptor and buffer pool until
stop succeeds. Use a nonblocking completion queue sized for the outstanding
buffers and terminal notification. Mark the capture as retiring before
stopping and discard its queued samples. The generic I²S audio adapter
returns canceled buffers as ordinary dequeues and emits completion for the
last canceled buffer. A successful stop also covers an empty pool. Abrupt
close and physical stop, mute and reopen remain unqualified. Inject an RX
fault and verify that the provider reports an observable discontinuity or a
terminal failure. Verify mute and revocation with a known acoustic cue.
[I²S audio operations and callback](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/drivers/audio/audio_i2s.c) · [Audio upper half](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/audio/audio.c).

The profile admission review uses the [camera's current pin assignments](../../../platform/wroom/profiles/esp32s3-camera-stream.toml), [pinned BSP bring-up](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/boards/xtensa/esp32s3/esp32s3-devkit/src/esp32s3_bringup.c), [receive registration](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/boards/xtensa/esp32s3/common/src/esp32s3_board_i2s.c) and [example audio configuration](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/boards/xtensa/esp32s3/esp32s3-devkit/configs/audio/defconfig).

## Bridged camera and occupancy trial

The tested node has root 0, camera 1, aggregator 2 and occupancy 3. The
controller read root parts `[1,2,3]`, aggregator parts `[1,3]` and empty
parts lists on both children. Each child has its own Bridged Device Basic
Information cluster. Marking occupancy unreachable left the camera available.
Home Assistant rendered 60 advancing H.264 frames while the sensor was unavailable.

The order of two reports matters when a fresh observation changes the last
known value during an outage. With Bridged Device Basic Information first
in the sensor descriptor, Home Assistant received `Reachable=true` before
the updated Occupancy attribute. Its automation emitted `off → on` when
recovering to person and `on → off` when recovering to clear. Listing the
Occupancy Sensing cluster first delivered the new value while the entity was
still unavailable. Each recovery then produced just the expected transition.

Prefer these sibling endpoints for the gateway implementation. The ordering
result applies to the tested SDK, controller and subscription. Qualify reads
during recovery, reconnect, split subscriptions, multiple fabrics, camera
removal/re-addition and multiple cameras before assigning durable endpoint IDs.
A production ordering guarantee needs to cover each supported controller's
subscription behavior. A fixed sleep cannot establish delivery.

Home Assistant discovers a Camera from the device type, stream feature map,
current sessions attribute and a sufficiently new server schema. Discovery
does not validate all mandatory device behavior. Its frontend supports native
WebRTC through the Matter integration; keep browser/iPhone rendering evidence
separate from the aiortc decoded-frame witness.

## Sources

- [Camera type and root conditions](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/Camera.xml)
- [Root Node](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/RootNodeDeviceType.xml), [Base Device Type](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/BaseDeviceType.xml)
- [Power Source device type](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/PowerSource.xml)
- [Bridged Node](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/BridgedNode.xml), [Occupancy Sensor](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/OccupancySensor.xml)
- [Home Assistant camera discovery and signaling](https://github.com/home-assistant/core/blob/ff5d56b825a0fb66b03d01bc297d49e206cdaba3/homeassistant/components/matter/camera.py)
- [Home Assistant endpoint availability](https://github.com/home-assistant/core/blob/ff5d56b825a0fb66b03d01bc297d49e206cdaba3/homeassistant/components/matter/entity.py)
