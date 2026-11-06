# Native bounded-channel host controls

Run against a private SDK produced by the normal frontend:

```sh
python3 tests/nuttx-std/channels/run.py --prepared-sdk PREPARED --out NEW_OUTPUT
```

The runner verifies the exact queue patch, overrides its selector only in a
private copy, rebuilds std and checks Cargo's actual selected source. Nine
controls cover capacity/errors, producer FIFO, MPMC ownership, disconnect/drop,
timeouts, reentrant destruction, unrelated park permits, zero/unbounded channels
and notification after the completed owner has joined. The host supplies mutual
exclusion; target interrupt masking and native NuttX parking require separate
device qualification. No installed SDK is changed.

## Hardware comparison fixture

The [entry-selection patch](mpsc-fixture-entry.patch) applies to the frozen MPSC
fixture at `a8a1b1baa05aeca76264e882ed31c3b2baa9973d`. Its executable entry selects
std on NuttX and the native queue on Zephyr. The timed engine, arrival schedules,
metrics and native adapters remain shared. Retain the older mixed NuttX image
for its separate native diagnostic; carrying both transports in the measured
image inflates its memory cost. See the
[comparison report](../../../docs/std-channel-performance.md).

The [phase-ordering patch](mpsc-fixture-phase.patch) moves endpoint priority and
policy queries after the existing retirement barrier, once timing and CPU
counters have closed. Priority validation and joined cleanup remain required.
Earlier captures include these diagnostics before the final message is received.
The corrected ordering passed both physical matrices and three fresh boots of
the four-producer paced case per runtime; see the comparison report.

Apply the same [fixture IRAM fragment](mpsc-fixture-iram.ld) to both kernels,
alongside their native queue/wait/scheduler IRAM policy. It covers workers,
transport adapters, clock/gate wrappers and scheduled-release callbacks. Verify
the linked symbols and their successful bounded-operation callees, including
literal-loaded Xtensa calls; a section selector alone is not proof. Native
callbacks must resolve to the same qualified hot path. Setup, reporting,
retirement diagnostics, reclamation and unselected channel flavors retain normal
placement. Match logical operations rather than compiled byte counts; record
each image's total IRAM and shared-SRAM cost separately.
