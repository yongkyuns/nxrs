# Component organization cleanup

## Scope and completion criteria

The goal is a normal Cargo application composed from understandable Rust crates,
not a new capability resolver. A short selector is necessary but not sufficient:
public interfaces, shared software behavior, dependency selection, test wiring
and native build inputs must also be understandable independently.

PR #260 simplified source selection across the 15 HAL capabilities. PR #261
started with camera, then expanded to every HAL selector and the remaining
backend wildcard exports. It is not a completed whole-repository semantic or
build-system rewrite. Qualification results belong to the exact PR head.

## Organization pass

All HALs select a source module and enumerate its exported names. No capability
root uses `pub use platform::*`, and no selector uses `selected::*`.
Factories and unavailable implementations remain in their selected source files;
shared contracts and algorithms do not acquire platform-selection logic.
Existing explicit public factory signatures and qualified fixture types are
preserved. In particular, named exports do not by themselves make legacy
storage/transport factories platform-independent.

| Components | Scope of this pass |
| --- | --- |
| Camera | Explicit public factory, independent optional file replay, private unavailable type, separate host-test wiring |
| IMU, GNSS, LED | Named factory/fixture exports and separate host-injection wiring |
| Wi-Fi, BLE | Named private backend factory exports and separate host-injection wiring; controller, worker, callback and lease behavior unchanged |
| Storage, transport | Enumerated factory/fixture exports; unconditional private selector declaration; existing runtime and file/socket semantics preserved |
| Config | Named acquisition and separate host-injection wiring |
| Console, execution, TLS, boot, watchdog, detector | Named selector exports matching the existing explicit facade; runtime behavior unchanged |

The repository-wide check in `tests/host/test_hal_boundaries.py` discovers HALs
from workspace Cargo metadata rather than a camera-only or hardcoded name list.
It rejects backend wildcard exports, public platform namespaces, implementation
bodies in selectors, and provider-source injection from crate roots. Independent
negative controls exercise these rejection paths. The existing architecture job
already runs this suite; no new workflow or reduced gate is required.

Run both structural suites and actual Rust qualification:

```sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_architecture.py -v
python3 -m unittest discover -s tests/host -p test_hal_boundaries.py -v
python3 tools/check-hal-isolation.py --out target/hal-isolation-clean
```

Host-injected NuttX tests remain tests of the production ownership code against
fake native calls, not physical device qualification. Keep their assertions and
strict linting; do not suppress duplicate-module or lifecycle failures.

## Remaining work: do not label the entire repository complete

### 1. Storage and transport software APIs

Their legacy `open()` factories have different argument lists on desktop,
NuttX and mock selections. Buffered recording and UDP helpers also retain
profile admission and repeated standard-library/OS guards. Named re-exports
make these differences visible but do not resolve them.

Before changing these APIs, enumerate all current callers and distinguish the
actual operations: synchronous record-file writes versus queued directory
recording; IPv4/port connection versus general socket-address parsing; scripted
fixtures versus real I/O. Give different operations distinct, stable names and
typed parameters. Shared file/socket functionality should use ordinary modules
or explicit Cargo functionality, not pretend to be a different OS backend.
Migrate callers and preserve error, rollback, bounded queue and zero-length
packet contracts in the same change. Do not silently replace one operation
with another merely to make signatures identical.

### 2. Public contract consistency and reuse

Review the remaining selected concrete-type exports and standard-library
requirements. Keep mock/qualification APIs explicit and normal product APIs
stable. Preserve current core/WASM contract tests unless their removal is an
explicitly documented support decision. Do not impose a trait or a separate
crate for every implementation. Replace cross-package source inclusion and
repository-layout assumptions only where ordinary dependencies improve actual
reuse; qualify an application outside this checkout.

### 3. Cargo and native build ownership

Remove the build controller's handwritten detector-dependency traversal in
favor of Cargo-grounded selection. Optional, renamed and target-conditional
dependencies must behave exactly as Cargo resolves them. Make implementation
Rust dependencies conditional where they are currently unconditional, and
select native helper compilation from consumed components rather than every
binding available on the board. Preserve native board-initialization needs and
final Kconfig/ABI checks; do not replace Cargo with another dependency solver.

### 4. Incremental and reproducible builds

Separate invalidating stale success evidence from deleting reusable compilation
outputs. Reuse only matching SDK, native configuration and Cargo inputs. Keep an
explicit clean qualification path. Test dependency edits, profile switches,
feature switches and SDK patch changes against clean-build results.

### 5. Repository-wide acceptance

Application and service code must remain OS/provider-free, with composition in
ordinary Rust. Review CLI/configuration documentation and tests together with
each semantic change, not just HAL directories. Completion requires consumer
migration, negative controls, clean and incremental compilation evidence,
platform test matrices and no regression in shutdown/resource ownership. A
source inventory or passing host build is not sufficient to claim this done.
