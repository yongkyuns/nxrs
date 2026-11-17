# Camera qualification boundaries

Issue [#388](https://github.com/yongkyuns/nxrs/issues/388) remains the release
checklist. Protocol interoperability does not establish device conformance.
This inventory uses connectedhomeip `v1.5.1.0`
(`abcc720b48c5e59c0edcfe65c516f76ca9448aa3`) and Home Assistant `2026.11.2`
(`ff5d56b825a0fb66b03d01bc297d49e206cdaba3`). Do not publish the specification
XML in Nxrs; follow the upstream source links.

| Requirement | Current fixture / remaining work |
| --- | --- |
| Camera AV Stream Management | The Camera type requires video, audio and snapshots. The fixture supplies only H.264 video. The existing Nxrs camera profile and ingest do not provide audio. A real audio source and snapshot implementation are release dependencies; silent or fictitious audio is not evidence. |
| WebRTC | Provider server and requestor client are required. Both offer directions and trickled candidates work in the independent receiver; Home Assistant's browser offer also includes an audio section before video. Unsupported media must be rejected without changing SDP indices. |
| Root-node conditions | Camera requires power-source support, TLS certificate/client management and time synchronization with NTP client, Matter time client and timezone support. The default SDK Ethernet root is not evidence that these conditions are satisfied. |
| Base device requirements | Descriptor is mandatory. A simple endpoint with a client cluster also requires Binding. Descriptor server/client lists, parts and device types must describe implemented behavior. |
| Optional camera functions | Identify, occupancy, zones, settings and push transport are optional for the Camera type. The example's no-op PTZ/zone hooks must not be copied into advertised production capabilities. |
| Occupancy Sensor type | A separate Occupancy Sensor requires Identify and Occupancy Sensing. Its availability must expire independently from live video. |
| Persistent identity | The blob store is qualified for process recovery. Stable endpoint assignment waits for the topology decision; multi-admin mutation and semantic SDK-state recovery remain open. |

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
2. Have the BSP register the input endpoint. Give a Rust audio capture HAL
   ownership of the input, bounded buffers, capture timestamps, overrun
   reporting and joined shutdown. Use std where the operations fit and keep
   target-header audio buffer/ioctl translation beside its NuttX provider.
3. Negotiate a versioned audio-capable stream with the gateway. Older ingest
   requires JPEG after optional detection metadata. Preserve this wire format
   for existing clients. Carry a source generation, sample sequence, format,
   capture time and discontinuities with bounded PCM chunks. Share capture
   and an audio encoder across viewers. Start by measuring mono PCM to the
   gateway and Opus there. At 16 kHz and 16 bits this costs 256 kbit/s before
   framing. Measure link budget and video FPS alongside detection.
4. Publish synchronized H.264 and Opus tracks and derive the Matter audio
   capability from the acquired format and negotiated encoding. Exercise
   allocation, deallocation, microphone mute, gain where supported, privacy
   and active-session revocation. The pinned camera handler exposes preallocated
   audio, but its audio allocation commands require implementation. Treat
   speaker/talkback as a separate capability with its own hardware.
5. Use a known acoustic pattern at the physical microphone and a visible timing
   cue. Compare decoded samples and rendered video in Home Assistant and its
   iPhone app, measure A/V skew and drift, and verify mute, disconnect,
   reconfiguration and recovery. Keep private household audio out of evidence.

[Pinned BSP bring-up](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/boards/xtensa/esp32s3/esp32s3-devkit/src/esp32s3_bringup.c)
and [example audio configuration](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/boards/xtensa/esp32s3/esp32s3-devkit/configs/audio/defconfig).

## Topology decision still open

Evaluate a gateway aggregator with a camera endpoint and a separately reachable
occupancy endpoint per source. A bridged endpoint needs Bridged Device Basic
Information, stable identity and correct Descriptor composition. Home Assistant
checks an endpoint's `Reachable` and its composed parent's `Reachable`; stale
detection must not make the camera's video endpoint unreachable. Verify this
with real attribute reports and automation states before allocating durable
endpoint identities. Also qualify multiple cameras, source removal/re-addition
and the exact bridging rules. Use the standard per-camera-node alternative only
after comparing those constraints; neither topology removes camera requirements.

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
