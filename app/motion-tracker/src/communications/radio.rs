use super::{
    export::{Capture, Version, MAX_BYTES, REQUEST_LEN, REQUEST_PREFIX},
    publication::Publisher,
    tiles::{Key, Receiver, Response},
    wire,
};
use crate::{text::Text, tracker::Snapshot};
use ble::{
    peripheral::{self, DeliveryProgress, Link, Listener, Service},
    Uuid,
};
use std::io::{self, Read, Write};
use std::time::{Duration, Instant};

// The supported phone ends an authenticated stream after twelve seconds of
// silence. Bound outstanding delivery here too, without treating a nonempty
// queue or a replay as proof that the peer has stopped. TLS owns its separate
// authentication deadline.
const DELIVERY_SILENCE: Duration = Duration::from_secs(12);

fn admit_capture(capture: &Capture, version: Version) -> io::Result<()> {
    match capture.enable() {
        // A retained source failure keeps sample admission closed. Evidence
        // peers may still receive its typed terminal result; legacy peers
        // cannot represent it, and other startup errors remain errors.
        Err(_) if matches!(version, Version::Evidence) && capture.is_failed() => Ok(()),
        result => result,
    }
}

#[derive(Default)]
struct DeliveryWatch {
    previous: Option<DeliveryProgress>,
    waiting_since: Option<Instant>,
}

impl DeliveryWatch {
    fn deadline(&self) -> Option<Instant> {
        self.waiting_since.map(|since| since + DELIVERY_SILENCE)
    }

    fn observe(
        &mut self,
        now: Instant,
        progress: Option<DeliveryProgress>,
        local_pending: bool,
        locally_written: bool,
    ) -> io::Result<()> {
        let (pending, advanced) = match progress {
            Some(current) => {
                let advanced = self.previous.is_none_or(|previous| {
                    current.generation != previous.generation
                        || current.completed != previous.completed
                });
                self.previous = Some(current);
                // Local acceptance cannot extend a peer-ACK deadline while the
                // provider retains undelivered output. It can show progress of
                // a partial TLS write before output reaches the provider.
                (
                    current.pending || local_pending,
                    advanced || (!current.pending && locally_written),
                )
            }
            None => {
                self.previous = None;
                // Unknown providers can bound a stalled local frame, but cannot
                // claim that successful writes acknowledge the remote peer.
                (local_pending, locally_written)
            }
        };
        self.wait(now, pending, advanced)
    }

    fn unavailable(
        &mut self,
        now: Instant,
        local_pending: bool,
        locally_written: bool,
    ) -> io::Result<()> {
        // Snapshot contention is not an empty queue. Keep the outstanding
        // obligation until a coherent snapshot can confirm progress or idle.
        self.wait(
            now,
            self.waiting_since.is_some() || local_pending || locally_written,
            self.previous.is_none() && locally_written,
        )
    }

    fn wait(&mut self, now: Instant, pending: bool, advanced: bool) -> io::Result<()> {
        if !pending {
            self.waiting_since = None;
        } else if advanced || self.waiting_since.is_none() {
            self.waiting_since = Some(now);
        } else if self.deadline().is_some_and(|deadline| now >= deadline) {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "BLE delivery made no observed progress",
            ));
        }
        Ok(())
    }
}

struct TrackedWrite<'a, W> {
    output: &'a mut W,
    advanced: &'a mut bool,
}

impl<'a, W> TrackedWrite<'a, W> {
    fn new(output: &'a mut W, advanced: &'a mut bool) -> Self {
        Self { output, advanced }
    }
}

impl<W: Write> Write for TrackedWrite<'_, W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let count = self.output.write(bytes)?;
        *self.advanced |= count != 0;
        Ok(count)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.output.flush()
    }
}

fn uuid(last: u8) -> Uuid {
    Uuid([
        0x3c, 0xb1, 0xde, 0x6e, 0xa9, 0x55, 0x49, 0xb7, 0xbe, 0x4f, 0xc4, 0x57, 0x2d, 0x3f, 0x34,
        last,
    ])
}

