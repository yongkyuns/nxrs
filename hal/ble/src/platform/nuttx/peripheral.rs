use crate::peripheral::{Link, Listener, Service};
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
const NOTIFY_CHUNK: usize = 244;
const OFFSET_BYTES: usize = 4;
const REPLAY_AFTER: Duration = Duration::from_millis(180);
const HANDLE_MASK: u32 = 0x0fff;
const CONNECTED: u32 = 1 << 12;
const RETIRED: u32 = 1 << 13;
const GENERATION: u32 = 1 << 14;
static NOTIFY: AtomicBool = AtomicBool::new(false);
static OPEN: AtomicBool = AtomicBool::new(false);
static REGISTERED: Mutex<Option<(Service, Option<Service>)>> = Mutex::new(None);
// One atomic tracks the HCI handle, its generation and the link state.
static STAMP: AtomicU32 = AtomicU32::new(0);
static ACKNOWLEDGED: AtomicU32 = AtomicU32::new(0);
static INPUT: Mutex<Queue> = Mutex::new(Queue::new());
static OUTPUT: Mutex<Output> = Mutex::new(Output {
    queue: Queue::new(),
    offset: 0,
    next: 0,
    sent: 0,
    replay_at: None,
});

struct Queue {
    stamp: u32,
    bytes: VecDeque<u8>,
}

impl Queue {
    const fn new() -> Self {
        Self {
            stamp: 0,
            bytes: VecDeque::new(),
        }
    }

    fn select(&mut self, stamp: u32) -> bool {
        if self.stamp == stamp {
            return false;
        }
        self.stamp = stamp;
        self.bytes.clear();
        true
    }
}

struct Output {
    queue: Queue,
    offset: u32,
    next: usize,
    sent: usize,
    replay_at: Option<Instant>,
}

impl Output {
    fn acknowledge(&mut self, offset: u32, now: Instant) -> io::Result<()> {
        let count = offset.wrapping_sub(self.offset);
        if count == 0 || count > i32::MAX as u32 {
            return Ok(());
        }
        let count = count as usize;
        if count > self.sent {
            return Err(io::ErrorKind::InvalidData.into());
        }
        self.queue.bytes.drain(..count);
        self.offset = offset;
        self.sent -= count;
        self.next = self.next.saturating_sub(count);
        self.replay_at = (self.sent != 0).then_some(now + REPLAY_AFTER);
        Ok(())
    }
}

fn validate(stamp: u32) -> io::Result<()> {
    if !OPEN.load(Ordering::Acquire)
        || stamp & (CONNECTED | RETIRED) != CONNECTED
        || stamp != STAMP.load(Ordering::Acquire)
    {
        return Err(io::ErrorKind::NotConnected.into());
    }
    Ok(())
}

fn input(stamp: u32) -> io::Result<MutexGuard<'static, Queue>> {
    // ATT writes run on NuttX's HCI receive worker. This lock protects only
    // bounded byte copies and is never held across an SDK call or I/O.
    let mut queue = INPUT.lock().map_err(|_| io::ErrorKind::BrokenPipe)?;
    validate(stamp)?;
    queue.select(stamp);
    Ok(queue)
}

