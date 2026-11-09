# Getting started

Start on your development computer. The navigation demo exercises the application and service model without requiring a board, a sensor, an async runtime or a NuttX cross-toolchain.

## Prerequisites

Install Git and Rust through rustup, along with your operating system’s native linker and build tools. Work from a terminal in the repository root. The checkout’s `rust-toolchain.toml` selects the host Rust toolchain; do not replace it with a guessed target toolchain.

The commands below use a POSIX shell. On Windows, use the PowerShell equivalents shown separately. Python 3 is needed for the repository’s independent checks and target tooling, but the first Rust host demo does not build the documentation website.

## Clone the project

```sh
git clone https://github.com/yongkyuns/nxrs.git
cd nxrs
rustup show
```

Run `rustup show` inside the checkout so you can see the toolchain override. Initial Cargo builds may need network access to obtain the locked dependencies. `--locked` preserves dependency resolution; it does not mean “offline.”

## Run the navigation demo

```sh
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 2000
```

PowerShell:

```powershell
$env:NXRS_PLATFORM = "host-demo"
cargo run --locked -p nxrs-event-demo -- --duration-ms 2000
Remove-Item Env:NXRS_PLATFORM
```

`host-demo` explicitly supplies synthetic IMU and GNSS providers. It is not a physical-board profile. The app composes sensor services, a fusion owner and synchronous health processing, exercises GNSS pause/resume, and stops after the requested interval.

## Read the result

The output begins with `EVENT_DEMO topology:`. Navigation observations use the `NAV` prefix; the two control phases use `CONTROL`. A successful completed run prints `EVENT_DEMO PASS` with observation, production, drop and error counters and exits successfully.

Do not copy particular counts into an acceptance rule: scheduling changes them. Read the process exit status and the complete transcript. A success marker followed by a fault is not a clean run.

The demo is intentionally finite. You do not need to terminate it to demonstrate normal teardown.

## Explore the checkout

```sh
cargo nxrs apps
cargo nxrs platforms
cargo nxrs build --help
```

These commands discover the application/build vocabulary. `cargo nxrs` is a repository Cargo alias, not a claim that an `nxrs` executable has been installed globally from crates.io.

The key starting files are the [application](../../app/event-demo/src/main.rs), its [Cargo manifest](../../app/event-demo/Cargo.toml), and the [navigation demo reference](../../docs/event-driven-demo.md).

## Next steps

Follow [Your first application](first-application.md) to understand the composition, then read [Services and concurrency](services.md). Use [Building for NuttX](firmware.md) only after preparing the selected target’s dependencies. The [troubleshooting guide](troubleshooting.md) separates environment, profile, runtime and device failures.
