# HAL and platform architecture

Each capability is one Rust crate. Applications use `wifi`, `ble`, `led`,
`imu`, `gnss`, `camera`, `storage` and `transport`; Cargo package names
retain the `nxrs-` prefix.

## Required implementation boundaries

The capability API and implementation are Rust. Rust owns validation, resource
ownership, queues, synchronization, state transitions, retries, cancellation,
recovery, deadlines, and error policy. Each capability owns its errors; shared
delivery contracts do not imply a shared device-error enum.

Use ordinary qualified `std::fs`, `std::io`, `std::net`, `std::thread`, and
`std::sync` operations directly wherever available. One standard-library
implementation can serve several platforms. Do not wrap normal POSIX file,
socket, or thread operations in a second C adapter.

Private C/Objective-C is restricted to mechanical target-header ABI translation
and native SDK calls that Rust/std cannot directly express. Constructing a
device-specific ioctl argument or registering a native callback can require
such glue. Workflow sequencing, event queues, retry loops, connection recovery,
and storage rollback belong in Rust. A fixed-width scalar C API is not by itself
evidence of a thin binding.

Rust modules call one another through typed Rust functions or private traits.
Do not route Rust-to-Rust calls through C exports, opaque context pointers,
numeric status protocols or raw command/event layouts. Native error values are
translated at the SDK boundary. C-facing adapters used only by SDK witnesses
live under `tests/host/radio_shims`, enabled only in those test builds; they do
not determine the production provider interface. Actual native callbacks keep
the ABI and lifetime rules required by their SDK.

~~~text
capability-owned Rust API and shared behavior
                    |
          selected Rust platform provider
                    |
       std facilities / minimal private ABI or SDK binding
                    |
             platform implementation
~~~

Platform/provider flags are allowed at build selection and module/factory
wiring boundaries. Shared APIs, controllers, workers, and I/O algorithms must
not branch on `target_os`, `nxrs_provider`, or platform environment variables.
Platform factories supply resource budgets such as worker stack size. Providers
report capabilities; shared behavior does not infer them from the OS name.
Ordinary `cfg(test)` and language/ABI requirements inside a platform provider
remain appropriate.

Native callback and DMA ownership still require explicit lifetime proofs. A
refactor must preserve cancellation, quiescence barriers, bounded overload,
sticky failures, and release ordering. Retain tests of those guarantees when
moving behavior across an FFI boundary.

Legacy core-only fixture implementations belong under `tests/`, not in
production HALs. Radio behavior now lives in the owning Rust providers, including
NetworkManager/BlueZ schemas, station setup, discovery, bounded queues and
retirement. `hal/ffi-boundaries.toml` inventories only remaining ABI glue; no
radio migration exceptions remain. The architecture audit checks that inventory
and rejects platform selection in shared Rust modules.
Inventory checks support review; they cannot prove that C code is mechanical.

## One platform selection

The developer selects the application and a complete product platform:

~~~sh
cargo firmware --app event-demo --platform pico2-mock
~~~

The platform owns the OS, board, HAL bindings, native drivers and resource
configuration. Application/service manifests do not choose HAL providers.
There are no per-capability Cargo provider features.

For example, `platform/nuttx/platforms/pico2w-wifi.toml` binds Wi-Fi to the
NuttX implementation and configures the CYW43439 driver. The ESP32-S3 profile
uses the same Rust implementation and different native driver configuration.
Do not duplicate Rust adapters merely because the boards differ.

For desktop/test builds, select one profile for the entire Cargo invocation:

~~~sh
NXRS_PLATFORM=native cargo test --locked -p nxrs-native-integration
NXRS_PLATFORM=mock cargo test --locked -p nxrs-simulator
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo
~~~

Explicit platform builds require Python 3.11+ for the shared TOML resolver.
`NXRS_PYTHON` can select its executable. A build without `NXRS_PLATFORM`
compiles portable contracts/common code, with no implicit hardware or mock.

