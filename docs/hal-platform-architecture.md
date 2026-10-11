# HAL capability architecture

**Status:** architectural source of truth for provider selection and service-owned resources.

## Core rule

Nxrs has no global HAL platform package or universal HAL object.

For physical NuttX resources, each capability's dedicated Rust provider also
owns device access. Qualified Rust `std` APIs are used directly for ordinary
descriptor I/O; NuttX-specific device controls remain private implementation
details with minimal target-ABI glue. There is no separate generic POSIX/device
adapter layer. See
[NuttX device access from Rust](nuttx-device-access.md).

Each capability owns its public facade and its provider selection:

~~~text
hal/imu/             -> nxrs-imu
  api/               -> provider-independent Imu/ImuSample contract
  mock/              -> synthetic provider
  <future provider>/ -> physical/native/web implementation

hal/camera/          -> nxrs-camera
  api/
  native/
  nuttx/
  mock/
~~~

Portable applications and services depend on capability facades such as
`nxrs-imu`, `nxrs-camera`, `nxrs-storage`, and
`nxrs-transport`. They do not depend on concrete provider crates.

Cargo package names retain the `nxrs-` prefix; Rust namespaces do not. Each HAL
or service declares its library name once in `[lib]`, so consumers use
`wifi::open()`, `ble::Ble`, `imu::Imu`, or `camera::Camera` without local aliases.
Service names retain a suffix where it distinguishes them from a capability,
such as `camera_service::CameraService` and `recording_service::RecordingService`.
Package-qualified build features such as `nxrs-wifi/nuttx` are unchanged.

Provider selection is a build/deployment decision implemented through features
on each capability facade. Production deployments should normally not enumerate
those features themselves. Instead they select one checked-in **product platform**
whose hardware description owns the complete set of provider bindings.

For example, the developer selects only:

~~~sh
cargo firmware --app vehicle --platform pico2w-product-a
~~~

The app's Cargo metadata supplies entry/resource settings while that platform
profile selects the appropriate IMU, GNSS, storage, network, board, target and
Kconfig configuration.

There is deliberately no central `hal/platform` Rust package or runtime object.
The product platform is build metadata, not a software dependency.

## Dependency direction

~~~text
application
    |
    +--> services
    |      |
    |      +--> capability facade when the service owns the resource
    |
    +--> capability facade when the application owns the resource
             |
             +--> stable capability contract
             |
             +--> one build-selected provider
                      |
                      +--> driver / OS support as required
~~~

Examples:

~~~text
ImuService
  -> nxrs-imu
       -> nxrs-imu-api
       -> nxrs-imu-mock          # selected by build

app/nxrs
  -> nxrs-camera
       -> nxrs-camera-api
       -> nxrs-camera-native     # selected by build
~~~

The application/service source sees the capability, not the provider identity.

## Why the facade is separate from api/

The `api/` crate contains only the provider-independent contract. Providers
depend on that contract, so putting provider dependencies directly into the
contract crate would create a Cargo cycle:

~~~text
camera-api -> camera-native -> camera-api
~~~

The capability root crate is therefore the thin selection facade:

~~~text
camera facade -> camera-api
              -> selected camera provider
~~~

The facade re-exports the portable contract and exposes acquisition where that is
useful. It must not become a registry, service factory, runtime manager, bus
container, or giant global HAL object.

## Build selection

Provider choice belongs below application and service policy.

The Cargo firmware frontend composes:

- one application, whose existing Cargo metadata owns binary/command/resource
  settings;
- one product platform.

The selected product platform owns:

- the provider feature required for each used HAL capability;
- execution target/OS and board configuration;
- target-specific Kconfig/build facts.

Thus a new product normally adds a new platform description rather than editing
the app or manually composing provider features at every build site.

Capability-local Cargo features remain the mechanism that excludes unselected
providers from the dependency graph. They are intentionally lower-level than
the product platform abstraction. Do not put provider-selection features in
application or service manifests.

## Build-time exclusion

Unselected providers must stay out of the active Cargo build graph. Source-level
`#[cfg]` around code is not enough if the provider remains an unconditional
Cargo dependency.

Provider crates therefore remain optional dependencies of their capability
facade. The selected feature activates exactly one provider for that capability.

The build checks must verify:

- app/service manifests do not depend directly on provider packages;
- each selected facade feature maps to one local provider;
- no capability selects conflicting providers;
- the provider declares support for the requested execution environment;
- minimal-image evidence excludes unselected Rust providers and exclusive C
  driver/registration objects.

A broad workspace test may compile several providers independently. That is not
evidence that a production image selected all of them.

## Resource ownership

The component that naturally consumes a capability normally owns it.

For a service-owned sensor:

~~~text
app
  -> ImuService::start()
       -> imu::open()
            -> selected IMU provider
