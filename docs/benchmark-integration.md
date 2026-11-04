# Benchmark-stack integration

Issue [#263](https://github.com/yongkyuns/nxrs/issues/263) integrates the completed
measurement infrastructure without changing the selected production transport,
thread priorities, parker, or owner dispatch policy. Integration and review are
separate from the remaining physical and performance acceptance gates.

## Selected source revisions

| Input | Selected revision | Disposition |
| --- | --- | --- |
| Main | `9b62522c3d65d77f3fa0008933d2dc039415e66a` | Preserve Rust build frontend and current HAL source selection |
| P0, including #252/#257/#259 | `4aeb64ceef1e2ea51ebe0fd7cb42a7ef42c12ec1` | Retain corrected measurement boundaries, paired boot helper, fixed-delay controls and original results |
| P1 #255 | `74adb6e4533d89d57722bf69542109882a276a6a` | Retain witnesses, named contracts, complete replay and the frozen physical handoff |
| P2 #256 | `e1841042ff64cc6935b63999a5e842a1044fe26a` | Retain ladder, comparisons, narrow scanner controls and sanitized physical summary |

These are source selections, not assertions that the integration executable has
run on a board. The separate #264/#265 experiments and their branches are not
part of this integration. The original feature branches remain unchanged.

The three benchmark packages join main's workspace without changing registry
package versions. Main's application, service, messaging, HAL, and Rust build
frontend implementations remain unchanged. The SysTick and SIMPLE_BOOT patches
are registered once in `upstream/build-patches.json`, which the existing frontend
consumes; the old Python build orchestrator is not restored.

## New-build provenance and historical replay

The original inventories preceded the Rust build frontend. New receipts also
include its Rust/TOML sources, Cargo configuration, toolchain selection, shared
patch manifest and detector build helper through
`tests/std-channel-perf/build_inputs.py`. This is a source selector, not another
build system. It excludes generated build output and rejects missing or linked
required inputs. The three benchmark workflows react to these inputs.

P1's Git-derived source selector retains the historical policies for recorded
trees without this helper: ARM v1 before the paired boot adapter and v2 after it.
New frontend-aware ARM inputs use v3. The ESP selector likewise distinguishes
its original v1 from frontend-aware v2. The frozen physical manifest still
selects its original 163-input firmware cohort; its images and identities are
not regenerated or relabelled by this integration.

Use each historical cohort's original pinned verifier when reproducing its full
stored reports. In particular, paired-image validators bind common helper bytes,
including the patch driver; main's new driver is not the historical one. Current
verifier identity and policy must never be substituted into an old receipt to
make it pass. New builds use the integrated source closure and need their own
host/target evidence. Source selection compatibility alone is not a claim that
all archived firmware was replayed by the integration's current validators.

Profile resolution is lazy: importing the P0 build helpers for offline receipt
inspection does not compile the frontend or contact a package registry.

## Evidence and privacy

The selected P0/P1 result files remain byte-for-byte unchanged. P2 retains only
its current `README.md`, `summary.json` and `provenance.json` public result files;
withdrawn device identifiers, raw sessions and console records are not restored
from earlier commits. Original privately retained evidence keeps its original
identities. A sanitized public summary is not a standalone replay archive.

The original failed progress contracts and false qualification flags are
preserved. The selected P2 scanner configuration retains exact field/value/path
exceptions for reviewed public source hashes, with refusal controls; it does not
exclude a whole results directory or admit arbitrary hash-shaped strings.

## Integration acceptance and follow-up ownership

Before integration is accepted, run the applicable host/Rust/architecture,
security, ARM/QEMU and ESP build workflows on the proposed integrated tree and
obtain independent review. The initial local checks do not replace those jobs.
Do not use a source-snapshot workflow pass as benchmark qualification. The
one-time input-snapshot workflow is removed from the integrated tree.

| Boundary | Owner and endpoint |
| --- | --- |
| Integration | #263: resolve shared changes, current CI and independent review; no automatic merge |
| P1 physical closure | #255: execute the frozen eight-image, nineteen-fresh-boot campaign under exclusive board ownership |
| Blocked event delivery | #264: attribute a material cost and accept or reject one measured implementation change |
| Actual selected owner | #265: qualify the existing owner, work accounting and dispatch under a supported scheduling contract |
| Absolute time | #266: independent paired reference evidence, not a configured-frequency conversion |

P0's completed engineering/control campaign and P2's initial transport screening
are not reopened. P1's prewarmed std-primitive result does not qualify std MPSC;
its equal-FIFO Crossbeam contract does not approve P2's unequal-priority timing
schedule. Passing diagnostic tests that reproduce a stall is not production
progress acceptance. No speedup, calibrated absolute time, WCET, or physical
execution of this integration is claimed.
