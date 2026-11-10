# Characterizing Zephyr, NuttX and NXRS

The comparison should answer: **what does it cost to deliver the same useful
firmware behavior, and what programming, timing and operating contracts does
each implementation provide?** RAM is one part of that answer. An empty-thread
test cannot characterize device integration, progress under load, recovery or
the value of ordinary Rust application code.

This document defines the broader evaluation scope and maps existing evidence
to it. The experiments below are the proposed next measurements, not completed
physical results. The historical [RTOS comparison](rtos-comparison.md),
[std channel campaign](std-channel-performance.md) and their frozen cohorts
retain their original scope. The new RAM controls are one component of this
characterization; they do not establish a platform ranking.

## Separate the OS from the application environment

Zephyr and NuttX provide OS facilities. NXRS builds an ordinary Rust application
environment, capability HALs and build integration on NuttX. Its scheduler is
NuttX's scheduler; Rust libraries and selected runtime patches can change the
path that an application takes through it. See the [architecture](architecture.md)
and [std implementation and measurements](std-channel-performance.md).

| Implementation | Question it answers |
| --- | --- |
| Zephyr using its native facilities | What does the required firmware contract cost on Zephyr? |
| NuttX using its native facilities | What does the same contract cost on NuttX? |
| Rust and C using the same NuttX adapter, configuration and topology | What changes when the application language/compiler changes within this narrow implementation? |
| NXRS using actual `std::thread`, the selected bounded messaging path and required HALs | What does the intended Rust application model add, remove or make easier on the same NuttX substrate? |

The third row is a diagnostic control. It cannot stand in for the fourth. The
old event-service study's 16-byte Rust/C RAM difference used the common native
pthread/mqueue adapter, not normal std threads, NXRS inboxes or Crossbeam.

Report two views:

1. **Controlled attribution:** hold hardware, required behavior, topology and
   relevant configuration equal, changing one implementation choice at a time.
   Keep NuttX revision, resolved patch set, configuration and built kernel
   archive identical when isolating an NXRS library or HAL layer.
2. **Practical deployment:** let each implementation organize the application
   sensibly while meeting the same externally observable requirements. Record
   its actual configuration, resource cost and integration work.

Matching every internal structure can conceal an architectural benefit; tuning
everything independently cannot identify its cause. Both views are useful.
If enabling a needed API changes configuration, report that change as part of
deployment cost rather than presenting it as a same-substrate control.

## Criteria that affect firmware decisions

| Criterion | Question | Measurements that make the distinction useful |
| --- | --- | --- |
| Resource cost and scaling | Does the real application fit, and what grows when a capability or owner is added? | Total physical SRAM reservation, peak live heap, stack reservation/use, executable flash contents and address span; increments for owners, queues, slots, real drivers and required library features. |
| Responsiveness and predictable progress | Does important work complete on time while other work runs? | Source/release-to-output latency, p99/p99.9, observed maximum, deadline misses, longest starvation interval, interrupt exclusion and blocking under declared interference. |
| Useful work and CPU efficiency | What load can the system sustain while preserving correctness and deadlines? | Completed useful work, active CPU/cycles per completion at equal offered load, occupancy, rejected/lost work and saturation point. Throughput alone does not measure CPU cost. |
| Bounded communication | Are queue capacity, pending work, selection and overload semantics suitable for the service? | Allocations at construction/first wait/steady state, producer-held work, copy/ownership costs, independent traffic admission, wakeups and progress at unequal priorities. |
| Lifecycle and failure behavior | Can startup, stop, failure and restart complete without accumulating resources or stale activity? | Time to ready, cancel, quiesce, join and recover; heap/descriptor/thread recovery; retained failure state; behavior during partial startup and full queues. |
| Application and dependency portability | What application logic and useful libraries survive a target change? | Unchanged source that actually executes, required API substitutions, unsupported behavior, and edits outside provider/platform bindings. Compilation alone is insufficient. |
| Hardware integration | Can devices be replaced or multiplied without rewriting service policy? | A real acquisition path on each board; adding a second device instance, replacing a device, configuring ownership and recovering acquisition errors. Record where changes occur. |
| Diagnostics and maintenance | Can a developer reproduce, diagnose and maintain the deployed system? | Clean setup/build reproduction, a concrete failure diagnosis, stack/heap visibility, required SDK patches and upgrade/requalification work. Record artifacts and work performed, not a subjective ease score. |

Power and boot readiness belong in the first campaign when the product is
battery-operated or has a startup deadline. Measure idle wake rate, residency,
energy per useful operation and time to useful readiness on the complete image;
do not infer energy from throughput or a tick interval. Hardware fault isolation
also needs its own acceptance contract when required. Compare equivalent
protection modes, including their memory and syscall costs. These are conditional
product requirements rather than a reason to expand into every RTOS feature.

## Architectural distinctions to test, not turn into scores

- **Application APIs:** NuttX is POSIX-oriented; Zephyr also provides a
  configurable POSIX subset alongside native APIs. Test the specific interfaces
  and libraries required by the application. Neither the presence of POSIX nor
  a successful std build establishes complete behavioral compatibility.
