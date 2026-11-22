use crate::peripheral::{Link, Listener};
use crate::{Error, Uuid};
use std::collections::VecDeque;
use std::io::{self, Read, Write};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

#[path = "ffi/peripheral.rs"]
mod ffi;

const CAPACITY: usize = 2048;
const ATT_CHUNK: usize = 20;
const HANDLE_MASK: u32 = 0x0fff;
const CONNECTED: u32 = 1 << 12;
const RETIRED: u32 = 1 << 13;
const GENERATION: u32 = 1 << 14;
static OPEN: AtomicBool = AtomicBool::new(false);
static REGISTERED: Mutex<Option<[Uuid; 3]>> = Mutex::new(None);
// One atomic tracks the HCI handle, its generation and the link state.
static STAMP: AtomicU32 = AtomicU32::new(0);
static DATA: Mutex<Buffers> = Mutex::new(Buffers {
    stamp: 0,
    input: VecDeque::new(),
    output: VecDeque::new(),
});

struct Buffers {
    stamp: u32,
    input: VecDeque<u8>,
    output: VecDeque<u8>,
}

fn current(stamp: u32) -> io::Result<MutexGuard<'static, Buffers>> {
    let mut data = DATA.try_lock().map_err(|error| match error {
        std::sync::TryLockError::WouldBlock => io::ErrorKind::WouldBlock,
        std::sync::TryLockError::Poisoned(_) => io::ErrorKind::BrokenPipe,
    })?;
    if !OPEN.load(Ordering::Acquire)
        || stamp & (CONNECTED | RETIRED) != CONNECTED
        || stamp != STAMP.load(Ordering::Acquire)
    {
        return Err(io::ErrorKind::NotConnected.into());
    }
    if data.stamp != stamp {
        data.stamp = stamp;
        data.input.clear();
        data.output.clear();
    }
    Ok(data)
}

fn pop(queue: &mut VecDeque<u8>, bytes: &mut [u8]) -> usize {
    let count = bytes.len().min(queue.len());
    for (destination, source) in bytes.iter_mut().zip(queue.drain(..count)) {
        *destination = source;
    }
    count
}

#[no_mangle]
extern "C" fn peripheral_connection_rust(connected: bool, handle: u16) {
    // The host delivers connection callbacks in order on its HCI worker.
    let stamp = STAMP.load(Ordering::Relaxed);
    if connected {
        let generation = (stamp & !(GENERATION - 1)).wrapping_add(GENERATION);
        STAMP.store(
            generation | CONNECTED | (u32::from(handle) & HANDLE_MASK),
            Ordering::Release,
        );
    } else {
        STAMP.fetch_and(!(CONNECTED | RETIRED), Ordering::Release);
    }
}

fn disconnect(stamp: u32) {
    if stamp & CONNECTED == 0 {
        return;
    }
    let handle = (stamp & HANDLE_MASK) as u16;
    if let Some(peer) = ffi::Peer::lookup(handle) {
        // Recheck after taking a native reference. That reference prevents
        // the host from reusing the connection object during the request.
        if STAMP.load(Ordering::Acquire) == stamp {
            peer.disconnect();
        }
    }
}

#[no_mangle]
unsafe extern "C" fn peripheral_read_rust(output: *mut u8, count: u8) -> i32 {
    if count == 0 {
        return 0;
    }
    let mut data = match current(STAMP.load(Ordering::Acquire)) {
        Ok(data) => data,
        // An empty reply lets the central poll again without consuming bytes
        // or treating a preempted publisher as a broken connection.
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => return 0,
        Err(_) => return -1,
    };
    // The GATT SDK owns count writable bytes until this callback returns.
    let bytes =
        unsafe { std::slice::from_raw_parts_mut(output, usize::from(count).min(ATT_CHUNK)) };
    pop(&mut data.output, bytes) as i32
}

#[no_mangle]
unsafe extern "C" fn peripheral_write_rust(input: *const u8, count: u8) -> i32 {
    let count = usize::from(count);
    if count == 0 || count > ATT_CHUNK {
        return -1;
    }
    let Ok(mut data) = current(STAMP.load(Ordering::Acquire)) else {
        return -1;
    };
    if count > CAPACITY - data.input.len() {
        return -1;
    }
    // SDK storage is valid for this synchronous callback only.
    data.input
        .extend(unsafe { std::slice::from_raw_parts(input, count) });
    count as i32
}

