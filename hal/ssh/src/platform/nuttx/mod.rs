use std::{fs, io, net::Ipv4Addr, thread, time::Duration};

#[allow(unsafe_code)]
mod ffi;

pub struct Session {
    pid: Option<i32>,
}

/// No credential files means SSH stays off. The native service validates
/// their contents before binding the listener.
pub fn start() -> io::Result<Option<Session>> {
    for path in ffi::credential_paths() {
        match fs::metadata(path) {
            Ok(m) if m.is_file() && m.len() != 0 => {}
            Ok(_) => return Err(io::ErrorKind::InvalidData.into()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        }
    }
    let address = ffi::bind_address()
        .parse::<Ipv4Addr>()
        .map_err(|_| io::ErrorKind::InvalidInput)?;
    if address.is_unspecified() || address.is_multicast() || address.is_broadcast() {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    Ok(Some(Session {
        pid: Some(ffi::spawn()?),
    }))
}

impl Session {
    pub fn stop(mut self) -> io::Result<()> {
        self.join()
    }

    fn join(&mut self) -> io::Result<()> {
        let Some(pid) = self.pid else { return Ok(()) };
        // The higher priority app can reach teardown before the child first
        // runs. Wait for this child's registration or exit, so stop cannot
        // return success while an unregistered listener is about to start.
        loop {
            if ffi::reaped(pid)? {
                self.pid = None;
                return Ok(());
            }
            if ffi::owner() == pid {
                ffi::stop()?;
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        // Keep the Wi-Fi owner and any flash maintenance on the stack until
        // the shell's native task group actually exits.
        while let Err(error) = self.join() {
            eprintln!("SSH teardown pending: {error}");
            thread::sleep(Duration::from_secs(1));
        }
    }
}
