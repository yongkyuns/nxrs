#![allow(unsafe_code)]

use std::ffi::{c_char, c_int};
use std::fs::{File, OpenOptions};
use std::io;
use std::net::Ipv4Addr;
use std::os::{fd::AsRawFd, unix::fs::OpenOptionsExt};

pub fn open(path: &str) -> io::Result<File> {
    // SAFETY: this returns the target O_NONBLOCK constant and retains no data.
    let flag = unsafe { cellular_nonblock() };
    OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(flag)
        .open(path)
}

pub fn raw(uart: &File) -> io::Result<()> {
    // SAFETY: the descriptor stays valid during this synchronous ioctl.
    check(unsafe { cellular_raw(uart.as_raw_fd()) })
}

pub fn wait(
    uart: Option<&File>,
    tun: Option<&File>,
    stop: Option<&File>,
    write: Option<&File>,
    ms: i32,
) -> io::Result<()> {
    let fd = |file: Option<&File>| file.map_or(-1, AsRawFd::as_raw_fd);
    // SAFETY: the descriptors remain borrowed until poll returns.
    let result = unsafe { cellular_poll(fd(uart), fd(tun), fd(stop), fd(write), ms) };
    if result < 0 && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub struct Interface([c_char; 16]);

impl Interface {
    pub fn create(tun: &File) -> io::Result<Self> {
        let mut name = [0; 16];
        // SAFETY: IFNAMSIZ is checked by the target and all storage is writable.
        check(unsafe { cellular_tun(tun.as_raw_fd(), name.as_mut_ptr()) })?;
        Ok(Self(name))
    }

    pub fn bring_up(&self, address: Ipv4Addr, gateway: Ipv4Addr) -> io::Result<()> {
        let name = self.0.as_ptr();
        // SAFETY: the kernel supplied the terminated interface name. Each call
        // borrows exactly four IPv4 bytes for the duration of the call.
        unsafe {
            check(cellular_addr(name, address.octets().as_ptr()))?;
            check(cellular_gateway(name, gateway.octets().as_ptr()))?;
            check(cellular_mask(name, [255; 4].as_ptr()))?;
            check(cellular_up(name))
        }
    }
}

fn check(result: c_int) -> io::Result<()> {
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

unsafe extern "C" {
    fn cellular_nonblock() -> c_int;
    fn cellular_raw(fd: c_int) -> c_int;
    fn cellular_poll(uart: c_int, tun: c_int, stop: c_int, write: c_int, ms: c_int) -> c_int;
    fn cellular_tun(fd: c_int, name: *mut c_char) -> c_int;
    fn cellular_addr(name: *const c_char, addr: *const u8) -> c_int;
    fn cellular_gateway(name: *const c_char, addr: *const u8) -> c_int;
    fn cellular_mask(name: *const c_char, addr: *const u8) -> c_int;
    fn cellular_up(name: *const c_char) -> c_int;
}
