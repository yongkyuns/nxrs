//! Standalone entry point for the shared portable GATT scenario.
#![forbid(unsafe_code)]

#[cfg(any(
    target_os = "nuttx",
    all(any(target_os = "macos", target_os = "linux"), not(test))
))]
fn main() {
    let mode = match ble_client_fixture::Mode::from_args(std::env::args().skip(1)) {
        Some(mode) => mode,
        _ => {
            eprintln!("usage: gatt_client [cancel|values|legacy]");
            std::process::exit(2);
        }
    };
    let mut observer = |progress| {
        match progress {
            ble_client_fixture::Progress::FirstWrite { .. } => {
                println!("NXRS_GATT_PROGRESS first_write=confirmed");
            }
            ble_client_fixture::Progress::LastRead { pairs, .. } => {
                println!("NXRS_GATT_PROGRESS completed_pairs={pairs}");
            }
            _ => {}
        }
        Ok(())
    };
    if !ble_client_fixture::run_with(mode, &mut observer, ble::open, |wake, recovery_ms| {
        ble::gatt::open::<{ ble_client_fixture::CAPACITY }>(move || wake.notify(), recovery_ms)
    }) {
        std::process::exit(1);
    }
}

#[cfg(not(any(
    target_os = "nuttx",
    all(any(target_os = "macos", target_os = "linux"), not(test))
)))]
fn main() {
    eprintln!("gatt-client requires an explicitly selected hardware platform; tests do not access BLE hardware");
}
