#![deny(unsafe_code)]

use navigation::observations::ObservationService;
use std::time::Duration;

/// Instantiate the production service that emits the startup diagnostic.
pub fn configure(with_gnss: bool) {
    let _ = ObservationService::<1>::new(
        Duration::from_millis(500),
        with_gnss.then_some(Duration::from_secs(3)),
    );
}

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
mod browser;
