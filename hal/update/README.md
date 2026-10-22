# Authenticated secondary-slot staging

`nxrs-update` owns one bounded Rust worker and an immutable image buffer. It
accepts independently trusted `Policy` and a caller-supplied `Crypto` provider.
Authentication includes the canonical nxrs manifest, approved RSA root, payload,
layout, configuration, version and nonzero admission floor before any erase.
There is no accepting crypto fallback. The floor supplied here is not protected
rollback storage, and `StagedImage` is result metadata, not activation authority.

The worker validates its resource, refuses pending/partial/unknown boot control
state in primary, secondary or scratch, erases the secondary's last block first,
then programs and reads back the exact image. It checks the remaining secondary
bytes, including alignment padding, swap status and trailer, are erased. It
never writes primary or scratch, selection magic, image-ok or a rollback counter.
A primary control tail is accepted only when erased or when it describes a
known, fully copied, confirmed single-image swap; other states need recovery
ownership. This is a staging component, not a complete update/boot state machine.

`stage` transfers the boxed image to the worker, or returns it on refusal. Only
one request may exist through its quiescence barrier. Request IDs are not reused.
`poll` and `poll_shutdown` do not wait for native I/O. A cancellation/deadline may
report an outcome with `quiescent=false`; buffers, native ownership and the lease
remain held until the native call returns. Drain the later `Quiescent` update
before admitting another request. Expected authentication, busy-boot and explicit
cancellation refusals permit another request; I/O, readback, timeout and worker
faults remain sticky. A failed retirement never proves resources were released.
Dropping the controller signals shutdown and detaches its owning worker. It does
not free in-flight resources. Wake functions must be bounded and nonpanicking;
they are Rust scheduling notifications, not native callbacks.

Provider selection is product wiring:

- `mock-update` explicitly selects a NOR memory model with Rust threads. This
  model supplies no protected counter, native timing or hardware evidence.
- `esp32s3-camera-update` extends the camera MCUboot qualification profile and
  enables the real raw SPI-flash MTD, internal kernel heap and PSRAM-stack path.
- `esp32s3-update-qemu` supplies an explicit qualification-only 16 MiB resource
  layout and uses the common ESP raw-MTD driver without PSRAM/radio/camera.

The NuttX provider obtains the kernel's actual static SDK MTD object. Its private
C file only translates target headers/vtable calls, geometry and the internal
kernel allocator. Rust creates checked partition views from the product profile,
requires exact policy/resource geometry, and limits every mutation to secondary.
A boot profile's layout cannot be overridden by fixture settings. Both
qualification profiles disable automatic SPI-flash filesystem setup and BCH.
Their general storage node is explicitly outside the entire boot/firmware prefix
at offset `0x260000`, length `0x100000`. The build resolver rejects overlap,
zero/unbounded size, misalignment and out-of-flash ranges. The C boundary refuses
automatic filesystem configurations, and Rust checks the actual target-header
storage bounds before acquiring a usable resource. The provider's
lease prevents concurrent owners within this capability; it does not isolate
privileged kernel code or debug shell access. These profiles are qualification
profiles, not production security configurations.

Ordinary file opens can introduce an FTL/BCH proxy, so raw NOR operations use the
actual MTD SDK. The private 4 KiB bounce buffer uses internal kernel memory. The
legacy PSRAM-stack driver queues to LPWORK and waits uninterruptibly for native
completion; no cancellation path frees its buffer, stack or lease early. The
kernel retains its static device; retirement frees only this provider's buffer
and lease after synchronous I/O ends. Encrypted-flash configurations are refused.

Run the nine resource/lifecycle model tests with:

```
NXRS_PLATFORM=mock-update cargo +1.90.0 test --locked -p nxrs-update --lib
```

With the pinned target Python environment, run
`tools/boot/qualify_admission.py --staging --work <fresh-private-directory>` for
the real SDK crypto, sanitizer probes, malformed-image suite and 31 cases through
the production staging owner on the explicit model. On disposable Linux,
`qualify_admission_target.py --staging --nuttx` additionally requires both full
NuttX images, actual SDK symbols/ABI gates and three fresh emulator kernels. The
staging kernel writes only a disposable private flash copy; its exact final
secondary image/erased tail and every byte outside secondary must match the
expected result. The original flash input must stay unchanged. Only bounded
public JSON is uploaded; keys, images and raw console/build logs remain private.

Actual-header compilation, full target linking, emulator execution and physical
qualification are separate evidence. Protected rollback state, production crypto
and update endpoint integration, ROM trust, trial/confirmation, physical power
loss/recovery, hardware timing/memory, manufacturing/debug policy and release
integration remain required. This component authorizes no flashing or eFuses.
