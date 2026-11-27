use crate::teardown::{self, COMMAND};
use crate::{Error, Session, Status};
use frames::MTU;
use ppproto::{
    pppos::{PPPoS, PPPoSAction},
    Config, Phase,
};
use sim7080::{reply, Reply};
use std::fs::File;
use std::io::{self, Read, Write};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

mod ffi;
mod frames;
const DIAL: Duration = Duration::from_secs(120);
const NEGOTIATION: Duration = Duration::from_secs(30);
const RETRY_REQUEST: Duration = Duration::from_secs(3);
const ECHO_INTERVAL: Duration = Duration::from_secs(5);
const ECHO_MISSES: u8 = 3;
static OWNED: Mutex<Lease> = Mutex::new(Lease::Available);

#[derive(Clone, Copy)]
enum Lease {
    Available,
    Active,
    Faulted,
}

/// Whether this build can establish a cellular link.
pub const LINK_AVAILABLE: bool = true;

pub fn connect(apn: &str) -> Result<impl Session, Error> {
    start(apn, None)
}

/// Maintain PPP across recoverable link failures. The callback receives each
/// status transition in task context and must complete without waiting.
pub fn connect_managed(
    apn: &str,
    notify: impl Fn(Status) + Send + Sync + 'static,
) -> Result<impl Session, Error> {
    start(apn, Some(Box::new(notify)))
}

type Notify = Box<dyn Fn(Status) + Send + Sync>;

fn start(apn: &str, notify: Option<Notify>) -> Result<Link, Error> {
    let context = sim7080::context(apn).ok_or(Error::InvalidApn)?;
    let mut lease = OWNED.lock().unwrap();
    match *lease {
        Lease::Available => (),
        Lease::Active => return Err(Error::Busy),
        Lease::Faulted => return Err(Error::RecoveryRequired),
    }
    let (stop_read, stop_write) = acquisition::nuttx::pipe().map_err(io_error)?;
    let shared = Arc::new(Shared {
        status: Mutex::new(Status::Connecting),
        stop: AtomicBool::new(false),
        notify,
    });
    let worker = thread::Builder::new()
        .name("cellular".into())
        .stack_size(15 * 1024)
        .spawn({
            let shared = shared.clone();
            move || {
                let result = run(&context, &shared, stop_read);
                shared.publish(match result {
                    Ok(()) => Status::Stopped,
                    Err(error) => Status::Failed(error),
                });
                *OWNED.lock().unwrap() = if result == Err(Error::RecoveryRequired) {
                    Lease::Faulted
                } else {
                    Lease::Available
                };
                result
            }
        })
        .map_err(io_error)?;
    *lease = Lease::Active;
    Ok(Link {
        shared,
        stop: Some(stop_write),
        worker: Some(worker),
        stopped: None,
    })
}

struct Shared {
    status: Mutex<Status>,
    stop: AtomicBool,
    notify: Option<Notify>,
}

impl Shared {
    fn publish(&self, status: Status) {
        let previous = std::mem::replace(&mut *self.status.lock().unwrap(), status);
        if previous != status {
            if let Some(notify) = &self.notify {
                notify(status);
            }
        }
    }

    fn failed(&self, error: Error) {
        self.publish(
            if self.notify.is_some() && error != Error::RecoveryRequired {
                Status::Retrying(error)
            } else {
                Status::Failed(error)
            },
        );
    }
}

struct Link {
    shared: Arc<Shared>,
    stop: Option<File>,
    worker: Option<JoinHandle<Result<(), Error>>>,
    stopped: Option<Result<(), Error>>,
}

impl Session for Link {
    fn status(&self) -> Status {
        *self.shared.status.lock().unwrap()
    }

    fn request_stop(&mut self) {
        self.shared.stop.store(true, Ordering::Release);
        // Closing the writer wakes poll on the reader with POLLHUP.
        drop(self.stop.take());
    }

