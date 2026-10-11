# HAL and platform architecture

Each capability is one Rust crate. Applications use `wifi`, `ble`, `led`,
`imu`, `gnss`, `camera`, `storage` and `transport`; Cargo package names
retain the `nxrs-` prefix.

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

## Source organization

~~~text
hal/wifi/
  Cargo.toml
  src/
    lib.rs                 public interface and acquisition
    api.rs                 portable traits, commands, events and errors
    controller.rs          shared operation/lifecycle logic
    worker.rs              private std execution for blocking adapters
    providers/             private OS-specific implementations
  ffi/nuttx/               minimal target-header C helpers
~~~

`api.rs` is an ordinary module, not another crate. Provider modules import
the contract from their own crate, so no contract/provider dependency cycle
exists. Providers are not public namespaces. Radio test seams and C bridge
layouts stay private; product code uses the common contract and `open()`.
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

Each radio's private std worker owns one bounded execution thread when the
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

The current `ble` contract is scanning only. Scan events have no generic GATT
value buffer. A full bounded peer registry emits `DiscoveryLimited` once per
scan without failing the provider or silently recycling session-local IDs.
Connections/GATT should be a separately qualified capability within `ble`, not
speculative commands carried by every scan queue.

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
introduced. The older camera/storage NuttX adapters retain their shared
descriptor owner under `hal/support/nuttx`; see
[NuttX device access](nuttx-device-access.md).

## Current radio qualification

The device results below precede the private-worker refactor. Host lifecycle
tests and target compile checks pass; device qualification must be repeated.

- ESP32-S3 Wi-Fi: scan/lifecycle/error controls and ten complete local station
  runs passed, including DHCP and exact UDP echoes. The bounded ARP profile
  change is not long-term reliability or a packet-level root-cause proof.
- ESP32-S3 BLE: three local scan runs and an injected-error control passed.
  Connections, GATT, pairing and coexistence are not qualified.
- Pico 2 W: Wi-Fi scans passed, but a station attempt stalled NSH.
  Association/DHCP/traffic and BLE scanning remain unqualified.
- Pico USB recovery: the opt-in 1200-baud hook was verified at idle and during
  a foreground command. It requires working USB/timer interrupts; it is not
  a hardware watchdog and should be disabled in production unless intended.
- macOS/Linux radio providers are planned, not implemented. Native file/socket
  adapters and host-injected radio tests do not establish desktop radio support.

See the [Wi-Fi fixture](../tests/nuttx-std/wifi/README.md),
[BLE fixture](../tests/nuttx-std/ble/README.md), and
[dependency patchsets](upstream-patchsets.md) for exact evidence and limitations.
Radio fixtures are functional tests, not refreshed size/speed benchmarks.

## Regression checks

~~~sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_deployment.py
python3 tools/check-hal-isolation.py --out target/hal-isolation-new
cargo test --locked -p nxrs-wifi -p nxrs-ble --lib
~~~

The isolation check records clean Cargo artifacts and actual emitted provider
cfgs, compiles portable core/WASM variants, runs independently checked native
examples, and rejects unsupported platform/target combinations.
