# View a CoreS3 kernel trace in Perfetto

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
trace mode +o +w -i
trace start
trace stop
trace dump
```

Use a PID from `ps` while the tracker is running. Leave a short interval
between `trace start` and `trace stop` to capture the running tracker.

This records scheduler switches and heap events. Use `trace mode +o +w +i` to
include interrupts. The trace retains the newest events when the 8 KiB buffer
fills. Keep the capture window short when tracing interrupts.

Convert the saved console text and open the JSON with **Open trace file**
in [Perfetto](https://ui.perfetto.dev/).

```sh
python3 tools/nuttx-trace-to-perfetto.py cores3-console.txt cores3-perfetto.json
```

Use a regular firmware build for throughput measurements. Tracing consumes
memory and CPU time, which can change BLE behavior.
