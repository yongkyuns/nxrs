# Fixed PICO numerical SDK capsule

`source.json` pins ESP-DL v3.3.0 revision, the unmodified S3 PICO_S8_V1 model,
MIT notices and every retained numerical source, before and after the port.
Run `python3 upstream/esp-dl/prepare.py` explicitly; Cargo never fetches it.
An existing source directory must pass the reviewed post-port inventory. Editing
`prepared.json` cannot certify edited sources. Cargo tracks the prepared tree.

`prepare.py` removes SDK dual-core task launch code (attempted use aborts),
corrects a host pool pointer-width check and removes unused iostream initializers.
Every generated call requests synchronous single-core execution. This capsule
provides numerical operations, not an RTOS compatibility layer.

`graph.py` reads only the pinned EDL2 model at build time, validates its 114
operators and exact input/output protocol, and creates a fixed numerical graph.
There is no runtime model parser or loader. Its aligned lifetime arena allocates
an output before retiring inputs and retains all six result heads. Host weights
invert ESP-PPQ's S3 NHWC16 packing for scalar operators; the optimized target uses
the original packed weights. Both use the same quantized parameters.

The owning HAL's private `ffi/build.py` compiles the selected SDK subset. C++
uses text-section literals; assembly explicitly uses separate literal sections.
With inline literals, SDK labels placed before `ENTRY` can point at data. This
was caught on the physical S3. `qualify.py` now checks every global SDK assembly
function's actual ELF entry bytes, including `.iram1` sections omitted by
`objdump -d`. Recompiling the original bad flag is a failing negative control.

The HAL owns Rust image preparation, confidence/box decoding and bounded NMS.
See [detector API and ownership](../../hal/detector/README.md),
[PICO qualification](../../docs/camera-pico-qualification.md), and
[the model's MIT notice](../../hal/detector/models/PICO-NOTICE).
