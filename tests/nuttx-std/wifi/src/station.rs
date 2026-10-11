//! Hardware qualification only: runtime credentials, DHCP, exact UDP echoes,
//! then explicit disconnect and worker join. Not connectivity-service policy.
use crossbeam_channel::{bounded, Sender, TrySendError};
use std::net::{SocketAddrV4, UdpSocket};
use std::time::{Duration, Instant};
use wifi::{
    Command, Completion, Credentials, DeliveryError, Error, Event, EventSink, LinkState,
    Passphrase, ProviderStatus, Rejected, Ssid, Update,
};

unsafe extern "C" {
    fn nxrs_wifi_test_credentials(
        ssid: *mut u8,
        password: *mut u8,
        ssid_length: *mut u32,
        password_length: *mut u32,
    ) -> i32;
}

struct Sink(Sender<Event>);
impl EventSink<Event> for Sink {
    fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
        self.0.try_send(event).map_err(|error| match error {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        })
    }
}

fn credentials() -> Result<Command, Error> {
    let mut ssid = [0; 32];
    let mut password = [0; 63];
    let (mut ssid_length, mut password_length) = (0, 0);
    // SAFETY: writable arrays/scalars match the fixed-width C ABI. The helper
    // disables console echo before reading and retains no pointers.
    let status = unsafe {
        nxrs_wifi_test_credentials(
            ssid.as_mut_ptr(),
            password.as_mut_ptr(),
            &mut ssid_length,
            &mut password_length,
        )
    };
    let result = (|| {
        if status != 0 {
            return Err(Error::Io);
        }
        let ssid = Ssid::new(ssid.get(..ssid_length as usize).ok_or(Error::Protocol)?)?;
        let bytes = password
            .get(..password_length as usize)
            .ok_or(Error::Protocol)?;
        let credentials = if bytes.is_empty() {
            Credentials::Open
        } else {
            Credentials::Wpa2Personal(Passphrase::new(
                std::str::from_utf8(bytes).map_err(|_| Error::InvalidConfig)?,
            )?)
        };
        Ok(Command::Connect { ssid, credentials })
    })();
    // Best-effort clearing of the fixture input copy; the native driver still
    // needs credentials in RAM. This is not a claim of whole-stack zeroization.
    password.fill(0);
    result
}

pub(super) fn run(peer: SocketAddrV4) -> Result<(), Error> {
    if peer.ip().is_unspecified() || peer.ip().is_multicast() || peer.port() == 0 {
        return Err(Error::InvalidConfig);
    }
    let command = credentials()?;
    let (tx, rx) = bounded(8);
    let mut controller = wifi::open(Sink(tx))?;
    let start = Instant::now();
    let clock = || u64::try_from(start.elapsed().as_millis()).unwrap();
    let wait =
        |controller: &mut wifi::Controller<_>, expected: Option<Completion>| -> Result<(), Error> {
            let deadline = Instant::now() + Duration::from_secs(30);
            loop {
                controller.reconcile().map_err(|_| Error::Protocol)?;
                if let ProviderStatus::Failed(error) = controller.status() {
                    return Err(error);
                }
                if expected.is_none() && controller.status() == ProviderStatus::Ready {
                    return Ok(());
                }
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err(Error::Timeout);
                }
                let event = rx.recv_timeout(remaining).map_err(|_| Error::Timeout)?;
                let update = controller
                    .on_event(event, clock())
                    .map_err(|_| Error::Protocol)?;
                let timeout = controller.advance(clock()).map_err(|_| Error::Protocol)?;
                if let Update::Finished(outcome) = timeout {
                    return outcome.result.map(|_| ());
                }
                if let Update::Finished(outcome) = update {
                    return if Some(outcome.result?) == expected {
                        Ok(())
                    } else {
                        Err(Error::Protocol)
                    };
                }
            }
        };
    let result = (|| {
        wait(&mut controller, None)?;
        controller
            .submit(command, clock(), 25_000)
            .map_err(|rejected| match rejected.error {
                wifi::ControllerError::Device(error) => error,
                _ => Error::Protocol,
            })?;
        wait(&mut controller, Some(Completion::Connected))?;
        if controller.link() != LinkState::Addressed {
            return Err(Error::Protocol);
        }
        println!("NXRS_WIFI_ASSOCIATED_DHCP");
        let traffic = echo(peer);
        // Even failed traffic must explicitly disconnect, not merely stop callbacks.
        controller
            .submit(Command::Disconnect, clock(), 5_000)
            .map_err(|_| Error::Protocol)?;
        wait(&mut controller, Some(Completion::Disconnected))?;
        if controller.link() != LinkState::Disconnected {
            return Err(Error::Protocol);
        }
        traffic
    })();
    controller.request_stop().map_err(|_| Error::Io)?;
    // Native connection/DHCP waits are bounded but not preemptible. Drain before
    // join, even after an operation/traffic failure. No false PASS on cleanup.
    let deadline = Instant::now() + Duration::from_secs(30);
    while controller.status() != ProviderStatus::Stopped {
        controller.reconcile().map_err(|_| Error::Protocol)?;
        if controller.status() == ProviderStatus::Stopped {
            break;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(Error::Timeout);
        }
        let event = rx.recv_timeout(remaining).map_err(|_| Error::Io)?;
        controller
            .on_event(event, clock())
            .map_err(|_| Error::Protocol)?;
    }
    controller.finish().map_err(|_| Error::Io)?;
    result?;
    println!("NXRS_WIFI_STATION_PASS echoes=20 disconnect=confirmed shutdown=joined");
    Ok(())
}

fn echo(peer: SocketAddrV4) -> Result<(), Error> {
    let socket = UdpSocket::bind("0.0.0.0:0").map_err(|_| Error::Io)?;
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|_| Error::Io)?;
    socket
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|_| Error::Io)?;
    socket.connect(peer).map_err(|_| Error::Io)?;
    let mut sent = [0; 512];
    let mut received = [0; 513];
    for sequence in 0u32..20 {
        let size = [16, 64, 256, 512][sequence as usize % 4];
        sent[..4].copy_from_slice(&sequence.to_be_bytes());
        for (offset, byte) in sent[4..size].iter_mut().enumerate() {
            *byte = (offset as u8).wrapping_add(sequence as u8);
        }
        if socket.send(&sent[..size]).map_err(|error| {
            eprintln!(
                "NXRS_WIFI_TRAFFIC_FAIL phase=send sequence={sequence} kind={:?}",
                error.kind()
            );
            Error::Io
        })? != size
        {
            return Err(Error::Protocol);
        }
        let length = socket.recv(&mut received).map_err(|error| {
            eprintln!(
                "NXRS_WIFI_TRAFFIC_FAIL phase=receive sequence={sequence} kind={:?}",
                error.kind()
            );
            Error::Timeout
        })?;
        if received[..length] != sent[..size] {
            return Err(Error::Protocol);
        }
    }
    Ok(())
}
