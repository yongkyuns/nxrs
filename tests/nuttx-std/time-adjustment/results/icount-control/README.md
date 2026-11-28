# Unchanged firmware icount controls

These controls boot the exact phase1 corrected ELF images. Only the QEMU command construction is overridden in the task-local Python drivers. The APB reference, native fixture, Rust fixture, installed oracle, and ELF bytes are unchanged and hash checked before and after capture.

## Exact native source scope

Both unchanged images already include the repository's existing `0149-armv8m-systick-coherent-status.patch`. The ordinary and icount results must therefore be interpreted against that patched implementation, not the pristine NuttX submodule source. The exact shared patch ledger, patch file, and configured build's `arm_systick.c` postimage are copied without modification under `source/` and bound by SHA256 in `summary.json` and the evidence manifest.

The ledger hash is `65f762d70750a834b6a063536b13ad240f1c970c7726ca2f4b31f467be300958`; the compiled source postimage hash is `7196931b9477c8bcbafd223b49d3f9a7251fd35e8f5f824e0183121882fe6ca1`.

## Observations

| Existing image | Added QEMU option | Largest absolute monotonic/APB rate error over phases | Negative-phase backward reads | Replaced-phase backward reads |
| --- | --- | --- | --- | --- |
| Corrected 1 ms | `-icount shift=5,sleep=off` | 1.15 ppm | 0 | 0 |
| Corrected 100 us | `-icount shift=5,sleep=off` | 28.73 ppm | 4, minimum -300 us | 4, minimum -300 us |
| Same corrected 100 us | `-icount shift=3,sleep=off` | 3.85 ppm | 4, minimum -400 us | 4, minimum -400 us |

The ordinary phase1 corrected captures have phase monotonic/APB ratios of 0.9460–0.999996 for the 1 ms image and 0.9014–0.9788 for the 100 us image. Those large rate discrepancies disappear in these instruction-driven controls. That supports an emulator timing/delivery contribution; it is not a measurement of physical oscillator drift and does not alone identify a specific lost/coalesced interrupt path.

Negative realtime discontinuities remain at 100 us even with instruction-driven virtual time and accurate average monotonic/APB rates. The additional shift=3 run resolves a sampling limitation of the first fine control: shift=5 reads advanced by about 200 us, while shift=3 obtains about 20 us sampling and therefore resolves sub-tick behavior. All other dense phases had zero observed realtime regressions in these three controls. This absence is not proof of the cause or repair of the ordinary one-tick unadjusted regressions; separate source-level timer/read analysis is still needed.

The unchanged oracle still reports unqualified backends. Public adjustment declaration, pending-query behavior, and complete old-delta behavior fail in every control; the fine controls also fail realtime continuity and monotonic stability across clock operations.

## Modeled time and sources

The installed QEMU 10.0.13 help and its packaged `system/invocation.html` manual define `shift=N` as 2^N virtual nanoseconds per guest instruction. Thus shift=5 uses 32 ns and shift=3 uses 8 ns. `sleep=off` advances idle virtual time directly to the next timer deadline. These are explicit model parameters, not CPU frequency measurements or cycle-accurate emulation. APB and SysTick remain emulated clocks and do not establish physical RTC/deep-sleep behavior.

QEMU's instruction-counting documentation explains that timer deadlines constrain execution budgets and translation blocks; MMIO handling also updates the instruction count. Therefore the control changes scheduling and read cadence as well as removing normal host-clock coupling. It must not be described as a production fix.

Primary documentation:

- https://www.qemu.org/docs/master/system/invocation.html
- https://www.qemu.org/docs/master/devel/tcg-icount.html

The exact installed manual path and SHA256, QEMU executable/version/hash, actual command, image hash, original build identity, source hashes, oracle hash, and host duration are recorded in each control's `analysis.json` and `captures/results.json`. Every raw observation is retained in `captures/console-0-software.log`. `summary.json` records the direct comparisons without normalizing any clock values.

## Reproduction

From the repository root, source `target/time-sync-tools/environment.sh`, then run the relevant driver against a fresh output directory. The scripts intentionally refuse existing output directories. `run.py software-corrected` and `run.py software-fine100us` select shift=5; `run-shift3.py software-fine100us` selects shift=3. Each invokes the unchanged `clock_probe` once on a fresh boot through the existing NSH harness.

No tracked files, production code, SDK inputs, images, or prior evidence were modified by these experiments, and no Rust build was started.