    fn is_finished(&self) -> bool {
        self.worker.as_ref().is_none_or(JoinHandle::is_finished)
    }

    fn join(&mut self) -> Result<(), Error> {
        if let Some(worker) = self.worker.take() {
            self.stopped = Some(worker.join().unwrap_or_else(|_| {
                *OWNED.lock().unwrap() = Lease::Faulted;
                self.shared.publish(Status::Failed(Error::RecoveryRequired));
                Err(Error::RecoveryRequired)
            }));
        }
        self.stopped.unwrap_or(Ok(()))
    }
}

impl Drop for Link {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn run(context: &str, shared: &Shared, stop: File) -> Result<(), Error> {
    let mut delay = Duration::from_secs(1);
    loop {
        let mut online = false;
        let result = attempt(context, shared, &stop, &mut online);
        let Err(error) = result else { return result };
        if shared.notify.is_none() || error == Error::RecoveryRequired {
            return result;
        }
        if shared.stop.load(Ordering::Acquire) {
            return Ok(());
        }
        shared.failed(error);
        if online {
            delay = Duration::from_secs(1);
        }
        let retry_at = Instant::now() + delay;
        while !shared.stop.load(Ordering::Acquire) {
            let remaining = retry_at.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            ffi::wait(
                None,
                None,
                Some(&stop),
                None,
                remaining.as_millis().max(1) as i32,
            )
            .map_err(io_error)?;
        }
        if shared.stop.load(Ordering::Acquire) {
            return Ok(());
        }
        delay = (delay * 2).min(Duration::from_secs(30));
        shared.publish(Status::Connecting);
    }
}

fn attempt(context: &str, shared: &Shared, stop: &File, online: &mut bool) -> Result<(), Error> {
    let mut uart = ffi::open(env!("CELLULAR_DEVICE")).map_err(io_error)?;
    ffi::raw(&uart).map_err(io_error)?;
    let mut modem = Modem {
        uart: &mut uart,
        cancel: Some((&shared.stop, stop)),
    };
    let mut dialed = false;
    let result = (|| {
        modem.command(b"AT\r", Reply::Ok, COMMAND)?;
        modem.command(b"ATE0\r", Reply::Ok, COMMAND)?;
        modem.command(context.as_bytes(), Reply::Ok, COMMAND)?;
        let response = modem.command(sim7080::DIAL, Reply::Connect, DIAL);
        dialed = response != Err(Error::Rejected);
        response?;
        modem.network(shared, online)
    })();
    // A stop after a published fault joins teardown and retains that fault.
    let result = if shared.stop.load(Ordering::Acquire) {
        Ok(())
    } else {
        result
    };
    if let Err(error) = result {
        shared.failed(error);
    }
    if dialed {
        modem.cancel = None;
        // SIM7080G uses guarded escape to leave online data mode on this
        // two-wire UART. Do not release the modem until AT mode is verified.
        teardown::command_mode(&mut modem)?;
    }
    result
}

struct Modem<'a> {
    uart: &'a mut File,
    cancel: Option<(&'a AtomicBool, &'a File)>,
}

impl teardown::Control for Modem<'_> {
    fn now(&self) -> Instant {
        Instant::now()
    }
    fn pause(&mut self, duration: Duration) {
        thread::sleep(duration);
    }
    fn write(&mut self, bytes: &[u8], deadline: Instant) -> Result<(), Error> {
        self.write(bytes, deadline)
    }
    fn ok(&mut self, deadline: Instant) -> Result<(), Error> {
        self.response(Reply::Ok, deadline)
    }
}