- **One wait point:** Zephyr has native `k_poll` for multiple kernel objects.
  NuttX integrates supported device readiness through file-descriptor `poll`.
  NXRS can use a shared bounded std inbox or, where independent capacities are
  needed, the selected multi-queue implementation. These have different
  readiness, ownership and selection semantics; none is automatically a
  thread-per-input design.
- **Device binding:** Zephyr uses devicetree with its device model. NuttX
  exposes registered devices through native driver interfaces and commonly
  VFS operations. NXRS adds capability contracts, Rust ownership and product
  composition. Test whether these boundaries actually localize a device change.
- **Scheduling:** NXRS does not acquire a new scheduler by using `std`.
  Relative priorities, preemption, time slicing, tick/wakeup resolution, CPU
  affinity, locking protocols and library progress rules determine behavior.
  A nonblocking API name does not prove bounded progress when its caller
  preempts a lower-priority participant.
- **Queue coordination:** the selected NuttX positive-capacity std channel
  backend protects queue/waiter updates with an interrupt-excluding guard on
  the qualified single-core path, including payload movement. Measure maximum
  observed interrupt exclusion versus payload size separately from elapsed
  send/receive latency on every candidate; improved channel timing
  alone does not establish acceptable device-interrupt latency. See the
  [backend description](std-channel-performance.md#why-patch-std-and-what-changed).
- **Protection:** Rust ownership and hardware isolation answer different
  questions. Zephyr userspace and NuttX protected/address-environment options
  are configuration and architecture dependent. Flat-build measurements do
  not characterize those modes or establish NXRS support for them.

These distinctions follow the project contracts and the official sources linked
below. Current upstream documentation describes available mechanisms; each
experiment must still pin and inspect the version it actually builds.

## Five experiments using the existing fixtures

### 1. Cost of the required deployment

Use the [native RAM controls](../tests/event-services-comparison/RAM.md),
[ordinary Rust controls](../tests/ram-footprint-std/README.md) and
[std footprint analysis](rust-std-footprint.md). Start with a deployment baseline,
then add the same useful workload. Vary owner count separately from queue count,
capacity and payload size. Record finite increments rather than assuming a
constant per-thread or per-service slope.

Account for linked data, RAM code, pools, all stacks, runtime metadata, live
heap and pending payload ownership. Count physical memory once, including
IRAM/DRAM aliases and heap arenas. Observe construction, first blocking use,
steady state, full capacity and joined teardown. Keep requested/reserved stack
bytes separate from observed use and the chosen safety margin. An observed
maximum is not a proof of worst-case stack demand.

Publish a minimal configuration that still meets the experiment's requirements
and the practical configuration with the actual required I/O. Attribute feature
costs explicitly. Removing a required filesystem, driver or recovery behavior
does not produce an equivalent smaller application.

### 2. Communication, selection and priority progress

Reuse [the maintained MPSC fixture](../tests/mpsc-comparison/README.md) for the
single-inbox path. It already runs a shared Rust workload using NuttX std
`sync_channel` and Zephyr `k_msgq` on ESP32-S3 and Pico 2 W. Comparisons remain
within one board. It does not run Rust std on Zephyr; a matched native NuttX
control is still needed to attribute kernel versus std-backend costs.

For independent stop/important/ordinary capacity, reuse the
[existing three-class Wi-Fi owner](../tests/nuttx-std/wifi/src/owner.rs) and
[communication contract](concurrency-event-communication.md). Measure ready
operations, verified blocked wakeups, full queues, timeout, disconnection and
unequal priorities. Exercise simultaneous readiness, important-traffic bursts
and ordinary progress. Selection preference cannot interrupt a running handler.

Hold admission, ordering, capacity and ownership promises equal. One 24-slot
FIFO and three independent eight-slot queues have equal slot capacity but
different isolation and admission behavior. Include messages held by blocked
senders, upstream buffers and executing handlers in the whole-system bound.
Report delivery tails, useful throughput, loss accounting and separately
measured CPU cost; primary MPSC images currently lack CPU accounting.

### 3. Device-to-output response under interference

Use one real acquisition source and a useful consumer output, with the
[IMU qualification witnesses](../tests/nuttx-std/imu-qualification/README.md)
and [service qualification](../tests/service-qualification/README.md) as
starting points. Observe source/IRQ or release time, acquisition, normalized
delivery, handler start and output. Account for source loss separately from
queue loss. Synthetic publication or GPIO output alone does not measure the
input interrupt path.

Measure a small load curve: quiet operation, ordinary load, a declared burst,
and relevant CPU/I/O interference. Include a long synchronous handler and the
actual priority relationship of important work. Report achieved source rate,
completed work and misses together; a fast run that rejected the difficult
events does not establish a better response envelope.

Inspect final flash/IRAM placement. Use a matched placement control for
attribution, then report the practical image's latency and SRAM cost together.
Measure timer/idle CPU or energy separately where relevant. Change one such
choice at a time rather than attributing all differences to the scheduler.

### 4. Bounded overload and recovery

Extend the existing lifecycle and pressure cases to the actual selected
std/multi-queue/HAL path. Fill ordinary capacity, inject a terminal source
failure, stop while acquisition waits, fail partial startup, and restart the
supported session. Track admitted, rejected, in-flight and discarded work.

Require retained error state, bounded pending storage, callback quiescence,
joined workers and resource recovery. Record time to each milestone, not just
eventual success. Explain warm-up and deferred frees when comparing snapshots;
track both pre-drain and post-drain storage. Test the real failure boundary:
a reported close error after a successful native close does not qualify a
descriptor that remains open.

Keep session/service restart distinct from re-entering a Rust built-in command.
The [flat NuttX restart caveat](rtos-benchmarking.md#flat-nuttx-built-in-restart-caveat)
documents the current one-main-per-boot performance contract and task-group/TLS
issue. Native-adapter restart results do not establish arbitrary std-command
restart support.

### 5. Portability, integration and maintenance

Use one ordinary application/service implementation on a host and the two MCU
profiles, with real capability bindings where available. Apply the same change
requests: substitute a device, add a second instance, and add one representative
library dependency required by the application. Record application changes,
provider changes, unsupported operations and runtime checks. Keep missing
capabilities visible rather than replacing them silently with simulation.

Include a reproducible clean build and a concrete diagnosis exercise, such as
a stalled provider or a stack/resource failure. Inventory target-specific std,
libc/compiler and kernel patches. A representative pinned-version update can
measure maintenance work; patch count alone cannot establish reliability or
developer productivity. Use [portable services](portable-services.md),
[NuttX std support](nuttx-std.md) and
[static device composition](static-device-composition.md) as the existing
implementation references.

## What the existing results currently establish

| Evidence | Supported finding | Remaining boundary |
| --- | --- | --- |
| [Historical event services](rtos-comparison.md) | Twenty-service native configurations provision equal payload capacity. Timer resolution, code placement and execution model affect observed results. | Native-adapter Rust is not ordinary NXRS; fixed 4 KiB requests do not establish minimal safe stacks; different cohorts cannot be added together. |
| [Modern std channel campaign](std-channel-performance.md) | The selected NuttX bounded std path has physical delivery, overload and throughput observations. | One bounded channel is not selected multi-queue service progress or complete device-to-output qualification. Each full timing/counter matrix had one pass; only a focal case had additional repeats. |
| [Native C/Rust service qualification](../tests/service-qualification/README.md) | Scoped pressure, same-boot restart, failure/recovery and resource-reclamation cases passed. | It shares native adapters, excludes production std/Crossbeam paths, and does not qualify input IRQ latency or arbitrary failure recovery. |
| [New ordinary Rust RAM controls](../tests/ram-footprint-std/README.md) | Actual std/inbox/Crossbeam paths have host accounting/lifecycle checks and target build controls. | Host behavior and target linking do not supply physical target heap, stack or timing measurements. |
| [Architecture and capability contracts](concurrency-event-communication.md) | Ownership, independent admission, failure retention and shutdown requirements are explicit. | A requirement is not an executed witness; each chosen provider/runtime still needs qualification. |

The old lean RAM figures put native-adapter NuttX Rust at 221,108 B and Zephyr C
at 205,380 B in that fixture. This is a 15,728 B configured-image difference,
not a measured price for NXRS. Embassy's separate 61,172 B row is useful as an
async execution reference, but the earlier near-threefold full-image ratio
cannot rank the three application environments covered by this document.

## Reporting discipline

Every result needs the exact source/toolchain/configuration and image identity,
board and memory domains, required capabilities, workload/arrival contract,
relative priorities, protection mode, payload/capacity/overflow policy and
observer scope. Record failure as well as successful completion. Keep matched
passes and independent fresh-boot repeats identifiable; do not pool different
images or observers into a single latency distribution.

Present a resource budget, latency/load envelope, progress/recovery outcomes and
portability/integration record. State whether each claim is an architectural
contract, source inspection, host check, target build or physical observation.
Rank deployments only against explicit product constraints, not a universal
weighted RTOS score.

## Official mechanism references

- [NuttX goals and facilities](https://nuttx.apache.org/docs/latest/introduction/about.html)
  and [filesystem/device readiness](https://nuttx.apache.org/docs/latest/os/filesystem/index.html).
- [Zephyr POSIX support](https://docs.zephyrproject.org/latest/services/portability/posix/overview/index.html)
  and [polling kernel objects](https://docs.zephyrproject.org/latest/kernel/services/polling.html).
- [Zephyr devicetree](https://docs.zephyrproject.org/latest/build/dts)
  and [NuttX driver interfaces](https://nuttx.apache.org/docs/latest/components/drivers/index.html).
- [Zephyr userspace](https://docs.zephyrproject.org/latest/kernel/usermode/overview.html)
  and [NuttX protected builds](https://nuttx.apache.org/docs/latest/guides/programs/protected_build.html).
