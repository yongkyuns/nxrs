//! OS camera wiring. Replay remains explicit and has no OS identity.
#[cfg(any(target_os = "nuttx", all(test, unix)))]
#[cfg_attr(test, allow(dead_code))]
mod nuttx;
#[cfg(target_os = "nuttx")]
pub fn open() -> Result<impl crate::Camera, crate::DeviceError> {
    nuttx::LiveCamera::acquire()
}
#[cfg(not(target_os = "nuttx"))]
#[cfg_attr(test, allow(dead_code))]
pub fn open() -> Result<crate::replay::ReplayCamera, crate::DeviceError> {
    Err(crate::DeviceError::Unsupported)
}