pub fn pair<'a>(
    identity: &'a tls::Identity,
    capture: &'a Capture,
) -> io::Result<Output<'a, impl Listener>> {
    let service = |base| Service {
        uuid: uuid(base),
        ingress: uuid(base + 1),
        egress: uuid(base + 2),
    };
    let listener = peripheral::open_with_notifications(service(0), service(0x20))
        .map_err(|error| io::Error::other(format!("BLE: {error:?}")))?;
    Output::start(listener, identity, capture)
}

struct Bearer<T>(T);
impl<T: Link> Read for Bearer<T> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0.read(buf)
    }
}
impl<T: Link> Write for Bearer<T> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0.flush()
    }
}
impl<T: Link> tls::Stream for Bearer<T> {
    fn prepare(&mut self) -> io::Result<()> {
        Ok(())
    }
    fn abort(&mut self) {
        self.0.abort();
    }
}

pub struct Output<'a, L: Listener> {
    // Fields leave scope in declaration order. Retire TLS before its listener.
    connection: Option<tls::Connection<'a, Bearer<L::Transport>>>,
    listener: L,
    identity: &'a tls::Identity,
    key: [u8; 32],
    capture: &'a Capture,
    tile: Receiver,
    advertised_tile: Option<Key>,
    request: [u8; REQUEST_LEN],
    request_len: usize,
    export: [u8; MAX_BYTES],
    export_len: usize,
    sent: usize,
    next_export: Option<(Version, Instant)>,
    last_export_service: Option<Instant>,
    max_service_gap: Duration,
    publication: Publisher<Snapshot, Text<768>>,
    delivery: DeliveryWatch,
    live: bool,
    paired: bool,
    drained: bool,
    next_frame: Instant,
    expires: Instant,
}

impl<'a, L: Listener> Output<'a, L> {
    pub fn start(
        listener: L,
        identity: &'a tls::Identity,
        capture: &'a Capture,
    ) -> io::Result<Self> {
        let key = identity
            .pairing_key()
            .map_err(|error| io::Error::other(format!("Pairing key: {error:?}")))?;
        let now = Instant::now();
        Ok(Self {
            listener,
            identity,
            key,
            capture,
            tile: Receiver::default(),
            advertised_tile: None,
            request: [0; REQUEST_LEN],
            request_len: 0,
            export: [0; MAX_BYTES],
            export_len: 0,
            sent: 0,
            next_export: None,
            last_export_service: None,
            max_service_gap: Duration::ZERO,
            connection: None,
            publication: Publisher::default(),
            delivery: DeliveryWatch::default(),
            live: false,
            paired: false,
            drained: false,
            next_frame: now,
            expires: now + Duration::from_secs(3600),
        })
    }
    pub fn key(&self) -> &[u8; 32] {
        &self.key
    }
    pub fn live(&self) -> bool {
        self.live
    }
    pub fn paired(&self) -> bool {
        self.paired
    }

    pub fn show_pairing_code(&mut self) {
        if !self.live {
            self.paired = false;
        }
    }

    pub fn expired(&self, now: Instant) -> bool {
        now >= self.expires
    }

    pub fn active(&self) -> bool {
        self.connection.is_some()
    }

    pub fn next_service(&self, active_at: Instant) -> Instant {
        let wake = if self.active() {
            active_at
        } else {
            self.listener.idle_retry_at().unwrap_or(self.expires)
        };
        let wake = self
            .delivery
            .deadline()
            .map_or(wake, |deadline| wake.min(deadline));
        wake.min(self.expires)
    }

    pub fn take_ready(&self) -> bool {
        self.listener.take_ready()
    }

