//! Test-only adapter for the actual frozen Mbed TLS SDK, never a product provider.
#[cfg(feature = "probe")]
pub mod native;

#[cfg(feature = "probe")]
pub mod fixture;
