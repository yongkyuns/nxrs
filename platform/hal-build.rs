//! Shared build-only selection; runtime providers stay inside their owning HAL.
#[allow(dead_code)]
pub fn main() {
    configure();
}
pub fn configure() -> String {
    nxrs_build::configure_hal().unwrap_or_else(|error| panic!("Platform configuration: {error}"))
}
