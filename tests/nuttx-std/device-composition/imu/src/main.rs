//! Keep two named devices and their public acquisition paths reachable at link time.
#![forbid(unsafe_code)]

use imu::{Device, DeviceError, Session};
use std::{hint::black_box, process::ExitCode};

fn stop(id: &str, session: &mut Result<impl Session, DeviceError>) -> bool {
    let result = match session {
        Ok(session) => session.request_stop(),
        Err(error) => {
            eprintln!("device={id} open failed: {error:?}");
            return false;
        }
    };
    if let Err(error) = result {
        eprintln!("device={id} stop failed: {error:?}");
        return false;
    }
    true
}

fn join(id: &str, session: &mut Result<impl Session, DeviceError>) -> bool {
    match session {
        Ok(session) => match session.join() {
            Ok(()) => true,
            Err(error) => {
                eprintln!("device={id} join failed: {error:?}");
                false
            }
        },
        Err(_) => false,
    }
}

fn main() -> ExitCode {
    let primary = black_box(composition::imu::primary_imu());
    let secondary = black_box(composition::imu::secondary());
    let primary_address = black_box(primary as *const _ as *const ());
    let secondary_address = black_box(secondary as *const _ as *const ());
    assert_ne!(primary_address, secondary_address);

    // Profile aliases are generated only for the physical catalogue. Host mock
    // qualification uses the fixed primary/secondary fixture names above.
    #[cfg(target_os = "nuttx")]
    {
        assert_eq!(
            primary_address,
            black_box(composition::imu::primary_alias() as *const _ as *const ())
        );
        assert_eq!(
            primary_address,
            black_box(composition::imu::imu() as *const _ as *const ())
        );
    }

    println!("devices={},{}", primary.id(), secondary.id());
    // Both calls are reachable from main under release LTO. Keeping the first
    // result alive while opening the second also retains independent sessions.
    let mut primary_session = primary.open_default(|event| {
        black_box(event);
        Ok(())
    });
    let mut secondary_session = secondary.open_default(|event| {
        black_box(event);
        Ok(())
    });

    // Stop both before joining either, and do not skip cleanup after an error.
    let primary_stopped = stop(primary.id(), &mut primary_session);
    let secondary_stopped = stop(secondary.id(), &mut secondary_session);
    let primary_joined = join(primary.id(), &mut primary_session);
    let secondary_joined = join(secondary.id(), &mut secondary_session);
    if primary_stopped && secondary_stopped && primary_joined && secondary_joined {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
