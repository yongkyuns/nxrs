//! Native SDK qualification only. Run after all camera/radio owners have joined.
#[cfg(target_os = "nuttx")]
fn run() -> Result<(), String> {
    use boot::Control;
    use std::os::fd::AsRawFd;
    let args: Vec<_> = std::env::args().skip(1).collect();
    let [operation] = args.as_slice() else {
        return Err("usage: boot_control state|trial|confirm".into());
    };
    // A repeated status query must not exhaust native flash/file references.
    let descriptor = || std::fs::File::open("/dev/null").map(|file| file.as_raw_fd());
    let before = descriptor().map_err(|_| "descriptor probe")?;
    for _ in 0..64 {
        let mut control = boot::open().map_err(|e| format!("acquire {e:?}"))?;
        if !matches!(boot::open(), Err(boot::DeviceError::Busy)) {
            return Err("exclusive owner not enforced".into());
        }
        let _ = control.state().map_err(|e| format!("state {e:?}"))?;
    }
    if descriptor().map_err(|_| "descriptor probe")? != before {
        return Err("native descriptor reference leaked".into());
    }
    let mut control = boot::open().map_err(|e| format!("acquire {e:?}"))?;
    let initial = control.state().map_err(|e| format!("state {e:?}"))?;
    match operation.as_str() {
        "state" => (),
        "trial" => control
            .request_trial()
            .map_err(|e| format!("trial {e:?}"))?,
        "confirm" => control.confirm().map_err(|e| format!("confirm {e:?}"))?,
        _ => return Err("unknown boot operation".into()),
    }
    let state = control.state().map_err(|e| format!("state {e:?}"))?;
    println!(
        "BOOT_CONTROL_PASS before={initial:?} after={state:?} descriptor_stable=1 exclusive=1"
    );
    Ok(())
}
fn main() {
    #[cfg(target_os = "nuttx")]
    if let Err(e) = run() {
        eprintln!("BOOT_CONTROL_FAIL {e}");
        std::process::exit(1);
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        eprintln!("requires the qualified NuttX MCUboot fixture");
        std::process::exit(2);
    }
}
