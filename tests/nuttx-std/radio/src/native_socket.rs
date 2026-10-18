//! Test-only control: native socket calls inside the unchanged traffic loop.
use std::io;
use std::net::SocketAddrV4;

unsafe extern "C" {
    fn nxrs_radio_tcp_open(address: *const u8, port: u16) -> i32;
    fn nxrs_radio_tcp_transfer(fd: i32, data: *mut u8, length: usize, sending: i32) -> i32;
    fn nxrs_radio_tcp_finish(fd: i32) -> i32;
    fn nxrs_radio_udp_open(address: *const u8, port: u16) -> i32;
    fn nxrs_radio_udp_send(fd: i32, data: *const u8, length: usize) -> i32;
    fn nxrs_radio_udp_receive(fd: i32, data: *mut u8, length: usize) -> i32;
    fn nxrs_radio_socket_close(fd: i32) -> i32;
    fn nxrs_radio_socket_snapshot(fd: i32, values: *mut u32, count: usize) -> i32;
    fn nxrs_radio_tcp_state(fd: i32, values: *mut u32, count: usize) -> i32;
}

pub(super) struct Socket(i32);

pub(super) fn snapshot(fd: i32) -> Option<[u32; 5]> {
    let mut values = [0; 5];
    // SAFETY: native code only peeks; the fixed-width output lives for this call.
    let result = unsafe { nxrs_radio_socket_snapshot(fd, values.as_mut_ptr(), values.len()) };
    (result == 0).then_some(values)
}

pub(super) fn tcp_state(fd: i32) -> Option<[u32; 8]> {
    let mut values = [0; 8];
    // SAFETY: C holds a descriptor reference and the native connection/device locks.
    // Its native connection pointer is never retained or exposed to Rust.
    let result = unsafe { nxrs_radio_tcp_state(fd, values.as_mut_ptr(), values.len()) };
    (result == 0).then_some(values)
}

fn checked(result: i32) -> io::Result<i32> {
    if result < 0 {
        Err(io::Error::from_raw_os_error(-result))
    } else {
        Ok(result)
    }
}

impl Socket {
    pub(super) fn raw_fd(&self) -> i32 {
        self.0
    }

    pub(super) fn open(peer: SocketAddrV4, tcp: bool) -> io::Result<Self> {
        // SAFETY: C copies four address bytes and retains no Rust pointers.
        let address = peer.ip().octets();
        let result = unsafe {
            if tcp {
                nxrs_radio_tcp_open(address.as_ptr(), peer.port())
            } else {
                nxrs_radio_udp_open(address.as_ptr(), peer.port())
            }
        };
        checked(result).map(Self)
    }

    pub(super) fn transfer(&mut self, data: &mut [u8], sending: bool) -> io::Result<usize> {
        // SAFETY: exclusive buffer valid for the call; this object owns the fd.
        let length = checked(unsafe {
            nxrs_radio_tcp_transfer(self.0, data.as_mut_ptr(), data.len(), i32::from(sending))
        })? as usize;
        if length == 0 && !data.is_empty() {
            return Err(if sending {
                io::ErrorKind::WriteZero
            } else {
                io::ErrorKind::UnexpectedEof
            }
            .into());
        }
        Ok(length)
    }

    pub(super) fn finish(self) -> io::Result<()> {
        // SAFETY: fd remains owned until Drop, including failed half-close.
        checked(unsafe { nxrs_radio_tcp_finish(self.0) }).map(|_| ())
    }

    pub(super) fn send_datagram(&self, data: &[u8]) -> io::Result<usize> {
        // SAFETY: readable slice is valid for this single synchronous call.
        checked(unsafe { nxrs_radio_udp_send(self.0, data.as_ptr(), data.len()) })
            .map(|length| length as usize)
    }

    pub(super) fn receive_datagram(&self, data: &mut [u8]) -> io::Result<usize> {
        // SAFETY: exclusive writable slice is valid only for this call.
        checked(unsafe { nxrs_radio_udp_receive(self.0, data.as_mut_ptr(), data.len()) })
            .map(|length| length as usize)
    }
}

impl Drop for Socket {
    fn drop(&mut self) {
        // SAFETY: unique ownership; never retry close (fd could be reused).
        unsafe { nxrs_radio_socket_close(self.0) };
    }
}
