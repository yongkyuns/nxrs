//! Qualification only. Remote probe modes use ordinary Rust std; the local lab
//! mode owns the existing Wi-Fi fixture and a separately scheduled native server.
#![deny(unsafe_op_in_unsafe_fn)]

use std::io::{self, BufRead, Write};
#[cfg(not(target_os = "nuttx"))]
use std::time::Duration;

#[cfg(target_os = "nuttx")]
mod nuttx;

fn token(value: Option<String>) -> Result<String, &'static str> {
    let value = value.ok_or("missing token")?;
    if value.len() != 24 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("token must be 24 hexadecimal characters");
    }
    Ok(value)
}

fn probe() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let mode = args
        .next()
        .ok_or("expected stdio, fail, bulk, wait, tty, lab or provision")?;
    #[cfg(target_os = "nuttx")]
    match mode.as_str() {
        "lab" => {
            nuttx::finish_local(nuttx::lab());
            return Ok(());
        }
        "provision" => {
            nuttx::finish_local(nuttx::provision());
            return Ok(());
        }
        _ => {}
    }
    let token = token(args.next())?;
    match mode.as_str() {
        "stdio" => {
            println!("NXRS_SSH_READY token={token}");
            io::stdout().flush()?;
            // Bound input; an absent newline cannot allocate an unbounded line.
            let mut input = String::new();
            io::Read::take(io::stdin().lock(), 128).read_line(&mut input)?;
            if input.trim_end_matches(['\r', '\n']) != format!("input:{token}") {
                return Err("stdin payload mismatch".into());
            }
            let worker_token = token.clone();
            let worker = std::thread::Builder::new()
                .name("ssh-stdio".into())
                .stack_size(8192)
                .spawn(move || {
                    println!("NXRS_SSH_THREAD token={worker_token}");
                    42
                })?;
            if worker.join().map_err(|_| "worker panic")? != 42 {
                return Err("worker result mismatch".into());
            }
            println!("NXRS_SSH_STDOUT token={token}");
            eprintln!("NXRS_SSH_STDERR token={token}");
            println!("NXRS_SSH_SUCCESS token={token}");
        }
        "fail" => {
            eprintln!("NXRS_SSH_EXPECTED_ERROR token={token}");
            io::stderr().flush()?;
            std::process::exit(7);
        }
        "bulk" => {
            let count: usize = args.next().ok_or("missing line count")?.parse()?;
            if !(1..=16384).contains(&count) {
                return Err("line count outside 1..16384".into());
            }
            let mut stdout = io::stdout().lock();
            for sequence in 0..count {
                writeln!(stdout, "NXRS_SSH_DATA token={token} sequence={sequence:05}")?;
            }
            writeln!(stdout, "NXRS_SSH_BULK_DONE token={token} lines={count}")?;
        }
        "wait" => {
            #[cfg(target_os = "nuttx")]
            return nuttx::wait_for_interrupt(&token).map_err(Into::into);
            #[cfg(not(target_os = "nuttx"))]
            {
                println!("NXRS_SSH_WAIT token={token}");
                io::stdout().flush()?;
                // Finite even when a target fails to deliver Ctrl-C. A timeout is a
                // failed interrupt check, never interpreted as a successful signal.
                std::thread::sleep(Duration::from_secs(60));
                println!("NXRS_SSH_WAIT_EXPIRED token={token}");
            }
        }
        #[cfg(target_os = "nuttx")]
        "tty" => {
            let (rows, columns) = nuttx::terminal_size()?;
            println!("NXRS_SSH_TTY token={token} rows={rows} columns={columns}");
        }
        _ => return Err("unsupported mode".into()),
    }
    Ok(())
}

fn main() {
    if let Err(error) = probe() {
        eprintln!("NXRS_SSH_PROBE_FAIL error={error}");
        std::process::exit(1);
    }
}
