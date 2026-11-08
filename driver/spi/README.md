# NuttX SPI resource

`nxrs-spi` implements `embedded_hal::spi::SpiDevice<u8>` over an owned NuttX
SPI character descriptor. The Rust adapter validates and flattens each
transaction into one exchange of at most 256 bytes, using two stack buffers
(512 bytes total). Read, write, transfer and in-place transfer may be combined;
unsupported delays or oversized transactions fail before any I/O. There is no
allocation per transaction, thread or user-space bus mutex.

The private C helper constructs the target-header sequence for `SPIIOC_TRANSFER`.
NuttX locks the bus, sets mode/frequency/8-bit MSB-first format, selects the device,
completes the exchange and deselects before releasing the lock. Optional native
fields are initialized through the actual header layout. Disabled SPI support
returns `ENOTSUP`; Rust reports native transfer failures as `Error::Io` without
publishing partial receive data. The underlying exchange API cannot report every
controller-level failure, so a successful ioctl is not proof of a sensor response.

Profiles supply the registered path, frequency, mode and `chip-select` index.
The index maps to **NuttX `SPIDEV_USER(index)`**, not a GPIO number. The BSP must
implement that selection, pinmux and character-device registration, with
`CONFIG_SPI_EXCHANGE` and `CONFIG_SPI_DRIVER` enabled. The stock ESP32-S3 selector
uses one configured CS and ignores the device ID unless board-defined selection
is enabled; additional indices therefore need explicit board support.

The pinned NuttX header describes this raw character interface as intended for
SPI testing, not a qualified production driver. This adapter establishes the
resource seam; production use still requires board-specific CS, DMA/buffer,
latency and fault qualification (or a hardened kernel resource interface with
the same Rust contract). Host transaction and native-layout tests plus flat/
protected target compile/link checks do not establish that qualification.