struct Peripheral {
    accepted: u32,
    advertised: u32,
    retry_advertise: Instant,
}

pub(crate) fn open_peripheral(
    service: Uuid,
    ingress: Uuid,
    egress: Uuid,
) -> Result<impl Listener, Error> {
    let ids = [service, ingress, egress];
    if service == ingress || service == egress || ingress == egress {
        return Err(Error::InvalidValue);
    }
    let mut registered = REGISTERED.try_lock().map_err(|_| Error::Busy)?;
    let stamp = STAMP.load(Ordering::Acquire);
    if OPEN.load(Ordering::Acquire) || stamp & CONNECTED != 0 {
        return Err(Error::Busy);
    }
    match *registered {
        Some(prior) if prior != ids => return Err(Error::Unsupported),
        Some(_) => {}
        None => {
            {
                let mut data = DATA.lock().map_err(|_| Error::Io)?;
                data.input
                    .try_reserve_exact(CAPACITY)
                    .map_err(|_| Error::Io)?;
                data.output
                    .try_reserve_exact(CAPACITY)
                    .map_err(|_| Error::Io)?;
            }
            let little_endian = ids.map(|Uuid(mut bytes)| {
                bytes.reverse();
                bytes
            });
            // The registration copies UUIDs into the SDK's static GATT table.
            unsafe { ffi::peripheral_register(little_endian.as_ptr().cast()) };
            *registered = Some(ids);
        }
    }
    OPEN.store(true, Ordering::Release);
    if unsafe { ffi::peripheral_start() } != 0 {
        OPEN.store(false, Ordering::Release);
        return Err(Error::Io);
    }
    Ok(Peripheral {
        accepted: 0,
        advertised: stamp,
        retry_advertise: Instant::now(),
    })
}

impl Listener for Peripheral {
    type Transport = Transport;

    fn accept(&mut self) -> Result<Option<Self::Transport>, Error> {
        let stamp = STAMP.load(Ordering::Acquire);
        let now = Instant::now();
        if stamp & CONNECTED == 0 {
            // The controller stops legacy advertising on connection. Re-arm
            // from the application task after the host retires that link.
            if stamp != self.advertised && now >= self.retry_advertise {
                self.retry_advertise = now + Duration::from_secs(1);
                if unsafe { ffi::peripheral_start() } != 0 {
                    return Err(Error::Io);
                }
                self.advertised = stamp;
            }
            return Ok(None);
        }
        if stamp & RETIRED != 0 {
            if now >= self.retry_advertise {
                self.retry_advertise = now + Duration::from_secs(1);
                disconnect(stamp);
            }
            return Ok(None);
        }
        if stamp == self.accepted {
            return Ok(None);
        }
        self.accepted = stamp;
        Ok(Some(Transport { stamp }))
    }
}

impl Drop for Peripheral {
    fn drop(&mut self) {
        OPEN.store(false, Ordering::Release);
        // Tables and callbacks have static lifetimes across controller reuse.
        unsafe { ffi::peripheral_stop() };
        disconnect(STAMP.load(Ordering::Acquire));
    }
}

struct Transport {
    stamp: u32,
}
impl Read for Transport {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let count = pop(&mut current(self.stamp)?.input, bytes);
        if count == 0 {
            Err(io::ErrorKind::WouldBlock.into())
        } else {
            Ok(count)
        }
    }
}
impl Write for Transport {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let mut data = current(self.stamp)?;
        let count = bytes.len().min(CAPACITY - data.output.len());
        if count == 0 {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        data.output.extend(&bytes[..count]);
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        if current(self.stamp)?.output.is_empty() {
            Ok(())
        } else {
            Err(io::ErrorKind::WouldBlock.into())
        }
    }
}
impl Link for Transport {
    fn abort(&mut self) {
        let _ = STAMP.compare_exchange(
            self.stamp,
            self.stamp | RETIRED,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
    }
}
impl Drop for Transport {
    fn drop(&mut self) {
        self.abort();
    }
}
