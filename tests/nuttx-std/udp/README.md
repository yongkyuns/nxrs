# Direct std UDP qualification

This is an ordinary Rust `main()` using `std::net::UdpSocket`, not PacketSink,
a project socket wrapper, or the existing C transport bridge. It is a separate,
non-default test binary/image. Production app/providers are unchanged.

## Run

Native: `cargo +1.100.0 build --locked -p nxrs-std-udp`, then
`python3 tests/nuttx-std/udp/run.py --native target/debug/nxrs-std-udp`.

NuttX: install the same pinned prerequisites as the RV32 std probe, then run
`NUTTX_STD_COMPAT_FIXES=1 bash tests/nuttx-std/build.sh udp`, followed by
`python3 tests/nuttx-std/udp/run.py --image target/nuttx-udp/nuttx/nuttx`.
The build explicitly enables IPv4 UDP loopback without external networking.
The standard-library build uses a private copied SDK: the existing parker,
descriptor-sanitization and SIG_IGN fixes remain, and the exact locked libc
source is locally overridden only inside that copy to match NuttX's required
8-byte sockaddr_storage alignment. The installed Rust toolchain and Cargo
registry cache are not modified.

## Gates and scope

The existing startup/thread ABI and unsafe-poll import gates run unchanged.
A separate socket gate then compiles C and Rust observations with the exact
commands/toolchain/libc used by that build. It checks socket scalars, IPv4
address layouts, generic address storage capacity/alignment, timeval, relevant
socket/ioctl/descriptor constants and error values. Generic address storage
may be larger, never underaligned; no socket mismatch is whitelisted.
The report and raw C/Rust witnesses are retained even if the gate fails.
Runtime execution requires a passing socket report with matching image/config
hashes; earlier thread-only passes cannot authorize this test.

The runtime probe checks numeric loopback addresses, nonblocking WouldBlock,
receive-timeout behavior, sixteen exact packets from a joined std thread with
bounded-channel acknowledgements, independent sockets, clone/drop ownership,
a queued zero-length UDP datagram, socket error lookup and port rebind after
close. The pinned NuttX commit fixes its UDP readahead path so a valid
zero-length datagram (recv length 0) is not mistaken for the -1 "no buffered
datagram" sentinel. Three fresh kernels must return cleanly with success. A deliberate exit(7) must produce no
success report and NSH must report failure. Native execution uses the identical
Rust source. Python verifies the packet transcript/report and exit status.
This is not an independent network peer: both live endpoints use std in one
kernel; the independent C/Rust ABI gate guards against common binding errors.

A source implementation is not a qualification result. Keep this path out of
production until the exact build and runtime gates pass. IPv6, DNS, TCP, other
socket options, radio hardware, browser networking, wall-clock deadlines and
full camera-app deployment are outside this first RV32 UDP profile. The
qualification records the exact NuttX and private-libc repairs and refuses
unreviewed source/version drift; neither repair is hidden in application code.