`NXRS_PLATFORM=macos` selects desktop Wi-Fi, BLE scanning and GATT as well as the
existing native file/socket adapters. Both reuse their capability's controller
and worker. CoreWLAN calls are synchronous; CoreBluetooth callbacks stay on a
serial SDK queue and the scan worker drains bounded reports. No additional
service or Rust runtime is introduced.
The platform is macOS-only. The ordinary `native` profile remains radio-free.
`NXRS_PLATFORM=linux` selects NetworkManager-backed Wi-Fi scanning, link
observation and temporary station activation, plus BlueZ BLE scanning. Both
Linux bridges use GIO/D-Bus on their existing Rust workers; they refuse ambiguous
adapters and do not change host radio power. The scanner uses fresh BlueZ report
signals, local UUID filtering and daemon-scoped opaque peer identities. Cached
device objects alone are not discoveries. Discovery start/stop retains ownership
through uncertain replies; callbacks retire before release. Linux GATT uses the
same public client API with a private native GIO executor, described below.
Private-bus tests cover IPC and lifecycle, not Linux RF hardware.
The GIO dependency is selected only for these native bridges, not embedded or
radio-free builds.
Selected native C/Objective-C helpers compile against the installed SDK through
the shared build helper; firmware builds do not compile or link these helpers.

## Source organization

~~~text
hal/wifi/
  Cargo.toml
  src/
    lib.rs                 public interface and acquisition
    api.rs                 portable traits, commands, events and errors
    controller.rs          shared operation/lifecycle logic
    worker.rs              private std execution for blocking adapters
    providers/
      nuttx/               NuttX Rust implementation
        ffi/               minimal target-header C helpers
      native/
        linux/             Linux Rust implementation
          ffi/             minimal GLib header access
        macos/             macOS Rust implementation
          ffi/             minimal Objective-C SDK binding
~~~

Use one Rust std implementation wherever the operating systems offer the same
operation: IP sockets belong to transport, and file/thread/synchronization/time
handling needs no per-platform copy. Radio control is a separate capability:
std TCP/UDP sockets do not provide Wi-Fi association, BLE discovery or GATT.
Provider code should contain those native protocols, with generic behavior in
shared Rust controllers/workers. The Linux providers intentionally share the
same GIO/D-Bus resource/call protocol via `driver/linux/gio.rs`; its local wiring
supplies SDK symbols, while each HAL retains its schemas and device policy.

Private FFI is owned by the provider that consumes it and lives beside that
provider's Rust implementation. There is no parallel capability-level FFI tree.
These are implementation details, not a second cross-platform HAL: CoreBluetooth,
GLib and NuttX bindings may expose different operations. Share contracts and
behavior in Rust when their consumers intentionally implement the same protocol;
do not require matching C entry points or status enums across providers. The C
headers under `tests/host/native_radio_abi` let SDK witnesses exercise the Rust
backends; they are not production SDK interfaces.

`api.rs` is an ordinary module, not another crate. Provider modules import
the contract from their own crate, so no contract/provider dependency cycle
exists. Providers are not public namespaces. Product code uses the capability
contract and `open()`; private provider contracts are typed Rust too.
Some older capabilities still export selected types for qualification fixtures.

API/controller modules forbid unsafe Rust. The capability root denies unsafe
code; only reviewed native adapter modules permit it. Unselected provider
modules are excluded by conditional compilation. Host-injected unit tests
may compile native source against fakes; that is not hardware qualification.

The build-only shared script `platform/hal-build.rs` asks
`tools/platform_config.py` to resolve the selected profile and emits one
registered private `nxrs_provider` value for its capability. This value is
derived from the platform, not a supported user selection flag. No platform
Rust dependency or runtime registry is introduced.

## Platform variants and validation

Profiles live in `platform/nuttx/platforms` (firmware) and
`platform/profiles` (host/core tests). Names are unique across both locations.
A variant can name `extends = "native"` and override selected `[hal]`
bindings; `host-demo` and `host-imu` are examples.

Bindings merge by capability; `"none"` explicitly removes an inherited
binding. Other fields/tables replace the parent's value. In particular,
Kconfig lists are not silently concatenated. Git records platform history;
named variants represent actual board/product/test differences.

The resolver rejects unknown/ambiguous platforms, inheritance cycles,
unknown implementations and target/OS mismatches. The firmware preflight also
checks an application's declared `required-capabilities`. A missing required
binding fails before dependency setup. An optional unbound capability must
not silently turn into a mock.

Cargo tracks the platform environment, inherited profile files and resolver
as build inputs. Switching platform in an existing target directory therefore
reruns selection. Normal third-party dependencies still use Cargo manifests;
conditional Rust modules do not replace Cargo dependency management.

