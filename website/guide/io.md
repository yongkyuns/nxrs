# Networking and storage

Many firmware applications combine device control with communication and persistent data. NXRS uses standard Rust APIs for ordinary file and IP operations, with hardware-specific control kept in the owning HAL or platform integration.

## Networking

Use `std::net` for supported TCP and UDP data paths. Configure the network interface, addressing, and any radio connection before starting application traffic. Wi-Fi or Bluetooth control belongs to the relevant capability implementation rather than the socket code.

A connection-handling component can own its socket, receive commands through a bounded inbox, and report state changes to the application. Define timeouts, retry limits, and shutdown behavior alongside the protocol. Bound pending messages so a disconnected peer does not accumulate unlimited work.

Protocol choice remains with the application. Using sockets does not require a project-wide broker, broadcast bus, or a specific cloud service.

## Files and storage

Use `std::fs` and `std::io` when the selected target supplies the required filesystem and device. Mount and configure the storage through NuttX and the platform integration before opening application files.

Keep record formats separate from the filesystem implementation. Decide when data is buffered, flushed, rotated, or discarded, and report write failures to the owner. A recorder worker can isolate slower storage operations from other processing while keeping a fixed buffer budget.

The existing [storage HAL](../../hal/storage/) includes recording support, and the [transport HAL](../../hal/transport/) includes IP transport support. Their source and module documentation describe the concrete interfaces and feature selection.

## Design example: a connected data logger

A logger can have an input component, a processing component, a recorder, and a network connection manager. Typed records pass between them; configuration describes the input device, storage, and network resources.

When connectivity is lost, the application decides whether to retain records locally, reject new uploads, or publish only the latest state after reconnecting. When storage fills, it applies an explicit retention policy. These are application decisions, not requirements imposed by NXRS.

This arrangement is useful for environmental monitoring, equipment telemetry, and other IoT applications. The [examples index](examples.md) links runnable examples and existing application code separately from these design illustrations.

## Security and testing

Authentication, encryption, credential storage, and update policy are part of the product design. See the [security reference](../../docs/security.md) for the existing integration and deployment requirements.

Test short reads and writes, disconnects, timeouts, full storage, and shutdown while I/O is active. [Testing and debugging](testing.md) describes the test workflow; [Performance and memory](performance.md) covers retained buffers, queue storage, and throughput.
