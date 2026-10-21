pub(crate) mod gatt;
pub(crate) mod gatt_backend;
#[cfg(any(nxrs_provider = "native", test))]
pub mod native;
#[cfg(any(nxrs_provider = "nuttx", test))]
pub mod nuttx;
