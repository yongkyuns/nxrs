#[cfg(any(
    all(
        nxrs_provider = "native",
        any(target_os = "macos", target_os = "linux")
    ),
    test
))]
pub(crate) mod native;
#[cfg(any(nxrs_provider = "nuttx", test))]
pub mod nuttx;
