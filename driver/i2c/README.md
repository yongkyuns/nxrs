# NuttX I²C adapter

On NuttX, `Bus::open(Config { path, frequency })` owns one controller descriptor and
implements `embedded_hal::i2c::I2c`. Peripheral drivers receive that trait;
they do not depend on this crate or on NuttX. Capability factories pass resolved
resource configuration. Device addresses are supplied per transaction.

The NuttX implementation and its target-header ioctl glue live under
`src/platform/nuttx/`. `Bus` is available only on NuttX; there are no macOS/Linux
backends, host stubs or implicit mocks. Host builds expose the configuration and
error types for test witnesses, which inject fake operations into the real Rust
adapter. There is no additional `[hal]` role or product provider switch.
Its generic build adapter compiles the owning package's native glue against the
configured target context when this resource crate is in the target graph.

The current NuttX adapter preserves the qualified transaction subset: seven-bit
addresses, one write, one read, or one write/read pair with repeated START, with
at most 64 bytes per direction. Unsupported shapes and sizes fail before I/O.
Controller serialization belongs to the NuttX I²C driver; no Rust lock is held
across sensor delays or acquisition waits. Wider transfers and additional
transaction shapes require separate qualification.

IMU diagnostics wrap this resource through the embedded-hal interface. This
crate owns no sensor protocol, sampling policy, timing observer or worker.
Host tests check shared-adapter behavior and native message construction with
fake target operations. They do not qualify the target ABI or electrical bus.