    pub fn service(&mut self, now: Instant, wanted: Option<Key>) -> Option<Response> {
        self.tile.retain(wanted);
        if let Some(last) = self.last_export_service.as_mut() {
            self.max_service_gap = self.max_service_gap.max(now.duration_since(*last));
            *last = now;
        }
        let mut locally_written = false;
        let result = self
            .poll(now, wanted, &mut locally_written)
            .and_then(|response| {
                if self.live {
                    let pending = self.export_len != 0
                        || self.publication.in_flight()
                        || (!self.listener.notifying() && !self.drained);
                    match self.listener.delivery_progress() {
                        Ok(progress) => {
                            self.delivery
                                .observe(now, progress, pending, locally_written)?
                        }
                        Err(error)
                            if matches!(
                                error.kind(),
                                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                            ) =>
                        {
                            self.delivery.unavailable(now, pending, locally_written)?;
                        }
                        Err(error) => return Err(error),
                    }
                }
                Ok(response)
            });
        let error = match result {
            Ok(response) => return response,
            Err(error) => error,
        };
        self.connection = None;
        let high_water = self.capture.finish();
        log::warn!("motion-tracker BLE session ended: {error}");
        if self.last_export_service.take().is_some() {
            log::info!(
                "motion-tracker BLE export: high_water={high_water} samples max_service_gap={} us",
                self.max_service_gap.as_micros(),
            );
        }
        self.tile = Receiver::default();
        self.advertised_tile = None;
        self.request_len = 0;
        self.export_len = 0;
        self.next_export = None;
        self.sent = 0;
        self.live = false;
        self.drained = false;
        self.publication = Publisher::default();
        self.delivery = DeliveryWatch::default();
        None
    }

