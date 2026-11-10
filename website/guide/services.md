# Services and concurrency

A service is reusable application behavior with a defined interface and lifecycle. It may be a synchronous object or an active component with its own worker. NXRS uses ordinary Rust functions, threads, and synchronization rather than requiring an async executor.

## Start with ownership

Keep mutable state with the code that updates it. When a component needs independent execution, give it one event loop and send typed commands to that loop. The application constructs the components, connects them, and controls startup and shutdown.

A device controller, background recorder, or connection manager can use this pattern. A parser or calculation that runs as part of another operation can remain an ordinary function.

## A small event loop

The [worker example](../examples/basic-firmware/examples/worker.rs) owns a counter. It accepts `Add` and `Stop` events through a bounded inbox:

```rust
#[derive(Debug)]
enum Event {
    Add(u32),
    Stop,
}

fn process(inbox: std::sync::mpsc::Receiver<Event>) -> u64 {
    let mut total = 0;
    for event in inbox {
        match event {
            Event::Add(value) => total += u64::from(value),
            Event::Stop => break,
        }
    }
    total
}
```

The owner handles one event at a time. Calls within a handler remain synchronous. Cloning a sender allows several producers to submit commands to the same inbox.

## Run the worker in your application

Replace `../my-firmware/src/main.rs` from [Your first application](first-application.md) with this complete program:

<!-- tutorial:worker-source -->
```rust
use std::error::Error;
use std::io;
use std::sync::mpsc;
use std::thread;

#[derive(Debug)]
enum Event {
    Add(u32),
    Stop,
}

fn process(inbox: std::sync::mpsc::Receiver<Event>) -> u64 {
    let mut total = 0;
    for event in inbox {
        match event {
            Event::Add(value) => total += u64::from(value),
            Event::Stop => break,
        }
    }
    total
}

fn main() -> Result<(), Box<dyn Error>> {
    let (sender, inbox) = mpsc::sync_channel(4);
    let worker = thread::Builder::new()
        .name("counter".into())
        .stack_size(32 * 1024)
        .spawn(move || process(inbox))?;

    let delivery = (|| -> Result<(), mpsc::SendError<Event>> {
        for value in [1, 2, 3] {
            sender.send(Event::Add(value))?;
        }
        sender.send(Event::Stop)
    })();

    // Also let the worker finish if sending a command failed.
    drop(sender);
    let total = worker
        .join()
        .map_err(|_| io::Error::other("counter worker panicked"))?;
    delivery?;
    println!("Processed total: {total}");
    Ok(())
}
```

Keep your manifest unchanged. From the NXRS checkout, run the same host and firmware builds:

```sh
cargo run --locked --manifest-path ../my-firmware/Cargo.toml --bin hello
cargo nxrs build --manifest-path ../my-firmware/Cargo.toml --profile esp32s3-qemu-mock
```

Start QEMU with the command in [Building for NuttX](firmware.md#boot-qemu). The binary and shell command remain `hello`, but now execute the worker:

```text
nsh> hello
Processed total: 6
nsh> echo $?
0
```

The program sends three values, requests stop, and joins the worker before printing the result. It closes the sender even if delivery fails. The requested worker stack is 32 KiB; measure the stack use before selecting an allocation for a deployed application.

The checked-in source has tests for stop and disconnect behavior:

```sh
cargo test --locked --manifest-path website/examples/basic-firmware/Cargo.toml --all-targets
```

The documentation checks ensure this program matches that source. Target CI also compiles it as your application's `src/main.rs` and runs it in NuttX/QEMU.

## Choose delivery behavior

A bounded queue needs an explicit full-queue policy. `send` waits for capacity; `try_send` returns the value when the queue is full or disconnected. Choose between backpressure, rejection, or a latest-state representation according to the operation. A command that must be acted on needs different handling from a replaceable status update.

Record errors and rejected work where the application can respond. Avoid an unbounded retry list beside an otherwise bounded inbox. [Event communication](../../docs/concurrency-event-communication.md) describes NXRS delivery and lifecycle conventions in detail.

## Keep I/O out of unrelated owners

A blocking operation belongs with the component responsible for it. Hardware acquisition produces application events; it does not call arbitrary service handlers from an interrupt. A network or storage worker can own its handles and report completion through typed messages.

Use short, bounded interrupt notifications and defer parsing, allocation, and ordinary Rust channel operations to thread context. Keep registered callbacks and buffers alive until their producers have stopped.

## Shut down in order

Stop accepting new work, request producer shutdown, wake blocked waits, and join workers before releasing their resources. Decide whether admitted work is completed or cancelled. Dropping all senders closes a channel; a `Stop` message provides an explicit command while senders still exist.

Long-running I/O also needs a cancellation or timeout path. The application should be able to finish shutdown without waiting indefinitely for another device or network peer.

## Scheduling and tests

Test queue saturation, disconnects, partial startup, cancellation, and cleanup as well as normal processing. On fixed-priority targets, check the complete producer/consumer schedule: a nonblocking queue operation may still contend with other participants internally.

[Testing and debugging](testing.md) covers test setup. [Performance and memory](performance.md) covers stack, queue, latency, and throughput measurements.
