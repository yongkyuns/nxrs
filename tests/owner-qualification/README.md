# Current-source std and selected-owner qualification

This finite qualification package closes the current-source executable gap
between the ordinary std channel witnesses and the real independent-input
owner in [`tests/nuttx-std/wifi`](../nuttx-std/wifi). It builds through the normal
`cargo nxrs` frontend. It imports the actual owner and Wi-Fi controller; it does
not copy the owner into a benchmark adapter or add an application framework.

The inputs are synthetic `Wifi` implementations and ordinary task-context
source workers. No radio is acquired or enabled. Physical radio acquisition,
SDK callback lifetime, sensor I/O, network service, timing bounds and resource
measurements retain their separate qualification obligations. In particular,
these small functional witnesses are not a cross-RTOS RAM comparison.

There are **seven cases per target, one case per fresh boot**: fourteen captures
for the ESP32-S3 and Pico 2 W pair. Every case either completes finitely with
checked accounting and joined threads, or produces a retained failed capture.
There is no accepted “expected hang.”

## Case inventory

| Case | Controlled witness | Exact successful work / cleanup |
|---|---|---|
| `std-blocked-delivery` | Current `nxrs-messaging` bounded std channel, capacity one; priority-180 receiver is observed waiting before each priority-120 producer release. Sixteen sequenced payloads include checked data. | 16 admitted / handled, 16 phase-matched native waits, 2 joins, sender disconnection observed. |
| `std-full-progress` | Priority-180 producer fills capacity one, observes `Full` with original payload, then blocks in a bounded sleep gate so the priority-120 receiver can drain. One explicit retry succeeds. | 3 attempts, 2 admitted / handled, 1 rejection, 2 joins. This qualifies this explicit yielding contract. |
| `std-disconnect` | Priority-180 receiver is observed waiting on an empty inbox before the coordinator drops the last sender. | 1 native wait, `Disconnected` observed, 1 join. |
| `owner-independent-inputs` | Actual `OwnerLoop::run` waits across its independent inputs. Separate source workers deliver an important link event, an ordinary scan result and an ordinary completion, each after a phase-matched blocked-owner observation. | 3 admitted / handled, 3 native waits, 3 joins; provider reclamation follows both source joins and the owner join. Old command/event endpoints report `Closed`. |
| `owner-full-stop` | Important and ordinary queues are both full. Additional data rejects with `Full`; the isolated stop remains admissible and duplicate stop reports `AlreadyPending`. | 4 data attempts, 2 admitted then explicitly discarded by receiver retirement, 2 rejections, 1 join; old endpoints report `Closed`. |
| `owner-fairness-deadline` | Two finite owner runs: an ordinary result is serviced after at most seven queued link events plus one accepted important command; logical time then demonstrates deadline reconciliation before the first selected link event. | 23 admitted, 21 handled, 2 discarded on stop, 2 joins. The deadline subcase deliberately releases a timed-out operation before shutdown. This is ordering, not measured deadline latency. |
| `owner-failure-recovery` | Failure notification is rejected by a full queue while the provider retains its terminal fault. Observer refusal and one injected stop refusal are reported, then the same owner/controller resumes and stops. | 2 data attempts, 1 admitted then discarded, 1 rejection, 3 failure observations, 2 stop attempts, 1 join; sticky fault retained, underlying provider `finish` called once despite an explicit second finish. |

Data accounting excludes the isolated stop token, endpoint-closure probes and
the owner’s derived reports. A handler may create several reports for one input.
`admitted = handled + discarded` and `attempted = admitted + rejected` are
checked independently of report counts. Joins are actual `std::thread::JoinHandle`
joins, counted only after successful return. The report’s quiescence basis is
`all-fixture-workers-joined`; it does not claim a measured zero count for real
SDK callbacks.

## Execution contract and limits

Native runs require flat, single-core NuttX with priority inheritance disabled.
The build wrapper supplies `--kconfig-disable PRIORITY_INHERITANCE`; the native
translation unit also rejects an incompatible effective configuration. The
selected-owner cases set **every fixture participant** to FIFO priority 180:
coordinator, source workers, owner, command/event callers and stop caller. The
std cases use the priorities stated above and a priority-220 coordinator. Every
participant records native thread ID, policy and priority before entry, at
entry, before exit and after restoring its previous schedule. The parser checks
the complete participant inventory and schedule restoration.

All endpoint calls occur in task context after scheduling setup. No ISR calls
std or Crossbeam. Gate loops have a four-second failure deadline and sleep for
one millisecond on each unsuccessful iteration, allowing eligible peers to run.
An independent native 15-second watchdog aborts a stuck invocation. Those values
bound a qualification failure path; they are not application response-time or
worst-case execution-time guarantees. Main and worker stacks are explicitly
16 KiB on NuttX, a qualification budget rather than a demonstrated minimum.

`native.c` only translates target headers for scheduling, an atomic TCB
`TSTATE_WAIT_SEM` observation and the watchdog. Rust owns all queues, threads,
gates, cases, errors, stops and joins. A native blocked witness requires both
the intended unfinished Rust phase and the live target thread’s wait state,
effective priority and FIFO policy. The selected-owner phase uses the actual
owner’s pre-selection clock call. This identifies a pending receive/select
window; it does not claim a particular semaphore address or expose an internal
queue reservation. The std full-queue case does not cover adversarial
preemption inside an unpublished reservation.

