//! Explicit synthetic source used by session ownership tests.
#[cfg(not(platform = "mock"))]
#[allow(dead_code, unused_imports)]
#[path = "platform/mock/mod.rs"]
pub(crate) mod mock_tests;
#[cfg(platform = "mock")]
pub(crate) use crate::platform::mock as mock_tests;

#[cfg(unix)]
#[path = "platform/nuttx/mod.rs"]
mod physical;
#[cfg(unix)]
mod physical_tests;
#[cfg(unix)]
pub(crate) mod ffi {
    use std::{ffi::c_int, io};
    pub static FAIL_RESTORE: std::sync::atomic::AtomicBool =
        std::sync::atomic::AtomicBool::new(false);
    pub fn nonblocking_flag() -> c_int {
        0
    }
    pub fn capture(_: c_int, _: &mut [u8; 64]) -> io::Result<()> {
        Ok(())
    }
    pub fn configure(_: c_int, _: &[u8; 64], _: u32) -> io::Result<()> {
        Ok(())
    }
    pub fn restore(_: c_int, _: &[u8; 64]) -> io::Result<()> {
        if FAIL_RESTORE.swap(false, std::sync::atomic::Ordering::AcqRel) {
            Err(io::Error::other("injected termios restore failure"))
        } else {
            Ok(())
        }
    }
}
#[cfg(unix)]
pub(crate) mod events {
    use std::{
        fs::File,
        io,
        os::{fd::OwnedFd, unix::net::UnixStream},
    };
    pub static FAIL_PIPE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    pub fn pipe() -> io::Result<(File, File)> {
        if FAIL_PIPE.swap(false, std::sync::atomic::Ordering::AcqRel) {
            return Err(io::Error::other("injected cancellation setup failure"));
        }
        let (a, b) = UnixStream::pair()?;
        a.set_nonblocking(true)?;
        b.set_nonblocking(true)?;
        Ok((File::from(OwnedFd::from(a)), File::from(OwnedFd::from(b))))
    }
    #[allow(dead_code)]
    pub enum Readiness {
        Device,
        Cancelled,
        Timeout,
    }
    pub fn poll_io(_: i32, _: i32, _: bool, _: i32) -> io::Result<Readiness> {
        std::thread::sleep(std::time::Duration::from_millis(1));
        Ok(Readiness::Device)
    }
}
