# Capture a CoreS3 diagnostic trace

The `cores3-tracker-diagnostics` profile inherits the CoreS3 tracker configuration
and enables [NuttX diagnostics](../../../docs/nuttx-memory-qualification.md#diagnostic-builds).

```sh
cargo nxrs build --app motion-tracker \
  --profile cores3-tracker-diagnostics \
  --out target/cores3-trace
```

Capture starts only when requested. Save the USB console output to a text
file and run a bounded workload from the NuttX shell.

```text
motion_tracker --frames 60 &
cat /proc/meminfo
ps
cat /proc/<pid>/stack
trace mode -o +w +i +d
trace start
trace stop
trace dump
```

Use a PID from `ps` while the tracker is running. Leave a short interval
between `trace start` and `trace stop` to capture the running tracker.

This records scheduler switches, IRQ handlers, heap operations and enabled
acquisition markers. `+d` admits named application/driver scopes; omitting it
would hide the device-specific work even though the firmware contains probes.
The diagnostic build defaults to a 32 KiB buffer and stops retaining new notes
when full. A full buffer does not establish coverage of the rest of the workload.
Keep the capture short, inspect loss/coverage, and increase the diagnostic
profile's `build.diagnostics-trace-bytes` when the event budget requires it.
`trace start` clears the old buffer; `trace start -c` continues it explicitly.

Use `+o` only when you deliberately want the most recent window. Overwrite mode
can remove earlier entries and leaves the captured interval boundaries incomplete.
For allocation-focused work, `-w -i` reduces scheduler/IRQ volume. Keep `+d`
enabled when you need acquisition context. These are capture choices, not changes
to application scheduling or device configuration.

Convert the saved console text and open the JSON with **Open trace file**
in [Perfetto](https://ui.perfetto.dev/).

```sh
python3 tools/nuttx-trace-to-perfetto.py cores3-console.txt cores3-perfetto.json \
  --elf target/cores3-trace/nuttx/nuttx

cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/cores3-trace/nuttx/nuttx --trace cores3-perfetto.json --view timing
```

Use the exact unstripped final ELF from that capture. The converter resolves
recorded allocator caller and IRQ callback PCs into function names, source files
and lines, while preserving the original addresses. It automatically looks for
the target's `addr2line` on `PATH`; pass `--addr2line /path/to/xtensa-esp32s3-elf-addr2line`
when needed. Supplying an ELF is a producer assertion of identity; it is not
independent proof of which firmware ran.

IRQ details show the interrupted task separately from the handler. An interrupt
that preempts the GNSS task is not necessarily a GNSS interrupt. Shared GPIO/UART
handlers identify the controller; device-specific `imu/<id>/...` and
`gnss/<id>/...` markers identify the task-side acquisition work. The system tick
handler and an `imu/<id>/wake_timer` marker describe different parts of that path.

Heap request entries include failures and internal allocator calls, but do not
record their outcomes. They remain requests with unknown outcome. Native
allocation/free notes now preserve operation, address, native byte count, task,
and contemporaneous used bytes. Native addresses/sizes differ between allocator
paths and are not replayed as individual allocation lifetimes. Use captured
backtraces or explicit application probes when the immediate caller resolves to
a libc/Rust allocation wrapper.

The timing inspector reports native note counts and source resolution coverage.
Wake-to-dispatch intervals use observed scheduler endpoints; missing transitions,
repeated ambiguous wakes and loss gaps prevent a latency claim. Callback notes
from explicitly enabled NuttX watchdog instrumentation are supported, but that
instrumentation is not enabled by the diagnostics profile: the pinned native
`sched_note_wdog` bypasses the recorder enable filter, so `trace stop` alone would
not stop those notes.

Use a regular firmware build for throughput measurements. Tracing consumes
memory and CPU time, which can change BLE behavior.