~~~

For the current native camera profile, the app is the natural composition and
configuration boundary:

~~~text
app/nxrs
  -> camera::open(...)
  -> CameraService(camera)

  -> storage::open(...)
  -> RecordingService(storage)

  -> transport::open(...)
  -> TelemetryService(transport)
~~~

Both are valid. The important boundary is that neither app nor service names
`nxrs-*-native`, `nxrs-*-nuttx`, `nxrs-*-mock`, a concrete sensor,
or a bus/IRQ implementation.

## Capability configuration

Different providers may legitimately need different setup data. A native replay
camera needs a source path and replay period; a physical NuttX camera may need a
device/resource description.

Do not invent one giant cross-platform configuration enum merely to force every
provider constructor into one signature. Keep portable behavior in the
capability contract, keep provider-specific setup below the facade when possible,
and add a genuinely portable configuration type only when the semantics are
actually common.

## Relationship to platform/

`hal/` answers:

> Which implementation satisfies this capability in this build?

`platform/` answers:

> How is this firmware built, linked, configured, and booted on this target?

For example, `cargo firmware --app event-demo --platform pico2-mock` combines
the event-demo Cargo metadata with
`platform/nuttx/platforms/pico2-mock.toml`. The platform file contains the Pico 2
board/target configuration and mock IMU/GNSS bindings. That product description
is analogous to a Zephyr board/overlay selection; it is not a global runtime HAL
abstraction.

## Portability invariant

The target invariant is:

> Portable application/service source must compile and behave against compatible
> capability-provider selections without naming or branching on provider
> identity.

Platform differences may change device wiring, OS calls, resource limits,
readiness implementation, physical timing, or configuration data. They must not
force product policy to import concrete backend packages. On NuttX, raw device
paths, descriptors, request numbers and native control structures remain inside
the selected capability provider rather than leaking into portable app/service
code.

## Current migration state

### Wi-Fi and BLE bring-up

`hal/wifi` and `hal/ble` expose provider-independent, allocation-free `no_std`
contracts. `wifi::Controller` implements shared Wi-Fi operation tracking,
deadlines and lifecycle above the native provider; `wifi::open(sink)` acquires
that controller. It owns no thread, queue, scheduling or reconnect policy.
Applications can use it directly, or a connectivity service can own it alongside
other capabilities and implement network selection, retry/backoff and product
connectivity policy. No separate Wi-Fi service or empty connectivity service is
introduced. The first native provider is `hal/wifi/nuttx`, selected through
`nxrs-wifi/nuttx`. It implements scan, open/WPA2 association, bounded DHCP,
explicit disconnect and link notifications using generic NuttX interfaces.
ESP scans, shutdown, unavailable-network timeout/reuse, and successful
association/DHCP/UDP echoes have been hardware-tested. A bounded ARP-timing
profile change passed ten complete local station trials, including five
hardware resets; this is not a long-term reliability claim. The fixtures track
the remaining gates. The
initial DHCP path expires addresses but does not yet renew leases.
`ble::Controller` applies the same single-operation/deadline model to BLE.
The API covers central/client contracts, but the first NuttX provider implements
only LE scanning and explicitly rejects GATT. Hardware qualification is tracked
in the [BLE fixture](../tests/nuttx-std/ble/README.md). It does not create a BLE service.

The intended providers are CoreWLAN/CoreBluetooth on macOS, NetworkManager/BlueZ
on Linux, and capability-local NuttX providers shared by ESP32-S3 and Pico 2 W
where the underlying drivers/stacks permit. Board-specific details stay in
drivers/build profiles. The ESP Wi-Fi profile is separate from the unchanged
footprint-study configuration. macOS/Linux native radios and Pico 2 W Wi-Fi are
not yet qualified; sharing the contracts does not establish driver support.
Pico Wi-Fi wiring/transport and the experimental Bluetooth shared-SPI HCI
transport are staged as [upstream patches](upstream-patchsets.md), with pinned
radio firmware fetched only when its profile is selected. Both are unqualified
for reliable Pico operation; a successful build is not evidence of radio parity.

Common contract rules:

- Submission is nonblocking admission, not successful radio operation. Wi-Fi
  rejection returns the original owned command, including credentials; its
  copyable request ID remains with the caller.
- Events carry a provider-lifetime session identity and completions carry an
  operation identity. Neither may be reused while stale callbacks can exist.
- The initial Wi-Fi controller permits one native operation at a time. Timeout
  reports failure once but retains the slot until native completion or quiescence.
- Provider readiness, association, IP configuration and application connectivity
  are distinct. Unknown link state is not reported as disconnected.
- Stop closes admission; provider Stopped means callbacks are quiescent, not
  that a worker has been joined or that an OS-managed Wi-Fi link disconnected.
  Faults stay observable through shutdown. Joining remains explicit owner work.
