# View a CoreS3 kernel trace in Perfetto

Build Motion Tracker with the opt-in trace overlay. Use a distinct build
directory so the diagnostic and ordinary images are easy to distinguish.

```sh
cargo nxrs build --app motion-tracker \
  --profile m5stack-cores3-stamp-catm \
  --overlay tests/profiles/cores3-trace.conf \
  --out target/cores3-trace
```

The overlay enables the native NuttX trace command and reserves a 4 KiB RAM
buffer. Capture starts only when requested. Save the USB console output to
a text file and run a bounded workload from the NuttX shell.

```text
trace mode +o +w -i
trace start
motion_tracker --frames 25
trace stop
trace dump
```

This records scheduler switches. Use `trace mode +o +w +i` to include interrupts.
The trace retains the newest notes once the buffer
fills. Keep the capture window short when tracing interrupts.

Convert the saved console text and open the JSON with **Open trace file**
in [Perfetto](https://ui.perfetto.dev/).

```sh
python3 tools/nuttx-trace-to-perfetto.py cores3-console.txt cores3-perfetto.json
```

Use a regular firmware build for throughput measurements. Tracing consumes
memory and CPU time, which can change BLE behavior.