Host execution verifies the same payload, accounting, fairness, failure and
join behavior with the host’s scheduler. Host reports have `domain` set to
`host-functional`, null native schedule fields and `native_waits: 0`. Passing
host runs cannot establish fixed-priority NuttX progress. Frozen historical
P1/P2 artifacts remain separate evidence for their exact source revisions.

## Build and inspect

Use the repository’s usual Rust and native target toolchain environment. The
two qualification profiles keep radios disabled and retain a normal NSH entry:

```sh
python3 tests/owner-qualification/workflow.py build \
  --profile esp32s3-ram-footprint --out /tmp/owner-s3-build
python3 tests/owner-qualification/workflow.py build \
  --profile pico2w-std-qualification --out /tmp/owner-pico-build

python3 tests/owner-qualification/workflow.py inspect --build /tmp/owner-s3-build
```

Output directories must be new. `build` invokes the normal frontend with the
entry manifest, `owner_qualification` command, the test-only native source and
the explicit stack/configuration controls. It retains:

- The final ELF and selected firmware image, preserving `.bin` or `.uf2`.
- The normal frontend’s resolved configuration, toolchain/provenance, provider,
  ABI and patch receipts.
- Exact current source bytes and their hashes for this fixture, actual owner,
  controller, in-repo dependency/build support and the checked-in std/kernel
  patches, plus the indexed NuttX/apps revisions.
- A build identity computed before compilation and embedded in both the
  executable and firmware payload. Rebuilding produces a new build identity.

`inspect --build DIR` is a read-only admission check. It rehashes retained
sources and artifacts, verifies the selected runtime identity and emits JSON
with `build_id`, `build_sha256`, `source_manifest_sha256`, `domain`, `profile`,
`case_count`, `cases`, `image`, `elf` and `executable`. Each artifact entry gives
an absolute `path`, `relative_path`, `sha256` and `bytes`. The UF2 identity check
handles markers split across payload blocks. No import of this script is needed
by a campaign coordinator.

These receipts bind the retained build/capture bytes. They do not independently
attest the physical board or operator’s flashing action. All results retain
`physical_execution_verified: false`, `timing_qualified: false` and
`production_qualified: false`.

## Fresh-boot capture and replay

The capture command never flashes or resets a board. First flash the exact
selected image using the existing board workflow and verify its identity.
Create one session JSON for each case, using values from `inspect`:

```json
{
  "schema": "owner-qualification-session-v1",
  "measurement_session": "board-label-and-campaign-label",
  "case": "owner-independent-inputs",
  "token": "unique-run-token-001",
  "build_id": "COPY_THE_64_HEX_BUILD_ID",
  "image_sha256": "COPY_THE_SELECTED_IMAGE_SHA256",
  "fresh_boot": true,
  "flash_verified": true
}
```

The token must contain 8–64 ASCII letters, digits or hyphens, and be unique to
the invocation. The session is an explicit operator assertion, not an automated
hardware attestation. Run capture, then reset the board when it reports ready:

```sh
python3 tests/owner-qualification/workflow.py capture \
  --build /tmp/owner-s3-build --case owner-independent-inputs \
  --session /tmp/owner-session.json --port /dev/ttyUSB0 \
  --out /tmp/owner-s3-independent-inputs

python3 tests/owner-qualification/workflow.py replay \
  /tmp/owner-s3-independent-inputs
```

Capture requires `pyserial`. It observes one fresh NSH banner, sends the exact
case/token command once, retains the complete console, waits for the result and
return to the prompt, requests the terminal exit status and observes a further
one-second tail. Before command submission it tolerates USB removal and
re-enumeration by reopening the selected port within the same 90-second capture
deadline; use a stable device path. It retains any bytes already received and
still requires exactly one boot banner. A disconnect after command submission
fails the run and never retries the command. A panic or native failure anywhere in retained output,
including after the success marker, fails admission. Missing or duplicated
records, changed identities, incorrect accounting/schedules, nonzero or
ambiguous status, a second boot, a missing prompt, timeout and incomplete
captures also fail. No finite capture can exclude failures occurring after its
retained observation window.

Each physical run is self-contained: it includes the exact build/source/image
receipt, session, raw console, capture digest and parsed analysis. `replay`
rehashes them and reruns validation, including late-failure rejection; it does
not execute a board or generate measurements. Failed captures retain their
console and failure receipt and never become a pass during replay.

## Local validation

```sh
python3 tests/owner-qualification/workflow.py host --out /tmp/owner-host
python3 tests/owner-qualification/workflow.py inspect --build /tmp/owner-host
python3 tests/owner-qualification/workflow.py replay \
  /tmp/owner-host/runs/owner-independent-inputs --artifact-root /tmp/owner-host
python3 -m unittest discover -s tests/owner-qualification -p 'test_*.py'
```

The host command builds the current release source with a bound identity, runs
all seven cases in separate fresh processes and replays every retained result.
Parser controls use explicitly synthetic records to reject missing joins,
incorrect priority/phase evidence, host-to-target relabeling, wrong image or
source, late native failure, malformed sessions, duplicate output and altered
analysis. They are checks of evidence admission, not physical results.
