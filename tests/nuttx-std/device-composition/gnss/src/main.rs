//! Keep one receiver's public acquisition path reachable without consuming IMU.
#![forbid(unsafe_code)]

use gnss::{Device, Session};
use std::{hint::black_box, process::ExitCode};

fn main() -> ExitCode {
    let receiver = black_box(composition::gnss::primary_gnss());

    // The hardware profile alias must select the canonical receiver object.
    #[cfg(target_os = "nuttx")]
    assert_eq!(
        black_box(receiver as *const _ as *const ()),
        black_box(composition::gnss::primary_alias() as *const _ as *const ())
    );

    println!("device={}", receiver.id());
    let mut session = match receiver.open(|event| {
        black_box(event);
        Ok(())
    }) {
        Ok(session) => session,
        Err(error) => {
            eprintln!("device={} open failed: {error:?}", receiver.id());
            return ExitCode::FAILURE;
        }
    };
    let stopped = session.request_stop();
    let joined = session.join();
    if let Err(error) = stopped {
        eprintln!("device={} stop failed: {error:?}", receiver.id());
    }
    if let Err(error) = joined {
        eprintln!("device={} join failed: {error:?}", receiver.id());
    }
    if stopped.is_ok() && joined.is_ok() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
