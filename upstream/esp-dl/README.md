# Fixed PICO numerical SDK capsule

`source.json` pins ESP-DL v3.3.14 revision, the unmodified S3 PICO_S8_V1 model,
MIT notices and every retained numerical source, before and after the port.
Run `cargo nxrs fetch detector` explicitly; Cargo build scripts never fetch it.
An existing source directory must pass the reviewed post-port inventory. Editing
`prepared.json` cannot certify edited sources. Cargo tracks the prepared tree.

The reviewed `port.patch` removes SDK dual-core task launch code (attempted use
aborts), corrects a host pool pointer-width check and removes unused iostream
initializers. It rejects random tensor generation and missing Conv kernels, which
are outside the fixed inference graph. Rust applies it and verifies every
prepared source hash.
ESP-DL 3.3.14 separates Conv loops, scalar kernels and dispatch metadata.
`kernels.json` retains the upstream S8 selection rows for scalar and ESP32-S3
execution. The Rust builder checks their ABI, ordering, precision and symbols,
then emits the three compile-time SDK tables. Run
`python3 tests/detection/check_pico_dispatch.py --sdk target/pico-sdk` to compare
them with the retained, hash-pinned upstream generator and specifications.
The current LUT interface receives the existing context-owned parameter tensor;
the model bytes, quantization and fixed graph protocol are unchanged.

Every generated call requests synchronous single-core execution. This capsule
provides numerical operations, not an RTOS compatibility layer.

The HAL-owned `tools/detector-build-support/src/graph.rs` reads the pinned EDL2 model at build time, validates its 114
operators and exact input/output protocol, and creates a fixed numerical graph.
There is no runtime model parser or loader. Its aligned lifetime arena allocates
an output before retiring inputs and retains all six result heads. Host weights
invert ESP-PPQ's S3 NHWC16 packing for scalar operators; the optimized target uses
the original packed weights. Both use the same quantized parameters.
One aligned RAM block holds unchanged numerical parameters, avoiding repeated
flash reads during convolution. Its S3 size is 343,120 bytes. The native SDK
owner destroys operators and tensor views before freeing this block and its
activation arena; an initial parameter-allocation failure frees the arena.

The owning HAL's `nxrs-detector-build-support` package compiles the selected SDK subset. C++
uses text-section literals; assembly explicitly uses separate literal sections.
With inline literals, SDK labels placed before `ENTRY` can point at data. This
was caught on the physical S3. The Rust builder checks every global SDK assembly
function's actual ELF entry bytes, including `.iram1` sections omitted by
`objdump -d`. Recompiling the original bad flag is a failing negative control.

The former Python graph and ELF-entry readers remain independent qualification
references; they are not used by product builds. The HAL owns Rust image
preparation, confidence/box decoding and bounded NMS.
See [detector API and ownership](../../hal/detector/README.md),
[PICO qualification](../../docs/camera-pico-qualification.md), and
[the model's MIT notice](../../hal/detector/models/PICO-NOTICE).
