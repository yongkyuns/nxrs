#![cfg_attr(target_os = "none", no_std)]

mod arrivals;
mod engine;
mod platform;
mod transport;
use core::fmt::{self, Write};

pub(crate) fn line(args: fmt::Arguments<'_>) {
    struct Buffer {
        bytes: [u8; 1024],
        used: usize,
    }
    impl Write for Buffer {
        fn write_str(&mut self, s: &str) -> fmt::Result {
            let end = self.used + s.len();
            if end > self.bytes.len() {
                return Err(fmt::Error);
            }
            self.bytes[self.used..end].copy_from_slice(s.as_bytes());
            self.used = end;
            Ok(())
        }
    }
    let mut b = Buffer {
        bytes: [0; 1024],
        used: 0,
    };
    if b.write_fmt(args).is_err() {
        platform::fatal();
    }
    platform::output(&b.bytes[..b.used]);
    platform::output(b"\n");
}
pub(crate) fn check(ok: bool, why: &str) {
    if !ok {
        line(format_args!("MPSC_FAILURE {why}"));
        platform::fatal();
    }
}

fn execute(args: &[&str], backend: &str, run: fn(engine::Mode, usize, usize, usize, usize)) {
    static USED: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);
    check(
        !USED.swap(true, core::sync::atomic::Ordering::Relaxed),
        "fresh-boot-required",
    );
    check(
        args.len() == 6,
        "usage: mode std|native producers payload capacity samples",
    );
    let mode = engine::Mode::parse(args[0]);
    let producers: usize = args[2].parse().unwrap_or(0);
    let payload: usize = args[3].parse().unwrap_or(0);
    let capacity: usize = args[4].parse().unwrap_or(0);
    let samples: usize = args[5].parse().unwrap_or(0);
    check(
        matches!(producers, 1 | 4)
            && matches!(payload, 16 | 64)
            && matches!(capacity, 8 | 64)
            && (1..=if mode == engine::Mode::Sustained {
                131072
            } else {
                2048
            })
                .contains(&samples)
            && samples % producers == 0,
        "configuration",
    );
    check(args[1] == backend, "backend");
    run(mode, producers, payload, capacity, samples);
}

pub fn execute_std(args: &[&str]) {
    execute(args, "std", transport::run_std);
}

pub fn execute_native(args: &[&str]) {
    execute(args, "native", transport::run_native);
}

#[cfg(any(target_os = "none", target_os = "nuttx"))]
#[unsafe(no_mangle)]
pub extern "C" fn mpsc_entry() {
    line(format_args!("MPSC_READY"));
    let mut command = [0u8; 128];
    let mut length = 0;
    let start = platform::monotonic_ns();
    let mut last_ready = start;
    loop {
        check(
            platform::monotonic_ns() - start < 120_000_000_000,
            "console-timeout",
        );
        match platform::read() {
            Some(b'\r' | b'\n') if length != 0 => break,
            Some(b'\r' | b'\n') => {}
            Some(byte) => {
                check(length < command.len(), "command-size");
                command[length] = byte;
                length += 1;
            }
            None => {
                // USB enumeration and host raw-mode setup can discard startup
                // output. Repeat readiness only while waiting for a command;
                // no reporting runs during a measurement.
                let now = platform::monotonic_ns();
                if length == 0 && now - last_ready >= 1_000_000_000 {
                    line(format_args!("MPSC_READY"));
                    last_ready = now;
                }
                platform::sleep_ns(1_000_000);
            }
        }
    }
    let text = core::str::from_utf8(&command[..length]).unwrap_or("");
    let mut words = [""; 6];
    let mut count = 0;
    for word in text.split_ascii_whitespace() {
        check(count < 6, "command-count");
        words[count] = word;
        count += 1;
    }
    check(count == 6, "command-count");
    platform::execute(&words);
}

#[cfg(target_os = "none")]
#[panic_handler]
fn panic(_: &core::panic::PanicInfo<'_>) -> ! {
    platform::fatal()
}

pub extern "C" fn mpsc_deadline() {
    platform::fatal();
}

pub fn main() {
    platform::main();
}