impl Modem<'_> {
    fn cancelled(&self) -> bool {
        self.cancel
            .is_some_and(|(flag, _)| flag.load(Ordering::Acquire))
    }

    fn wait(
        &self,
        tun: Option<&File>,
        write: Option<&File>,
        deadline: Instant,
    ) -> Result<(), Error> {
        if self.cancelled() {
            return Err(Error::LinkLost);
        }
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(Error::Timeout);
        }
        let ms = left.as_millis().clamp(1, i32::MAX as u128) as i32;
        ffi::wait(
            write.is_none().then_some(&*self.uart),
            tun,
            self.cancel.map(|(_, fd)| fd),
            write,
            ms,
        )
        .map_err(io_error)?;
        if self.cancelled() {
            return Err(Error::LinkLost);
        }
        // Readiness is only a wake hint. A delayed scheduling return must not
        // admit another UART operation beyond the caller's absolute deadline.
        if Instant::now() >= deadline {
            return Err(Error::Timeout);
        }
        Ok(())
    }

    fn write(&mut self, mut bytes: &[u8], deadline: Instant) -> Result<(), Error> {
        while !bytes.is_empty() {
            self.wait(None, Some(self.uart), deadline)?;
            match self.uart.write(bytes) {
                Ok(0) => return Err(Error::Io),
                Ok(count) => bytes = &bytes[count..],
                Err(error) if retry(&error) => (),
                Err(_) => return Err(Error::Io),
            }
        }
        Ok(())
    }

    fn command(&mut self, bytes: &[u8], expected: Reply, timeout: Duration) -> Result<(), Error> {
        let deadline = Instant::now() + timeout;
        self.write(bytes, deadline)?;
        self.response(expected, deadline)
    }

    fn response(&mut self, expected: Reply, deadline: Instant) -> Result<(), Error> {
        let mut line = [0; 192];
        let mut len = 0;
        loop {
            if self.cancelled() {
                return Err(Error::LinkLost);
            }
            if Instant::now() >= deadline {
                return Err(Error::Timeout);
            }
            let mut byte = [0];
            match self.uart.read(&mut byte) {
                Ok(1) => (),
                Ok(_) => return Err(Error::LinkLost),
                Err(error) if retry(&error) => {
                    self.wait(None, None, deadline)?;
                    continue;
                }
                Err(_) => return Err(Error::Io),
            }
            match byte[0] {
                b'\r' | b'\n' => {
                    if let Some(line) = line.get(..len) {
                        let reply = reply(line);
                        if reply == expected {
                            return Ok(());
                        }
                        if reply == Reply::Rejected {
                            return Err(Error::Rejected);
                        }
                    }
                    len = 0;
                }
                byte if len < line.len() => {
                    line[len] = byte;
                    len += 1;
                }
                _ => len = line.len() + 1,
            }
        }
    }

    fn network(&mut self, shared: &Shared, online: &mut bool) -> Result<(), Error> {
        let mut tun = ffi::open("/dev/tun").map_err(io_error)?;
        let interface = ffi::Interface::create(&tun).map_err(io_error)?;
        let mut ppp = PPPoS::new(Config {
            username: b"",
            password: b"",
        });
        ppp.open().map_err(|_| Error::Io)?;
        let mut rx = [0; MTU + 5];
        let mut tx = [0; frames::MAX_ENCODED];
        let mut input = [0; 512];
        let mut packet = [0; MTU];
        let mut frames = frames::Frames::default();
        let deadline = Instant::now() + NEGOTIATION;
        let mut echo_at = deadline;
        let mut echo_id = 0u8;
        let mut unanswered = 0;
        let mut request = PendingRequest {
            bytes: [0; 128],
            len: 0,
            retry_at: deadline,
        };
        if let PPPoSAction::Transmit(count) = ppp.poll(&mut tx, &mut rx) {
            request.record(&tx[..count])?;
            self.write(&tx[..count], Instant::now() + COMMAND)?;
        }
        loop {
            let now = Instant::now();
            let until = if *online {
                if now >= echo_at {
                    if unanswered == ECHO_MISSES {
                        return Err(Error::LinkLost);
                    }
                    echo_id = echo_id.wrapping_add(1);
                    let count = ppp
                        .echo_request(echo_id, &mut tx)
                        .map_err(|_| Error::Io)?
                        .ok_or(Error::LinkLost)?;
                    self.write(&tx[..count], now + COMMAND)?;
                    unanswered += 1;
                    echo_at = now + ECHO_INTERVAL;
                }
                echo_at
            } else {
                if now >= deadline {
                    return Err(Error::Timeout);
                }
                if now >= request.retry_at {
                    self.write(&request.bytes[..request.len], deadline.min(now + COMMAND))?;
                    request.retry_at = Instant::now() + RETRY_REQUEST;
                }
                deadline.min(request.retry_at)
            };
            match self.wait((*online).then_some(&tun), None, until) {
                Err(Error::Timeout) => continue,
                result => result?,
            }
            if let Some(bytes) = read_ready(self.uart, &mut input)? {
                for &byte in bytes {
                    if let Some(frame) = frames.push(byte)? {
                        ppp.consume(frame, &mut rx);
                        // At this boundary only one complete frame is available.
                        match ppp.poll(&mut tx, &mut rx) {
                            PPPoSAction::Transmit(count) => {
                                if !*online {
                                    request.record(&tx[..count])?;
                                }
                                self.write(&tx[..count], Instant::now() + COMMAND)?
                            }
                            PPPoSAction::Received(range) if *online => {
                                match tun.write(&rx[range.clone()]) {
                                    Ok(written) if written == range.len() => (),
                                    // TUN is packet-oriented. Drop on backpressure.
                                    Err(error) if retry(&error) => (),
                                    _ => return Err(Error::Io),
                                }
                            }
                            PPPoSAction::EchoReply(id) if *online && id == echo_id => {
                                unanswered = 0;
                            }
                            _ => (),
                        }
                    }
                }
            }
            let status = ppp.status();
            if status.phase == Phase::Dead || (*online && status.ipv4.is_none()) {
                return Err(Error::LinkLost);
            }
            if !*online && status.phase == Phase::Open {
                let ipv4 = status.ipv4.ok_or(Error::Rejected)?;
                let address = ipv4.address.ok_or(Error::Rejected)?;
                // Some peers omit their point-to-point address. The NuttX
                // TUN interface still carries IPv4 datagrams without ARP.
                interface
                    .bring_up(address, ipv4.peer_address.unwrap_or(address))
                    .map_err(io_error)?;
                *online = true;
                echo_at = Instant::now() + ECHO_INTERVAL;
                shared.publish(Status::Online { address });
            }
            if *online {
                if let Some(bytes) = read_ready(&mut tun, &mut packet)? {
                    let count = ppp.send(bytes, &mut tx).map_err(|_| Error::Io)?;
                    self.write(&tx[..count], Instant::now() + COMMAND)?;
                }
            }
        }
    }
}

// `ppproto` drives framing and negotiation but leaves retry timing to I/O.
// Retain one originating control request, never a burst of peer responses.
struct PendingRequest {
    bytes: [u8; 128],
    len: usize,
    retry_at: Instant,
}

impl PendingRequest {
    fn record(&mut self, burst: &[u8]) -> Result<(), Error> {
        if let Some(request) = frames::last_request(burst) {
            self.bytes
                .get_mut(..request.len())
                .ok_or(Error::Io)?
                .copy_from_slice(request);
            self.len = request.len();
            self.retry_at = Instant::now() + RETRY_REQUEST;
        }
        Ok(())
    }
}

fn retry(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
    )
}

fn read_ready<'a>(file: &mut File, buffer: &'a mut [u8]) -> Result<Option<&'a [u8]>, Error> {
    match file.read(buffer) {
        Ok(0) => Err(Error::LinkLost),
        Ok(count) => Ok(Some(&buffer[..count])),
        Err(error) if retry(&error) => Ok(None),
        Err(_) => Err(Error::Io),
    }
}

fn io_error(_: io::Error) -> Error {
    Error::Io
}