- Invalid completion kinds fault the session instead of permitting ambiguous
  retries. Provider `finish()` is explicit, possibly blocking teardown after
  quiescence; failed reclamation retains ownership for retry.
- Scan/discovery items and their completion use the same FIFO traffic class;
  priority selection must not let completion overtake already admitted items.
- Important rejected events require bounded retention/retry or sticky state plus
  a wakeup. A status snapshot alone does not solve delivery or queue selection.
- SSIDs are bounded bytes; passphrases have redacted Debug and are not Clone/Copy.
  This does not promise guaranteed memory zeroization or credential persistence.
- BLE peers/attribute instances use session-local IDs, not OS handles or mandatory
  MAC addresses. UUIDs alone cannot distinguish duplicate attribute instances.
  Product payload capacity does not override negotiated peer limits.

The caller feeds the controller events and monotonic deadlines through the
single-wait model in
[Concurrency and event communication](concurrency-event-communication.md).
The scan fixture's owner in `tests/nuttx-std/wifi/src/owner.rs`, not the HAL,
creates independently bounded important/ordinary traffic and
a reserved, non-cloneable stop endpoint. A fixed-arm Crossbeam selection waits
once; after eight important items, ready ordinary traffic gets a turn. Absolute
deadlines are checked between dispatches. Scan results and **all** completions
share the ordinary FIFO, including failed completions. `run` borrows the controller
and keeps its endpoints on error for explicit recovery. An already-selected
event is retained if deadline reporting fails, so recovery
cannot lose a native completion. State reports contain coherent status/link/fault
snapshots rather than a data-free change notification. It returns at quiescence;
the composition owner joins with `finish()` outside dispatch. The observer must
also be bounded/nonblocking; a rejected report is returned, never silently lost.

The NuttX provider owns a std UDP socket, one worker, one request slot, and one
4 KiB scan buffer. Its blocking native scan read never runs on the consumer's
dispatcher. Stop wakes an idle worker and interrupts event-delivery retries,
but cannot preempt the ESP driver's native scan wait (up to five seconds in the
pinned driver). A full sink retains one event and retries; it never overwrites
results. Terminal state is sticky and wakes the owner. Native header/layout
decoding uses a small private C bridge; AP normalization stays in Rust.

The interface must already be up; acquisition does not power an adapter, switch
networks, or configure IP addresses. The pinned ESP scan output cannot identify
WPA2 reliably, so encrypted results stay `Security::Other`. Its bounded native
scan table cannot certify exhaustiveness; completion marks `truncated=true` and
counts admitted results. Provider unit tests use an injected native source,
not a desktop radio. See the [scan qualification](../tests/nuttx-std/wifi/README.md)
for target build/run steps and the remaining hardware gates.
The separate [BLE scan fixture](../tests/nuttx-std/ble/README.md) documents the
smaller scan-only provider and its hardware gates. Neither fixture proves
Wi-Fi/BLE coexistence or native desktop radio parity.
The controller generates operation IDs and returns them at admission; providers
echo those IDs unchanged. Native provider delivery, cancellation and resource
budgets still require qualification; contract tests are not radio tests.

Run the allocation-free foundation tests without radio hardware or extra
dependencies. The separate scan-owner tests use Crossbeam only in the fixture:

```sh
cargo +1.90.0 test --locked --offline -p nxrs-hal-common -p nxrs-wifi-api \
  -p nxrs-wifi -p nxrs-ble-api -p nxrs-ble
cargo +1.90.0 test --locked --offline -p nxrs-std-wifi --lib
cargo +1.90.0 test --locked --offline -p nxrs-wifi-nuttx --lib
cargo +1.90.0 test --locked --offline -p nxrs-ble-nuttx --lib
cargo +1.90.0 check --locked --offline -p nxrs-wifi -p nxrs-ble \
  --target thumbv6m-none-eabi
```

The bare-metal compile checks portability, not a Pico/ESP driver or firmware
image. ESP32-S3 scan/shutdown is now hardware-qualified on three fresh boots,
including an injected-failure exit-status control; the linked qualification
records the tested image. Next gates are association and IP configuration,
desktop provider parity, Pico 2 W driver bring-up, then BLE peers.

### Existing capabilities

- IMU/GNSS services depend on `nxrs-imu` and `nxrs-gnss`; their mock
  providers are selected by build profiles.
- app/nxrs depends on `nxrs-camera`, `nxrs-storage`, and
  `nxrs-transport`; the native providers are selected by the build.
- the former global `nxrs-hal` / `hal/platform` package has been removed.
- low-level provider qualification tests may still depend on a provider directly,
  because the provider itself is what those tests are qualifying.

The next architectural work is execution/readiness and cross-thread payload
ownership, not another provider registry.