Native C helpers follow the platform's HAL bindings automatically. Additional
application-specific C sources remain explicit application integration inputs.
Helpers compile with the configured target headers, never as host objects.
Dependency changes remain traceable patches under `upstream/`.

## Ownership and shared behavior

The consuming application or service owns the capability. A HAL does not
automatically become a service or get a thread/queue. For example,
`wifi::Controller` owns its provider and handles operation identity, deadlines
and lifecycle in the consumer's existing event loop. A connectivity service
can own Wi-Fi/BLE and add network selection/reconnect policy when needed.

`app/camera-stream` composes the camera service with a caller-polled MJPEG
server. `service/connectivity` adds Wi-Fi reconnect policy separately; neither
component adds a service thread. Hardware, storage and time are injected, and
platform configuration owns native driver resources. See the
[local streaming qualification](../tests/nuttx-std/stream/README.md) for its
resource budgets, reproduction steps and evidence limits.

Wi-Fi and BLE scanning each use one private std worker thread when the
adapter's OS calls may block. NuttX adapters supply normalized operations and
native callback/interface leases, not a second controller or service.
Native stack workers are separate. Stop admission
does not preempt a blocking ioctl; finish/join belongs in explicit teardown,
not normal event dispatch.

Wi-Fi cleanup is fallible and retains its driver on failure. Both radios retain
the first terminal fault independently of Stopped and event-queue capacity.
An explicit owner can retry failed teardown; after Drop the worker retries with
capped backoff and retains native ownership until cleanup succeeds. Persistent
native failure still prevents resource reclamation; Drop does not force it.
Unexpected BLE worker unwind conservatively retains the callback lease rather
than admitting another owner while callback quiescence is unknown.

The hardware-backed `ble::open` contract is scanning only. Scan events have no generic GATT
value buffer. A full bounded peer registry emits `DiscoveryLimited` once per
scan without failing the provider or silently recycling session-local IDs.
`ble::gatt::Client` is a separate, allocation-free shared state machine for
connect, UUID discovery, bounded read/acknowledged write and disconnect.
The caller chooses the inline value capacity and drives it from the same event
loop using `poll(now_ms)` and `next_deadline_ms()`. Drain until `Update::None`
before waiting; an ignored stale event is still progress, not an empty queue.
It adds no worker or queue.
Peer IDs carry their scan session; discovered attributes carry their connection
identity, so handles from a previous connection cannot be reused accidentally.

Timeout/cancel reports one outcome and starts recovery. Admission stays closed
until the driver confirms that old callbacks are drained **and** the link is
gone. A late completion or disconnect notification alone cannot release that
barrier. Recovery has its own deadline; missing it produces a sticky failure,
not an indefinite application wait or permission to recycle native storage.
Ordinary remote ATT errors leave an established link usable.
When an OS omits the raw ATT code, `Failure::Remote(Error)` preserves the remote
rejection without inventing a number or treating it as a broken local transport.

NuttX selects `ble::gatt::open::<N>(wake, recovery_timeout_ms)`. The native
adapter uses the existing Bluetooth LPWORK queue, not unbounded central ioctls
or another thread. Native callbacks retain results; a later work item publishes
completion after those callbacks return. Timeout/drop retains native storage
until cancellation, link teardown and callback removal are confirmed. A late
connection after LE-create cancellation is disconnected before recovery ends.

The NuttX backend requires flat builds, one LPWORK consumer and no user-work
queue redirection; unsupported configurations fail compilation. Only one native
GATT client can be active at a time. Values are
bounded to at most 64 bytes. Reads use blob continuation until a short/empty
response proves completion; oversized values fail instead of returning a prefix.
Writes larger than the negotiated ATT payload are explicitly unsupported.
UUID discovery stays inside the selected service and rejects duplicate matches.

macOS selects the same `ble::gatt::open` API and private typed Rust executor
contract. Commands and replies carry Rust enums and bounded values; owned Rust
wake functions stay alive until native retirement completes.
CoreBluetooth runs on a private serial SDK queue, not another Rust service or
runtime. Consumer submit/poll never wait for that queue; cancellation/drop use
priority control signals. Successful disconnect/recovery retires callbacks
before publishing its barrier. A full event queue cannot prevent dropped-owner
cleanup, and a contended poll re-arms the owner's wakeup. Desktop peer identities
are copied at admission; characteristic handles are bounded local indices,
never silently reused within a link. SDK tests cover these paths with fake
central/peripheral objects; the device results and remaining gaps are listed below.