fn output(stamp: u32) -> io::Result<MutexGuard<'static, Output>> {
    let mut output = OUTPUT.try_lock().map_err(|error| match error {
        std::sync::TryLockError::WouldBlock => io::ErrorKind::WouldBlock,
        std::sync::TryLockError::Poisoned(_) => io::ErrorKind::BrokenPipe,
    })?;
    validate(stamp)?;
    if output.queue.select(stamp) {
        output.offset = 0;
        output.next = 0;
        output.sent = 0;
        output.replay_at = None;
    }
    Ok(output)
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
    NOTIFY.store(false, Ordering::Release);
    let stamp = STAMP.load(Ordering::Relaxed);
    if connected {
        ACKNOWLEDGED.store(0, Ordering::Relaxed);
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
extern "C" fn peripheral_notify_rust(enabled: bool) {
    NOTIFY.store(enabled, Ordering::Release);
}

#[no_mangle]
extern "C" fn peripheral_notifications_rust() -> bool {
    NOTIFY.load(Ordering::Acquire)
}

#[no_mangle]
unsafe extern "C" fn peripheral_read_rust(destination: *mut u8, count: u8) -> i32 {
    if count == 0 || NOTIFY.load(Ordering::Acquire) {
        return 0;
    }
    let mut data = match output(STAMP.load(Ordering::Acquire)) {
        Ok(data) => data,
        // An empty reply lets the central poll again without consuming bytes
        // or treating a preempted publisher as a broken connection.
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => return 0,
        Err(_) => return -1,
    };
    // The GATT SDK owns count writable bytes until this callback returns.
    let bytes =
        unsafe { std::slice::from_raw_parts_mut(destination, usize::from(count).min(ATT_CHUNK)) };
    pop(&mut data.queue.bytes, bytes) as i32
}

#[no_mangle]
unsafe extern "C" fn peripheral_write_rust(source: *const u8, count: u8, framed: bool) -> i32 {
    let count = usize::from(count);
    if count == 0 || count > ATT_CHUNK {
        return -1;
    }
    let stamp = STAMP.load(Ordering::Acquire);
    // SDK storage is valid for this synchronous callback only.
    let mut bytes = unsafe { std::slice::from_raw_parts(source, count) };
    if framed {
        if !NOTIFY.load(Ordering::Acquire) || validate(stamp).is_err() {
            return -1;
        }
        match bytes {
            [0, rest @ ..] if !rest.is_empty() => bytes = rest,
            [1, a, b, c, d] => {
                ACKNOWLEDGED.store(u32::from_le_bytes([*a, *b, *c, *d]), Ordering::Release);
                return count as i32;
            }
            _ => return -1,
        }
    }
    let Ok(mut data) = input(stamp) else {
        return -1;
    };
    if bytes.len() > CAPACITY - data.bytes.len() {
        return -1;
    }
    data.bytes.extend(bytes);
    count as i32
}

struct Peripheral {
    accepted: u32,
    advertised: u32,
    retry_advertise: Instant,
}

pub(crate) fn open_peripheral(
    legacy: Service,
    notifications: Option<Service>,
) -> Result<impl Listener, Error> {
    if unsafe { ffi::peripheral_max_connections } != 1 {
        return Err(Error::Unsupported);
    }
    let uuids = |service: Service| [service.uuid, service.ingress, service.egress];
    let mut ids = [Uuid([0; 16]); 6];
    ids[..3].copy_from_slice(&uuids(legacy));
    let count = if let Some(service) = notifications {
        ids[3..].copy_from_slice(&uuids(service));
        6
    } else {
        3
    };
    for (index, uuid) in ids[..count].iter().enumerate() {
        if ids[..index].contains(uuid) {
            return Err(Error::InvalidValue);
        }
    }
    let mut registered = REGISTERED.try_lock().map_err(|_| Error::Busy)?;
    let stamp = STAMP.load(Ordering::Acquire);
    if OPEN.load(Ordering::Acquire) || stamp & CONNECTED != 0 {
        return Err(Error::Busy);
    }
    match *registered {
        Some(prior) if prior != (legacy, notifications) => return Err(Error::Unsupported),
        Some(_) => {}
        None => {
            INPUT
                .lock()
                .map_err(|_| Error::Io)?
                .bytes
                .try_reserve_exact(CAPACITY)
                .map_err(|_| Error::Io)?;
            OUTPUT
                .lock()
                .map_err(|_| Error::Io)?
                .queue
                .bytes
                .try_reserve_exact(CAPACITY)
                .map_err(|_| Error::Io)?;
            let little_endian = ids.map(|Uuid(mut bytes)| {
                bytes.reverse();
                bytes
            });
            // The registration copies UUIDs into the SDK's static GATT table.
            unsafe {
                ffi::peripheral_register(little_endian.as_ptr().cast(), notifications.is_some())
            };
            *registered = Some((legacy, notifications));
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

    fn notifying(&self) -> bool {
        peripheral_notifications_rust()
    }

    fn service(&mut self) -> io::Result<()> {
        let stamp = STAMP.load(Ordering::Acquire);
        // Let accept() retire a failed link even if its notification
        // subscription is still enabled.
        if stamp & (CONNECTED | RETIRED) != CONNECTED || !self.notifying() {
            return Ok(());
        }
        let Some(peer) = ffi::Peer::lookup((stamp & HANDLE_MASK) as u16) else {
            return Ok(());
        };
        let mut output = match output(stamp) {
            Ok(output) => output,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(()),
            Err(error) => return Err(error),
        };
        let now = Instant::now();
        output.acknowledge(ACKNOWLEDGED.load(Ordering::Acquire), now)?;
        if output.replay_at.is_some_and(|deadline| now >= deadline) {
            // The oldest unacknowledged bytes may have been lost after HCI
            // accepted them. Replay from the peer's last contiguous offset.
            output.next = 0;
            output.replay_at = Some(now + REPLAY_AFTER);
        }
        let mut bytes = [0; NOTIFY_CHUNK];
        for _ in 0..8 {
            if !self.notifying() || output.next == output.queue.bytes.len() {
                break;
            }
            let offset = output.offset.wrapping_add(output.next as u32);
            bytes[..OFFSET_BYTES].copy_from_slice(&offset.to_le_bytes());
            let len = (NOTIFY_CHUNK - OFFSET_BYTES).min(output.queue.bytes.len() - output.next);
            for (out, value) in bytes[OFFSET_BYTES..OFFSET_BYTES + len]
                .iter_mut()
                .zip(output.queue.bytes.iter().skip(output.next))
            {
                *out = *value;
            }
            let sent = peer.notify(&bytes[..OFFSET_BYTES + len])?;
            if sent == 0 {
                break;
            }
            if sent <= OFFSET_BYTES {
                return Err(io::ErrorKind::InvalidData.into());
            }
            output.next += sent - OFFSET_BYTES;
            output.sent = output.sent.max(output.next);
            output.replay_at.get_or_insert(now + REPLAY_AFTER);
        }
        Ok(())
    }

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
        // Keep the HCI callback's wait bounded even for large TLS reads.
        let len = bytes.len().min(4 * ATT_CHUNK);
        let count = pop(&mut input(self.stamp)?.bytes, &mut bytes[..len]);
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
        let mut data = output(self.stamp)?;
        let count = bytes.len().min(CAPACITY - data.queue.bytes.len());
        if count == 0 {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        data.queue.bytes.extend(&bytes[..count]);
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        if output(self.stamp)?.queue.bytes.is_empty() {
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