    fn poll(
        &mut self,
        now: Instant,
        wanted: Option<Key>,
        locally_written: &mut bool,
    ) -> io::Result<Option<Response>> {
        let mut response = None;
        self.listener.service()?;
        if self.connection.is_none() {
            if let Some(stream) = self
                .listener
                .accept()
                .map_err(|_| io::ErrorKind::NotConnected)?
            {
                let conn = self
                    .identity
                    .accept_pairing(Bearer(stream), &self.key)
                    .map_err(|_| io::ErrorKind::PermissionDenied)?;
                self.connection = Some(conn);
            }
        }
        if let Some(conn) = self.connection.as_mut() {
            self.live = conn.poll().map_err(|_| io::ErrorKind::PermissionDenied)?;
            if self.live {
                self.paired = true;
                let notifications = self.listener.notifying();
                if notifications && self.request_len < REQUEST_LEN && self.publication.is_idle() {
                    match conn.read(&mut self.request[self.request_len..]) {
                        Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                        Ok(count) => {
                            self.request_len += count;
                            let prefix_len = self.request_len.min(REQUEST_PREFIX.len());
                            if self.request[..prefix_len] != REQUEST_PREFIX[..prefix_len] {
                                return Err(io::ErrorKind::InvalidData.into());
                            }
                            if self.request_len == REQUEST_LEN {
                                let version = Version::from_request(self.request[REQUEST_LEN - 1])
                                    .ok_or(io::ErrorKind::InvalidData)?;
                                admit_capture(self.capture, version)?;
                                self.last_export_service = Some(now);
                                self.max_service_gap = Duration::ZERO;
                                self.next_export = Some((version, now + version.batch_interval()));
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                        Err(error) => return Err(error),
                    }
                }
                if self.export_len != 0 {
                    Self::write_export(
                        &mut TrackedWrite::new(conn, locally_written),
                        &self.export[..self.export_len],
                        &mut self.sent,
                    )?;
                    if self.sent == self.export_len {
                        self.sent = 0;
                        self.export_len = 0;
                    }
                    return Ok(None);
                }
                let tile_ready = !notifications || self.request_len == REQUEST_LEN;
                let requested_tile = wanted.filter(|_| tile_ready);
                if !self.publication.is_idle() {
                    self.drained = if notifications {
                        self.publication.enqueue(
                            &mut TrackedWrite::new(conn, locally_written),
                            now,
                            self.expires,
                            |snapshot| {
                                let frame = wire::export_frame(
                                    snapshot,
                                    self.capture.scales(),
                                    requested_tile,
                                )?;
                                self.advertised_tile = requested_tile;
                                Ok(frame)
                            },
                        )?
                    } else {
                        self.publication.service(
                            &mut TrackedWrite::new(conn, locally_written),
                            now,
                            self.expires,
                            |snapshot| {
                                let frame = wire::ble_frame(snapshot, requested_tile)?;
                                self.advertised_tile = requested_tile;
                                Ok(frame)
                            },
                        )?
                    };
                    return Ok(None);
                }
                if !notifications {
                    self.drained = match conn.flush() {
                        Ok(()) => true,
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => false,
                        Err(error) => return Err(error),
                    };
                }
                // Read a bounded portion per turn, then service the sensor batch.
                if tile_ready && (notifications || self.drained) {
                    response = self.tile.poll(conn, wanted)?;
                }
                if let Some((version, deadline)) = self.next_export.filter(|_| notifications) {
                    if let Some(batch) =
                        self.capture
                            .take(&mut self.export, now >= deadline, version)
                    {
                        if batch.terminal && batch.len == 0 {
                            return Err(io::Error::new(
                                io::ErrorKind::UnexpectedEof,
                                "capture source failed",
                            ));
                        }
                        self.export_len = batch.len;
                        self.next_export =
                            (!batch.terminal).then(|| (version, now + version.batch_interval()));
                        if batch.terminal {
                            self.capture.finish();
                        }
                        Self::write_export(
                            &mut TrackedWrite::new(conn, locally_written),
                            &self.export[..self.export_len],
                            &mut self.sent,
                        )?;
                        if self.sent == self.export_len {
                            self.sent = 0;
                            self.export_len = 0;
                        }
                    }
                }
            }
        }
        Ok(response)
    }

    fn write_export(conn: &mut impl Write, bytes: &[u8], sent: &mut usize) -> io::Result<()> {
        match conn.write(&bytes[*sent..]) {
            Ok(0) => Err(io::ErrorKind::WriteZero.into()),
            Ok(count) => {
                *sent += count;
                Ok(())
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(()),
            Err(error) => Err(error),
        }
    }

    pub fn offer(&mut self, snapshot: &Snapshot, now: Instant, wanted: Option<Key>) {
        let new_tile = wanted.is_some()
            && wanted != self.advertised_tile
            && (!self.listener.notifying() || self.request_len == REQUEST_LEN);
        if self.live
            && (self.drained || self.listener.notifying())
            && self.publication.is_idle()
            && (now >= self.next_frame || new_tile)
        {
            self.publication.offer(*snapshot);
            self.next_frame = now + Duration::from_secs(1);
        }
    }
}
impl<L: Listener> Drop for Output<'_, L> {
    fn drop(&mut self) {
        self.capture.finish();
        self.key.fill(0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn delivery(generation: u32, completed: u32, pending: bool) -> Option<DeliveryProgress> {
        Some(DeliveryProgress {
            generation,
            completed,
            pending,
        })
    }

    #[test]
    fn a_preexisting_source_failure_is_reported_to_an_evidence_peer_without_reopening_capture() {
        let capture = Capture::default();
        capture.source_failed(navigation::observations::SensorFault::Imu(
            0,
            imu::DeviceError::Timeout,
        ));
        assert_eq!(
            admit_capture(&capture, Version::Timed).unwrap_err().kind(),
            io::ErrorKind::NotConnected
        );
        admit_capture(&capture, Version::Evidence).unwrap();
        let mut bytes = [0; MAX_BYTES];
        let terminal = capture.take(&mut bytes, false, Version::Evidence).unwrap();
        assert!(terminal.terminal);
        assert_eq!(&bytes[..terminal.len], &[0xa5, 4, 2, 0, 1, 3]);
        assert!(capture.enable().is_err());
    }

    #[test]
    fn advancing_acknowledgments_keep_a_nonempty_queue_alive() {
        let start = Instant::now();
        let mut watch = DeliveryWatch::default();
        for step in 0..10 {
            let now = start + Duration::from_secs(11 * u64::from(step));
            // flush remains WouldBlock and a local frame remains in flight,
            // while validated peer consumption advances every eleven seconds.
            watch
                .observe(now, delivery(1, step, true), true, false)
                .unwrap();
            assert_eq!(watch.deadline(), Some(now + DELIVERY_SILENCE));
        }
    }

    #[test]
    fn a_stuck_pending_marker_expires_despite_local_enqueues_or_missing_snapshots() {
        let start = Instant::now();
        let mut watch = DeliveryWatch::default();
        watch
            .observe(start, delivery(1, 100, true), false, true)
            .unwrap();
        watch
            .observe(
                start + Duration::from_secs(11),
                delivery(1, 100, true),
                true,
                true,
            )
            .unwrap();
        // Contention does not claim that the old outstanding output is idle.
        watch
            .unavailable(start + Duration::from_secs(11), false, false)
            .unwrap();
        assert_eq!(watch.deadline(), Some(start + DELIVERY_SILENCE));
        assert_eq!(
            watch
                .observe(start + DELIVERY_SILENCE, delivery(1, 100, true), true, true)
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn an_idle_queue_has_no_silence_deadline_and_new_work_gets_its_full_budget() {
        let start = Instant::now();
        let mut watch = DeliveryWatch::default();
        watch
            .observe(start, delivery(1, 2, true), false, false)
            .unwrap();
        watch
            .observe(
                start + Duration::from_secs(11),
                delivery(1, 3, false),
                false,
                false,
            )
            .unwrap();
        let later = start + Duration::from_secs(600);
        watch
            .observe(later, delivery(1, 3, false), false, false)
            .unwrap();
        assert_eq!(watch.deadline(), None);
        watch
            .observe(later, delivery(1, 3, true), false, false)
            .unwrap();
        assert_eq!(watch.deadline(), Some(later + DELIVERY_SILENCE));
    }

    #[test]
    fn a_new_generation_and_wrapped_progress_do_not_inherit_an_old_stall() {
        let start = Instant::now();
        let mut watch = DeliveryWatch::default();
        watch
            .observe(start, delivery(8, u32::MAX, true), false, false)
            .unwrap();
        watch
            .observe(
                start + Duration::from_secs(11),
                delivery(8, 0, true),
                false,
                false,
            )
            .unwrap();
        let replacement = start + Duration::from_secs(22);
        watch
            .observe(replacement, delivery(9, 0, true), false, false)
            .unwrap();
        assert_eq!(watch.deadline(), Some(replacement + DELIVERY_SILENCE));
        assert_eq!(
            watch
                .observe(
                    replacement + DELIVERY_SILENCE,
                    delivery(9, 0, true),
                    false,
                    false
                )
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn unknown_provider_bounds_the_same_partial_frame_using_local_write_progress() {
        #[derive(Default)]
        struct Partial {
            blocked: bool,
            bytes: Vec<u8>,
        }
        impl Write for Partial {
            fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
                if self.blocked {
                    return Err(io::ErrorKind::WouldBlock.into());
                }
                let count = bytes.len().min(2);
                self.bytes.extend_from_slice(&bytes[..count]);
                Ok(count)
            }
            fn flush(&mut self) -> io::Result<()> {
                Err(io::ErrorKind::WouldBlock.into())
            }
        }
        let start = Instant::now();
        let mut watch = DeliveryWatch::default();
        let mut publisher = Publisher::default();
        let mut peer = Partial::default();
        publisher.offer(b"abcdef" as &[u8]);
        for second in [0, 11, 22] {
            let now = start + Duration::from_secs(second);
            let mut wrote = false;
            assert!(!publisher
                .service(
                    &mut TrackedWrite::new(&mut peer, &mut wrote),
                    now,
                    start + Duration::from_secs(100),
                    |sample: &&'static [u8]| Ok(*sample),
                )
                .unwrap());
            watch
                .observe(now, None, publisher.in_flight(), wrote)
                .unwrap();
            assert_eq!(watch.deadline(), Some(now + DELIVERY_SILENCE));
        }
        assert_eq!(peer.bytes, b"abcdef");
        peer.blocked = true;
        let mut wrote = false;
        let deadline = start + Duration::from_secs(34);
        assert!(!publisher
            .service(
                &mut TrackedWrite::new(&mut peer, &mut wrote),
                deadline,
                start + Duration::from_secs(100),
                |sample: &&'static [u8]| Ok(*sample),
            )
            .unwrap());
        assert!(!wrote);
        assert_eq!(
            watch
                .observe(deadline, None, publisher.in_flight(), wrote)
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
    }
}
