# Camera HAL

One `nxrs-camera` crate contains the contract and private providers. A checked-in
profile selects one provider through `NXRS_PLATFORM`; unset builds select `none`.

| Provider | Support and behavior |
| --- | --- |
| `native` | Linux/macOS/Windows packed Gray8/RGB565 file replay. Blocking bounded-size input loading at setup, bounded memory-copy polling, and exact owner-clock retry deadlines between frames. Not a physical camera driver. |
| `nuttx` | Existing NuttX read-device/ioctl adapter. C bridge uses configured target headers. Current execution qualification uses synthetic devices on sim/ESP32-S3 QEMU, not a physical camera. |
| `mock` | Explicit std-based scripted camera, including WASM test builds. Preserves deliberate staging corruption, failures and stop retries. Not a web hardware implementation. |

The root manifest records supported providers in Cargo metadata. Native and
NuttX providers reject unsupported targets. `target_os="none"` is admitted by
the NuttX provider only for existing core-only compilation/link fixtures, not
as a claim of bare-metal operation without NuttX.

The native nxrs profile acquires the camera through
`camera::open(...)`. The selected profile owns provider choice; app/nxrs has no
direct provider dependency. Arguments, service wiring, timestamps and shutdown remain
unchanged. Replay now advertises `Camera::next_poll_at_ms()`, allowing the
app-owned execution loop to block until the next frame deadline instead of
polling every millisecond. Ordinary execution timekeeping continues to use
`std::time`.

## Dependency boundaries

`hal/common` owns only the pre-existing `DeviceError` values shared by camera,
storage and transport. It has no backend/runtime dependency. `hal/support/nuttx`
owns only the existing consumptive descriptor-close policy and bridge error
translation reused by camera, file storage and UDP. Adoption of a raw descriptor
is unsafe and requires unique ownership; close consumes the number even when
the driver reports an error. There is one implementation, not a copy per device.

`src/api.rs` contains the contract and `src/providers/` contains private
provider modules. NuttX helper C sources are in `ffi/nuttx/`.

## Target integration

Compile `ffi/nuttx/camera.c` and `../support/nuttx/ffi/nuttx_support.c` with the
configured NuttX headers, including both header directories. The former owns
format-query/open/read; shared support owns close. No timespec, file-descriptor
table, ioctl struct or native errno value crosses the scalar Rust/C boundary.
The existing sim/QEMU recipes build separate C objects and retain the close/error
fault hooks. Live-camera DMA/readiness and physical integration are unchanged
and still require their own qualification.

## Verification

Provider selection is profile-owned. For example, run the standalone replay
fixture with `NXRS_PLATFORM=native cargo run -p nxrs-camera --example native_replay`.
The shared isolation tooling is being updated for the consolidated crate layout.

These checks do not establish a camera-only production firmware image, physical
camera timing, interrupt-driven readiness for the NuttX camera, or exclusion of
every NuttX C driver. The native replay timer path is event/deadline-driven, while
physical readiness remains a separate integration step.
