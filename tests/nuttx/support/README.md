# Legacy core-only qualification providers

This test-only crate preserves the old read-device camera, synchronous record
sink, descriptor owner, and RCAMREC1 encoding used by the existing sim/QEMU
fault suites. Their C/POSIX compatibility operations live in `../c`.

Production HALs must not depend on this crate. Production camera capture uses
Rust file/DMA ownership and private video ioctl bindings. Production NuttX
storage uses `std::fs` and the shared Rust record writer.

The old target fault results qualify this compatibility path. They do not
qualify the new std storage writer; that writer has independent Rust I/O fault
tests and still needs NuttX target execution qualification.
