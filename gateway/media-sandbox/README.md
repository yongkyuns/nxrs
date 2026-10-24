# Media process isolation

`nxrs-media-sandbox` is a single-threaded Linux launcher for untrusted media
code. It closes inherited descriptors above stderr, requires full Landlock ABI3
filesystem enforcement and no-new-privileges, installs a native-ABI seccomp
filter, then replaces itself with the requested executable. Failure exits with
a fixed category; there is no unconfined fallback. No privilege, user namespace
or persistent host configuration change is required.

The decoder may read/execute its selected binary and system library directories,
read the loader cache/timezone, and access `/dev/null`. The optional private
`--work DIRECTORY` grant is reserved for per-attempt recording/finalizer data;
it must never contain credentials. Ingest grants no working directory. Known
credential paths are checked against both lexical and resolved grants before
admission. Filesystem metadata visibility is not hidden.

The seccomp policy rejects socket creation (including UDP/Unix), io_uring,
ptrace/process-memory/foreign-FD access, signal injection, BPF and performance
inspection. Alternate syscall ABIs are rejected. Descendants inherit the
restrictions. Encoder input/output remain existing pipes, with parent deadline,
progress, reaping and service cgroup controls preserved.

The installed launcher, executable and library hierarchy are trusted. This is
not protection against kernel/root compromise or proof of parser safety. The
credentialed publisher and recorder still need their own isolation review. The
activity recorder currently gives its token to FFmpeg over stdin; separating
its authenticated reader from decoders remains open under issue37.

The strict ingest renderer requires the helper and performs an enforcement
probe; older configurations need an explicit `media_sandbox` path migration
without changing enrollment/TLS/denial state. Development execution can omit
the field outside the strict service. macOS and unsupported Linux architectures
refuse this launcher rather than providing weaker enforcement.

Build with `cargo build --manifest-path gateway/Cargo.toml --locked -p nxrs-media-sandbox`. `--check` applies restrictions only to its disposable child
and returns a fixed ready marker on complete enforcement. Actual runtime
qualification is pending. The hosted fixture exercises both a real unconfined
positive control and restricted file/FD/process/socket/signal controls, followed
by real multithreaded FFmpeg JPEG/H264 encoding. Only its bounded numeric/hash
report may be uploaded. The fixture refuses non-hosted machines before acting.

The policy follows the kernel's [Landlock filesystem and ptrace
semantics](https://docs.kernel.org/userspace-api/landlock.html) and the pinned
[upstream Rust API](https://docs.rs/landlock/0.4.7/landlock/).
