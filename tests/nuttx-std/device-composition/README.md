# Target-linked device composition matrix

These qualification-only consumers exercise the public named-device catalogue
and HAL acquisition APIs without a navigation-service dependency. Separate
packages keep the normal Cargo capability graphs independent. They use the
workspace release profile, including fat LTO, and the normal `cargo nxrs build`
preparation flow. No production application or acquisition policy is changed.

The overlays preserve the CoreS3 profile's `imu` and `navigation` records, then
add `secondary`. Both IMUs share the BSP-supported I2C0 resource at 400 kHz. The
second overlay changes only the secondary sensor's compatible and address.
This topology is a build qualification fixture, not a claim about stock module
wiring or a physically tested pair of sensors.

| Case | Devices declared in the resolved profile | Consumer | Retained canonical devices | NXRS native helper packages |
| --- | --- | --- | --- | --- |
| `two-bmi270` | BMI270 at `0x68`, BMI270 at `0x69`, GNSS receiver | IMU-only package | Two IMUs; no GNSS | acquisition, GPIO, I2C |
| `mixed-imu` | BMI270 at `0x68`, LSM6DSO at `0x6a`, GNSS receiver | Same IMU-only package | Two IMUs; no GNSS | acquisition, GPIO, I2C |
| `gnss-only` | Same mixed profile, including both IMUs | GNSS-only package | One GNSS; no IMU | acquisition, GNSS |

The first two cases exclude the GNSS HAL, receiver driver and its NXRS native
adapter despite the receiver's declaration. The last case excludes the IMU HAL,
both sensor drivers, and the NXRS GPIO/I2C/SPI adapters despite both IMU
declarations. Native-helper exclusion is scoped to NXRS-owned archives and
symbols. The inherited CoreS3 NuttX configuration can still retain OS facilities
such as UART, GPIO and I2C support; this matrix does not assert their absence.

## Build and inspect

Use the target toolchain and NuttX inputs described in
[the build-system guide](../../../docs/build-system.md). From the repository
root, build each image through the common frontend into a separate output
directory:

```sh
cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/device-composition/imu/Cargo.toml \
  --platform cores3 \
  --overlay tests/nuttx-std/device-composition/overlays/two-bmi270.toml \
  --out target/firmware/device-composition/two-bmi270

cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/device-composition/imu/Cargo.toml \
  --platform cores3 \
  --overlay tests/nuttx-std/device-composition/overlays/two-bmi270.toml \
  --overlay tests/nuttx-std/device-composition/overlays/mixed-imu.toml \
  --out target/firmware/device-composition/mixed-imu

cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/device-composition/gnss/Cargo.toml \
  --platform cores3 \
  --overlay tests/nuttx-std/device-composition/overlays/two-bmi270.toml \
  --overlay tests/nuttx-std/device-composition/overlays/mixed-imu.toml \
  --out target/firmware/device-composition/gnss-only

python3 tools/check-device-closure.py \
  --same-model-out target/firmware/device-composition/two-bmi270 \
  --mixed-out target/firmware/device-composition/mixed-imu \
  --gnss-only-out target/firmware/device-composition/gnss-only \
  --revision "$(git rev-parse HEAD)" \
  --report target/firmware/device-composition/device-matrix.json
```

The firmware commands are `device_composition_imu` and
`device_composition_gnss`. The matrix cross-links them; it does not execute them
on hardware. Separate output directories keep each case's Cargo graph,
preparation receipt, generated catalogue, native archives and linked ELF
available for inspection. Sequential builds in distinct directories do not
establish concurrent-build isolation.

## Identity and retention scope

`primary_imu()`, `imu()` and the `imu_primary_alias` profile alias all select the
same `PRIMARY_DEVICE`; `secondary()` selects the distinct `DEVICE_1`. The
`imu_secondary` alias repeats that same secondary accessor mapping. The GNSS
device name `navigation`, default `primary_gnss()` and `gnss_primary_alias`
select its single `PRIMARY_DEVICE`. The checker verifies the generated mappings
and counts canonical objects in both the Rust executable and final firmware.
Aliases must not add static objects. Accessor function symbols themselves may
be inlined away by LTO.

The IMU consumer holds both named references, compares their identities, and
calls each device's `open_default` while retaining the first acquisition result.
It requests cancellation for both successful sessions before joining either and
continues cleanup if one operation fails. The GNSS consumer similarly calls
`open`, `request_stop` and `join`. These reachable calls retain real acquisition,
driver and cleanup paths; a descriptor-only reference would provide weaker link
evidence. `black_box` keeps the qualification identity observations visible to
the optimizer without introducing production instrumentation.

Target-only assertions compare generated alias pointers with the canonical
references. The cross-linked images establish that these assertions and their
public APIs compile and retain the expected objects. They do not establish that
the assertions ran, that two physical sensors acquired concurrently, or that
physical FIFO, timing, power and cancellation behavior passed qualification.
Existing host device-lifecycle tests remain the runtime identity and ownership
witnesses. This matrix reports target storage and dependency closure separately
from those witnesses and from physical hardware qualification.

## Portable harness check

The fixed host mock catalogue supplies primary/secondary IMUs and one GNSS
source. Each consumer can also run through those existing mocks:

```sh
NXRS_PLATFORM=mock cargo +1.90.0 run --locked --release \
  -p nxrs-device-composition-imu
NXRS_PLATFORM=mock cargo +1.90.0 run --locked --release \
  -p nxrs-device-composition-gnss
```

These checks exercise the fixture's public API calls and session cleanup. The
host mock catalogue has no profile-generated aliases, so target alias
assertions are omitted on the host. Host success does not replace the three
target builds or the final ELF checks.
