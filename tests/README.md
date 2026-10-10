# Test and comparison guide

The [RTOS analysis](../docs/rtos-comparison.md) is the main reading path.
Use a fixture guide for its scope and setup.

| Suite | Purpose | Guide |
| --- | --- | --- |
| Event services | Current independent-service comparison across NuttX C/Rust, Zephyr C and Embassy Rust. | [Guide](event-services-comparison/README.md) |
| Service footprint | Before/after Rust/NuttX runtime entry, worker and queue comparisons. | [Guide](service-footprint/README.md) |
| Service qualification | Lean C/Rust LED-service image, RAM, delivery, restart and fault qualification. | [Guide](service-qualification/README.md) |
| Arithmetic parity | Arithmetic correctness and compiler/device qualification; supporting developer evidence. | [Guide](arithmetic-parity/README.md) |
| Historical Zephyr | Older producer/worker/reply workload; keep results separate from event services. | [Guide](zephyr-comparison/README.md) |
| Historical Embassy | Same older workload with Embassy tasks; keep results separate. | [Guide](embassy-comparison/README.md) |

Run a fixture's Python tests by replacing the suite path in this example:

```sh
python3 -m unittest discover -s tests/event-services-comparison -p 'test_*.py'
```

Optional Linux host checks may require GCC, Rust and mounted `/dev/mqueue`; see
the fixture guide. Shared `rtos_harness` ownership: `images.py` handles ELF and
ESP-IDF aliases; `zephyr.py` handles Zephyr pins and target guards; `device.py`
handles serial, restore and markers; `matrix.py` handles case order and session
restore. These are workload-neutral utilities, not another measured suite.
