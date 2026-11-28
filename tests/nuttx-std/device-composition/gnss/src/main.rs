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
    if let Some(mut aiding) = session.aiding() {
        // Retain the public admission/reconciliation path in the target image.
        // An invalid lifetime exercises nonadmission without seeding a receiver
        // from an invented time. black_box prevents constant-folding the input.
        let invalid = gnss::aiding::Aid::Time(gnss::aiding::TimeAid {
            unix_time_ms: 0,
            observed_at_us: gnss::monotonic_us(),
            uncertainty: std::time::Duration::from_secs(1),
            max_age: std::time::Duration::ZERO,
            drift_ppm: 100,
        });
        if aiding.capabilities().time {
            assert_eq!(
                aiding.try_submit(black_box(invalid)),
                Err(gnss::aiding::SubmitError::InvalidInput)
            );
            assert!(aiding.try_completion().is_none());
        }
    }
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
