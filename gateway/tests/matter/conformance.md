# Camera qualification boundaries

Issue [#388](https://github.com/yongkyuns/nxrs/issues/388) remains the release
checklist. Protocol interoperability does not establish device conformance.
This inventory uses connectedhomeip `v1.5.1.0`
(`abcc720b48c5e59c0edcfe65c516f76ca9448aa3`) and Home Assistant `2026.11.2`
(`ff5d56b825a0fb66b03d01bc297d49e206cdaba3`). Do not publish the specification
XML in Nxrs; follow the upstream source links.

| Requirement | Current fixture / remaining work |
| --- | --- |
| Camera AV Stream Management | The Camera type requires video, audio and snapshots. The host fixture streams H.264 and microphone-format mono Opus from a generated A/V source. The physical camera still needs microphone capture and timestamped ingest. Snapshots are also a release dependency. |
| WebRTC | Provider server and requestor client are required. Both offer directions and trickled candidates work in the independent receiver; Home Assistant's browser offer also includes an audio section before video. Unsupported media must be rejected without changing SDP indices. |
| Root-node conditions | Camera requires power-source support, TLS certificate/client management and time synchronization with NTP client, Matter time client and timezone support. The default SDK Ethernet root is not evidence that these conditions are satisfied. |
| Base device requirements | Descriptor is mandatory. A simple endpoint with a client cluster also requires Binding. Descriptor server/client lists, parts and device types must describe implemented behavior. |
| Optional camera functions | Identify, occupancy, zones, settings and push transport are optional for the Camera type. The example's no-op PTZ/zone hooks must not be copied into advertised production capabilities. |
| Occupancy Sensor type | A separate Occupancy Sensor requires Identify and Occupancy Sensing. Its availability must expire independently from live video. |
| Persistent identity | On macOS two independent administrators commissioned the bridge. Removing one fabric stopped its video while the other kept playing. After restart, 253 real commissioning cycles led to reuse of the removed index. The new fabric started with no sessions and could open video. The retired controller could no longer read the node. Durable endpoint assignment, interrupted multi-admin mutations and semantic recovery still need qualification. |

The commissioning fixture accepts a private device-specific verifier and requires
an explicit local request to open the initial 180-second window. The pinned
development DAC, PAI and certification declaration remain qualification inputs.
Shipping requires provisioned product attestation and a separately assessed
private-key lifecycle. [Operator-initiated commissioning](README.md#operator-initiated-commissioning).

The cluster's feature choices and the device type's requirements are different
constraints. A video-only Camera AV Stream Management cluster may work with a
client while failing the standard Camera device type's mandatory feature set.
Do not call the video-only fixture a conforming product.

## Microphone delivery

The release includes a real microphone at the camera. The Freenove OV3660
profile currently has no audio binding, and Nxrs has no audio capture HAL.
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

Exercise stop, mute and immediate reopen with every capture buffer in flight.
The ESP driver calls back with `-ECANCELED` for active and pending buffers
during stop. The generic audio I²S callback passes `OK` upward on dequeue.
Mark the retiring capture before stop, discard its completions and account
for every buffer before reuse. Inject an RX failure separately and verify it
becomes an observable discontinuity or terminal fault. If the native interface
cannot distinguish the error, correct that boundary before admitting the
hardware profile. Verify mute and revocation with a known acoustic cue so a
listener cannot hear queued samples from an earlier capture. [ESP32-S3 I²S stop](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/arch/xtensa/src/common/espressif/esp_i2s.c#L3184-L3241) · [I²S audio operations and callback](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/drivers/audio/audio_i2s.c) · [Audio upper half](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/audio/audio.c#L287-L305).

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
- [Bridged Node](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/BridgedNode.xml), [Occupancy Sensor](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/device_types/OccupancySensor.xml)
- [Home Assistant camera discovery and signaling](https://github.com/home-assistant/core/blob/ff5d56b825a0fb66b03d01bc297d49e206cdaba3/homeassistant/components/matter/camera.py)
- [Home Assistant endpoint availability](https://github.com/home-assistant/core/blob/ff5d56b825a0fb66b03d01bc297d49e206cdaba3/homeassistant/components/matter/entity.py)