Linux uses a private GIO executor for blocking D-Bus/BlueZ work; submit/poll
remain nonblocking and wake the same owner event loop. UUID lookup is scoped
to the chosen device and service, with bounded local characteristic handles.
Read values must be complete and fit the requested capacity; writes use BlueZ's
acknowledged `request` mode. Pending replies and signal callbacks remain alive
through cancellation and teardown, including a connection arriving after an
earlier disconnect. A full event queue cannot prevent dropped-owner cleanup.

Unlike discovery sessions, [BlueZ device connections](https://bluez.readthedocs.io/en/latest/device-api/)
are system-wide. The adapter refuses an observed existing connection or a
rejected competing connect, but this is not an OS-wide lock. The application
must exclusively control its target peripheral while using this client;
concurrent use by another program is not supported. It does not pair, change
trust/power or remove cached devices. BlueZ's named errors sometimes omit the
original ATT number; those are normalized remote rejections, not guessed codes.
This is a desktop-only executor/dependency, not another NuttX worker or runtime.

Stop scanning before connecting. Keep its provider alive through connect
admission: peer IDs expire when that scan provider closes, but an accepted
connect owns its copied target independently. The owner supplies a nonblocking,
coalescing-safe wake function (for example, `thread.unpark()`) and drains GATT
updates in its existing event loop. The wake function has shared Rust ownership
and is released only after native close is quiet; it must not block during
destruction.

The backend is sanitizer-tested and cross-compiled against both boards' real
NuttX headers. ESP32-S3 and Pico 2 W clients have each passed the bounded
[BLE-only device test](../tests/nuttx-std/ble/README.md#portable-gatt-client)
against the other board's native peripheral. Both directions have also passed
cancellation, complete bounded long reads, overflow rejection and subsequent
re-acquisition without reboot. Separate
[portable-client coexistence checks](../tests/nuttx-std/radio/README.md#what-a-pass-means)
cover UDP/TCP on both boards, with an unresolved discovery miss.
Older native fixture passes do not qualify this adapter. macOS BLE scanning
uses the same controller/worker with opaque OS peer identities, not fabricated
MAC addresses. Its SDK bridge is sanitizer-tested with fake central/peripheral
objects, including UUID filtering, bounded reports and stale-callback isolation
across restart. A real Mac scan also discovered peers and stopped/joined cleanly;
that qualifies scanning only. macOS GATT has separate device checks below;
Linux has separate private-bus scan/GATT tests, not RF qualification.

Portable event contracts use bounded payloads and explicit request/session
identities. Rejected commands are returned to their owner. Scan results and
completion must preserve FIFO ordering; terminal state remains observable if
delivery is temporarily full. Discovery is bounded/best-effort, not a claim
that every radio report is delivered.

Ordinary files, sockets, threads and time use qualified Rust std facilities.
UDP uses one shared std implementation across the native and NuttX platforms;
only the existing constructors and empty-packet contracts differ. Device-control
adapters retain target-header bindings rather than guessed numeric constants.
Storage shares its RCAMREC1 record encoding while keeping each provider's
validation, writing and commitment behavior.
NuttX-specific controls stay behind small device-specific FFI boundaries; no
generic POSIX wrapper, global HAL object or platform-wide service factory is
introduced. The older core-only camera/storage adapters and their descriptor owner live
under `tests/nuttx/support`; production NuttX storage uses Rust std files. See
[NuttX device access](nuttx-device-access.md).

The radio refactor preserves the SDK/private-bus and NuttX ABI witnesses. Rust
owns stable callback storage until native delegate detachment or subscription
destruction completes. SDK execution contexts remain explicit: LPWORK on NuttX,
serial CoreBluetooth dispatch on macOS, and GIO contexts on Linux. CoreBluetooth
references remain opaque and are pinned by native containers; Rust decides
which resources to acquire, retire and release. Linux GATT uses a Rust thread
and a process-lifetime GIO wake context, avoiding a C thread/eventfd layer.

## Current radio qualification

The hardware results below describe the previously qualified images. This
behavior-preserving source refactor requires refreshed physical radio checks
before claiming hardware qualification for newly built images.


Qualification is capability- and image-specific; host tests are not radio
proof. The linked fixtures retain the measured workload and limitations.

- ESP32-S3 and Pico 2 W have completed Wi-Fi scans, station association/DHCP
  and exact UDP round trips, including negative controls followed by reconnect.
  The bounded ARP wait is not a packet-level root-cause proof or a reliability
  guarantee. The combined-radio results below remain bounded functional checks.
- BLE scanning and the bounded portable GATT client have device results on
  both boards. The combined-radio images passed the versioned V1/V2 sequence
  in both directions: eight cases and 600 exact write/read pairs, including
  cancellation/value/error checks, without reboot or role-change reflashing.
  The current images then passed all four portable-client Wi-Fi coexistence
  cases (UDP/TCP on both boards) on the same boot. An earlier warm case missed
  discovery before connection or traffic; the new pass does not resolve its
  cause. Both original full flashes were restored and verified. Pairing and
  notifications are outside the implemented client contract.
- Pico USB recovery: the opt-in 1200-baud hook was verified at idle and during
  a foreground command. It requires working USB/timer interrupts; it is not
  a hardware watchdog and should be disabled in production unless intended.
- macOS Wi-Fi's native bridge passed three read-only hardware scans after the
  test application received Location permission. Scans returned 10, 12 and 12
  networks, each including the current access point. Every context was released
  with the host association unchanged. The earlier CLI attempt was refused by
  permissions; the successful checks used a foreground test application, not
  the CLI scan-owner fixture. Real association remains unqualified.
  Joins remain disabled by default: [CoreWLAN stores joined-network passwords,
  with no macOS join-once equivalent](https://developer.apple.com/forums/thread/805392).
  `wifi::open_with_storage(..., CredentialStorage::SystemManaged)` explicitly
  permits this storage; saved credentials may survive failed joins, disconnect
  and stop. It requires exclusive control of the interface, not a race-free OS
  lease. Linux/NuttX retain volatile credentials under either policy. SDK-backed
  fake tests are not macOS association/keychain hardware qualification.
- macOS BLE scanning passed a real eight-second scan and quiescent worker join.
  Mac-to-ESP GATT cancellation, boundary/error values and a normal 100-pair
  exchange passed local checks; these are not long-term reliability results.
  A separate five-second scan missed the peer. The versioned Mac→Pico sequence
  passed V1 compatibility and V2 cancellation, boundary/error values and normal
  transfer: 300 exact pairs, confirmed teardown and original-flash restoration.
  V1 preserves its original three-handle service; V2 owns the extended schema.
  Earlier expanded-V1 discovery failures and unsuccessful device-only forget
  attempts remain recorded. The V2 pass is not proof of old-cache recovery;
  no global host reset is performed.
- Linux Wi-Fi scan/link observation and open/WPA2 station activation are
  tested with a private D-Bus service, not RF hardware. Activation uses only
  [volatile, client-bound NetworkManager profiles](https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html),
  disables autoconnect and refuses an observed existing association. There is
  no fallback to a persistent profile on older daemons. Shutdown deactivates
  only the owned activation and verifies it is inactive; unfinished replies or
  unproven cleanup retain the context and interface lease instead of reporting
  `Stopped`. An ambiguous IPC failure fails closed and can require restarting
  the application; bus closure is not treated as proof of rollback.
  Linux BLE scan/GATT have private-bus tests, including the portable client's
  legacy/cancel/value/full scenarios; no Linux radio hardware is qualified.
  Native file/socket adapters
  and host-injected radio tests do not establish other desktop radio support.

See the [Wi-Fi fixture](../tests/nuttx-std/wifi/README.md),
[BLE fixture](../tests/nuttx-std/ble/README.md), and
[dependency patchsets](upstream-patchsets.md) for exact evidence and limitations.
Radio fixtures are functional tests, not refreshed size/speed benchmarks.

## Regression checks

~~~sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_deployment.py
python3 -m unittest discover -s tests/host -p test_hal_boundaries.py
python3 tools/check-hal-isolation.py --out target/hal-isolation-new
cargo test --locked -p nxrs-wifi -p nxrs-ble --lib --tests
~~~

The isolation check records clean Cargo artifacts and actual emitted provider
cfgs, compiles portable core/WASM variants, runs independently checked native
examples, and rejects unsupported platform/target combinations.
