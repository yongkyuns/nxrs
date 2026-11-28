//! Paired BLE telemetry sessions stepped by the caller's existing owner.
//! This module has no worker, snapshot subscription or summary cadence. The
//! caller opens one identity lease, chooses UUIDs and offers protocol snapshots.
use super::{
    export::{Capture, Version, MAX_BYTES, REQUEST_LEN, REQUEST_PREFIX},
    publication::Publisher,
    wire,
};
use crate::Snapshot;
use ble::peripheral::{self, Link, Listener};
use maps::wire::{Key, Receiver, Response};
use std::io::{self, Read, Write};
use std::time::{Duration, Instant};
use text::Text;

/// Legacy polling and notification-capable BLE byte-stream UUID triples.
#[derive(Clone, Copy)]
pub struct BleServices {
    pub pairing: peripheral::Service,
    pub telemetry: peripheral::Service,
}

/// BLE state only. It does not describe a collector, modem or map relay.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BleStatus {
    Idle,
    Pairing,
    Paired,
    Connected,
    Unavailable,
}

/// Pairing presentation state. The caller uses Idle before a session is opened
/// and Unavailable when opening fails or its owner stops.
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct BleView {
    pub link: BleStatus,
    pub key: Option<[u8; 32]>,
}

impl Default for BleView {
    fn default() -> Self {
        Self {
            link: BleStatus::Idle,
            key: None,
        }
    }
}

#[inline(never)]
fn pairing_key_error(error: tls::Error) -> io::Error {
    io::Error::other(format!("Pairing key: {error:?}"))
}

/// Open the selected BLE services. The caller's identity outlives the session.
pub fn pair<'a>(
    identity: &'a tls::Identity,
    services: BleServices,
    product: &'static str,
) -> io::Result<Output<'a, impl Listener>> {
    let listener = peripheral::open_with_notifications(services.pairing, services.telemetry)
        .map_err(|error| io::Error::other(format!("BLE: {error:?}")))?;
    Output::start(listener, identity, product)
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

/// The real radio's request, batch deadline and partially written bytes.
/// Keeping this state beside Output lets host tests exercise the production
/// encoder and transport boundary with a deterministic nonblocking stream.
struct Export {
    request: [u8; REQUEST_LEN],
    request_len: usize,
    version: Option<Version>,
    pending_start: bool,
    bytes: [u8; MAX_BYTES],
    len: usize,
    sent: usize,
    next: Option<Instant>,
}

impl Default for Export {
    fn default() -> Self {
        Self {
            request: [0; REQUEST_LEN],
            request_len: 0,
            version: None,
            pending_start: false,
            bytes: [0; MAX_BYTES],
            len: 0,
            sent: 0,
            next: None,
        }
    }
}

impl Export {
    fn requested(&self) -> bool {
        self.request_len == REQUEST_LEN
    }

    fn read_request(&mut self, stream: &mut impl Read) -> io::Result<()> {
        if self.requested() {
            return Ok(());
        }
        match stream.read(&mut self.request[self.request_len..]) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(count) => {
                self.request_len += count;
                let prefix_len = self.request_len.min(REQUEST_PREFIX.len());
                if self.request[..prefix_len] != REQUEST_PREFIX[..prefix_len] {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                if self.requested() {
                    self.version = Some(
                        Version::from_request(self.request[REQUEST_LEN - 1])
                            .ok_or(io::ErrorKind::InvalidData)?,
                    );
                    self.pending_start = true;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
            Err(error) => return Err(error),
        }
        Ok(())
    }

    fn start(&mut self, capture: &mut Capture, now: Instant) -> io::Result<bool> {
        if !self.pending_start || !capture.enable()? {
            return Ok(false);
        }
        self.pending_start = false;
        self.next = Some(now + self.version.unwrap().batch_interval());
        Ok(true)
    }

    /// True means this turn serviced the pending frame, even if it just ended.
    /// Bytes cannot be replaced or another batch consumed while a suffix remains.
    fn write_pending(&mut self, stream: &mut impl Write) -> io::Result<bool> {
        if self.len == 0 {
            return Ok(false);
        }
        match stream.write(&self.bytes[self.sent..self.len]) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(count) => self.sent += count,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
            Err(error) => return Err(error),
        }
        if self.sent == self.len {
            self.sent = 0;
            self.len = 0;
        }
        Ok(true)
    }

    fn service(
        &mut self,
        capture: &mut Capture,
        stream: &mut impl Write,
        now: Instant,
    ) -> io::Result<()> {
        if self.write_pending(stream)? {
            return Ok(());
        }
        let Some(deadline) = self.next else {
            return Ok(());
        };
        let version = self.version.unwrap();
        if let Some(batch) = capture.take(&mut self.bytes, now >= deadline, version)? {
            self.len = batch.len;
            self.next = (!batch.terminal).then(|| now + version.batch_interval());
            if batch.terminal {
                // The terminal key is now owned by this immutable wire frame.
                // Pool retirement does not destroy it during partial writes.
                capture.finish();
            }
            self.write_pending(stream)?;
        }
        Ok(())
    }
}

/// Synchronous paired telemetry/export protocol over an injected BLE listener.
/// `service` advances bounded transport/export work; `offer` supplies summaries.
/// Keep the identity and capture owner alive until this value is dropped.
/// The caller owns summary cadence, waiting and session expiry. Finish any
/// active capture when cancelling or dropping the session; dropping this value
/// releases its connection and listener without retaining the capture owner.
pub struct Output<'a, L: Listener> {
    // Fields leave scope in declaration order. Retire TLS before its listener.
    connection: Option<tls::Connection<'a, Bearer<L::Transport>>>,
    listener: L,
    identity: &'a tls::Identity,
    product: &'static str,
    key: [u8; 32],
    tile: Receiver,
    advertised_tile: Option<Key>,
    export: Export,
    last_export_service: Option<Instant>,
    max_service_gap: Duration,
    publication: Publisher<Snapshot, Text<768>>,
    live: bool,
    paired: bool,
    drained: bool,
    expires: Instant,
}

impl<'a, L: Listener> Output<'a, L> {
    /// Use an existing listener without opening a modem or spawning a worker.
    pub fn start(
        listener: L,
        identity: &'a tls::Identity,
        product: &'static str,
    ) -> io::Result<Self> {
        let key = identity.pairing_key().map_err(pairing_key_error)?;
        let now = Instant::now();
        Ok(Self {
            listener,
            identity,
            product,
            key,
            tile: Receiver::default(),
            advertised_tile: None,
            export: Export::default(),
            last_export_service: None,
            max_service_gap: Duration::ZERO,
            connection: None,
            publication: Publisher::default(),
            live: false,
            paired: false,
            drained: false,
            expires: now + Duration::from_secs(3600),
        })
    }
    /// Current BLE pairing/connection state for presentation. A disconnected
    /// previously paired session remains Paired and can accept another peer.
    pub fn view(&self) -> BleView {
        if self.live {
            BleView {
                link: BleStatus::Connected,
                key: None,
            }
        } else if self.paired {
            BleView {
                link: BleStatus::Paired,
                key: None,
            }
        } else {
            BleView {
                link: BleStatus::Pairing,
                key: Some(self.key),
            }
        }
    }
    /// Whether the current peer has completed the TLS handshake.
    pub fn live(&self) -> bool {
        self.live
    }

    /// Show the existing pairing code again when no authenticated peer is live.
    /// This neither replaces the identity nor extends the session lifetime.
    pub fn show_pairing_code(&mut self) {
        if !self.live {
            self.paired = false;
        }
    }

    /// Whether the one-hour session lifetime has elapsed. The caller retires
    /// this output and finishes capture before opening a replacement session.
    pub fn expired(&self, now: Instant) -> bool {
        now >= self.expires
    }

    /// Whether an accepted transport needs polling, including TLS negotiation.
    pub fn active(&self) -> bool {
        self.connection.is_some()
    }

    /// Combine protocol/idle retry deadlines with the caller's next active
    /// transport poll, `active_at`. Readiness hints may require an earlier turn.
    /// This excludes summary cadence; the caller combines that deadline too.
    pub fn next_service(&self, now: Instant, active_at: Instant) -> Instant {
        let mut wake = if self.active() {
            active_at
        } else {
            self.listener.idle_retry_at().unwrap_or(self.expires)
        };
        // A short batch wakes at its deadline, independent of the transport's
        // polling phase. An expired empty deadline must not cause a busy loop;
        // the next publication hint or active poll will recheck it.
        if self.export.len == 0 {
            if let Some(deadline) = self.export.next.filter(|deadline| *deadline > now) {
                wake = wake.min(deadline);
            }
        }
        wake.min(self.expires)
    }

    /// Consume the listener's retained readiness hint. Capture publications
    /// have their own hint; the caller checks both before its logical wait.
    pub fn take_ready(&self) -> bool {
        self.listener.take_ready()
    }

    /// Advance bounded BLE, capture-export and optional phone-tile work.
    /// `wanted` is the current phone tile request; None disables that exchange.
    /// Completed tile responses return to the caller, which owns route selection.
    ///
    /// Pending export bytes take priority. Otherwise a queued summary uses the
    /// turn before a new export batch or tile read. Leave service turns without
    /// a queued summary so those exchanges can progress; offering a new summary
    /// after every turn can starve them.
    ///
    /// A transport/protocol error is logged and retires the current connection,
    /// queued frames and capture epoch. The listener remains available to accept
    /// another peer, and [`Self::view`] reflects the disconnected state. The
    /// caller continues capture retirement and decides when to end this session.
    // Keep per-turn protocol scratch out of the caller's retained frame; the
    // caller can perform another TLS handshake between BLE service turns.
    #[inline(never)]
    pub fn service(
        &mut self,
        capture: &mut Capture,
        now: Instant,
        wanted: Option<Key>,
    ) -> Option<Response> {
        self.tile.retain(wanted);
        if let Some(last) = self.last_export_service.as_mut() {
            self.max_service_gap = self.max_service_gap.max(now.duration_since(*last));
            *last = now;
        }
        let error = match self.poll(capture, now, wanted) {
            Ok(response) => return response,
            Err(error) => error,
        };
        self.connection = None;
        let high_water = capture.finish();
        log::warn!("{} BLE session ended: {error}", self.product);
        if self.last_export_service.take().is_some() {
            log::info!(
                "{} BLE export: high_water={high_water} samples max_service_gap={} us",
                self.product,
                self.max_service_gap.as_micros(),
            );
        }
        self.tile = Receiver::default();
        self.advertised_tile = None;
        self.export = Export::default();
        self.live = false;
        self.drained = false;
        self.publication = Publisher::default();
        None
    }

    // TLS construction initializes a record buffer. Release that scratch before
    // polling the handshake or publishing telemetry on the same service turn.
    #[inline(never)]
    fn accept_connection(&mut self) -> io::Result<()> {
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
        Ok(())
    }

    fn poll(
        &mut self,
        capture: &mut Capture,
        now: Instant,
        wanted: Option<Key>,
    ) -> io::Result<Option<Response>> {
        let mut response = None;
        self.listener.service()?;
        if self.connection.is_none() {
            self.accept_connection()?;
        }
        if let Some(conn) = self.connection.as_mut() {
            self.live = conn.poll().map_err(|_| io::ErrorKind::PermissionDenied)?;
            if self.live {
                self.paired = true;
                let notifications = self.listener.notifying();
                if notifications && !self.export.requested() && self.publication.is_idle() {
                    self.export.read_request(conn)?;
                }
                if notifications && self.export.start(capture, now)? {
                    self.last_export_service = Some(now);
                    self.max_service_gap = Duration::ZERO;
                }
                if self.export.write_pending(conn)? {
                    return Ok(None);
                }
                let tile_ready = !notifications || self.export.requested();
                let requested_tile = wanted.filter(|_| tile_ready);
                if !self.publication.is_idle() {
                    self.drained = if notifications {
                        self.publication
                            .enqueue(conn, now, self.expires, |snapshot| {
                                let frame =
                                    wire::export_frame(snapshot, capture.scales(), requested_tile)?;
                                self.advertised_tile = requested_tile;
                                Ok(frame)
                            })?
                    } else {
                        self.publication
                            .service(conn, now, self.expires, |snapshot| {
                                let frame = wire::ble_frame(snapshot, requested_tile)?;
                                self.advertised_tile = requested_tile;
                                Ok(frame)
                            })?
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
                if notifications {
                    self.export.service(capture, conn, now)?;
                }
            }
        }
        Ok(response)
    }

    /// Whether the current phone request still needs a summary advertisement.
    /// This is protocol state; the caller decides whether to send it immediately.
    pub fn needs_tile_advertisement(&self, wanted: Option<Key>) -> bool {
        wanted.is_some()
            && wanted != self.advertised_tile
            && (!self.listener.notifying() || self.export.requested())
    }

    /// Sample and queue one summary if the stream can accept it. The producer
    /// is called only after admission. There is no hidden rate limit: the
    /// caller advances its cadence only when this returns true.
    /// Pending frame bytes are never replaced, including during export.
    /// Choose a cadence that leaves service turns without queued summaries for
    /// export and tile progress; see [`Self::service`] for their priority.
    pub fn offer(&mut self, snapshot: impl FnOnce() -> Snapshot) -> bool {
        if self.live && (self.drained || self.listener.notifying()) && self.publication.is_idle() {
            self.publication.offer(snapshot());
            true
        } else {
            false
        }
    }
}
impl<L: Listener> Drop for Output<'_, L> {
    fn drop(&mut self) {
        self.key.fill(0);
    }
}

#[cfg(test)]
mod tests {
    use super::super::export::tests::{decode, fix, fixture, imu, pause_record};
    use super::*;
    use std::collections::VecDeque;

    enum WriteStep {
        Limit(usize),
        Block,
        Zero,
        Fail,
    }

    struct Peer {
        incoming: VecDeque<u8>,
        output: Vec<u8>,
        steps: VecDeque<WriteStep>,
    }

    impl Peer {
        fn request(version: Version) -> Self {
            Self {
                incoming: [b'N', b'X', b'R', b'S', version as u8].into(),
                output: Vec::new(),
                steps: VecDeque::new(),
            }
        }
    }

    impl Read for Peer {
        fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
            let byte = self.incoming.pop_front().ok_or(io::ErrorKind::WouldBlock)?;
            bytes[0] = byte;
            Ok(1)
        }
    }

    impl Write for Peer {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            let count = match self.steps.pop_front() {
                Some(WriteStep::Block) => return Err(io::ErrorKind::WouldBlock.into()),
                Some(WriteStep::Zero) => return Ok(0),
                Some(WriteStep::Fail) => return Err(io::ErrorKind::BrokenPipe.into()),
                Some(WriteStep::Limit(count)) => count.min(bytes.len()),
                None => bytes.len(),
            };
            self.output.extend_from_slice(&bytes[..count]);
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn request(export: &mut Export, peer: &mut Peer) {
        for _ in 0..REQUEST_LEN {
            export.read_request(peer).unwrap();
        }
        assert!(export.requested());
    }

    fn frames(bytes: &[u8]) -> Vec<&[u8]> {
        let mut frames = Vec::new();
        let mut offset = 0;
        while offset < bytes.len() {
            let end = offset
                + 4
                + usize::from(u16::from_le_bytes(
                    bytes[offset + 2..offset + 4].try_into().unwrap(),
                ));
            frames.push(&bytes[offset..end]);
            offset = end;
        }
        frames
    }

    #[test]
    fn actual_export_session_preserves_partial_writes_and_full_short_batch_deadlines() {
        for version in [Version::Fixed, Version::Compact, Version::Timed] {
            let (mut capture, [_, _, mut gnss]) = fixture([None; 2]);
            let now = Instant::now();
            let mut export = Export::default();
            let mut peer = Peer::request(version);
            for _ in 0..REQUEST_LEN - 1 {
                export.read_request(&mut peer).unwrap();
                assert!(!export.start(&mut capture, now).unwrap());
            }
            export.read_request(&mut peer).unwrap();
            assert!(export.start(&mut capture, now).unwrap());
            assert!(!export.start(&mut capture, now).unwrap());
            for sequence in 0..15 {
                gnss.record(fix(sequence));
            }
            export.service(&mut capture, &mut peer, now).unwrap();
            assert!(peer.output.is_empty());
            gnss.record(fix(15));
            peer.steps.extend([
                WriteStep::Limit(3),
                WriteStep::Block,
                WriteStep::Limit(1),
                WriteStep::Limit(11),
            ]);
            export.service(&mut capture, &mut peer, now).unwrap();
            assert_eq!(peer.output.len(), 3);
            let retained = export.bytes[..export.len].to_vec();
            gnss.record(fix(16));
            export.service(&mut capture, &mut peer, now).unwrap();
            assert_eq!(peer.output.len(), 3);
            assert_eq!(&export.bytes[..export.len], retained);
            while export.len != 0 {
                export.service(&mut capture, &mut peer, now).unwrap();
            }
            let decoded = decode(&peer.output, [None; 2]);
            assert_eq!(
                decoded
                    .iter()
                    .map(|record| record.sequence)
                    .collect::<Vec<_>>(),
                (0..16).collect::<Vec<_>>()
            );
            let first_len = peer.output.len();
            export
                .service(
                    &mut capture,
                    &mut peer,
                    now + version.batch_interval() - Duration::from_nanos(1),
                )
                .unwrap();
            assert_eq!(peer.output.len(), first_len);
            export
                .service(&mut capture, &mut peer, now + version.batch_interval())
                .unwrap();
            let batches = frames(&peer.output);
            assert_eq!(batches.len(), 2);
            assert_eq!(decode(batches[1], [None; 2])[0].sequence, 16);
        }
    }

    #[test]
    fn terminal_frame_survives_pool_finish_and_backpressure_for_all_versions() {
        for version in [Version::Fixed, Version::Compact, Version::Timed] {
            let (mut capture, [_, _, mut gnss]) = fixture([None; 2]);
            let now = Instant::now();
            let mut export = Export::default();
            let mut peer = Peer::request(version);
            request(&mut export, &mut peer);
            assert!(export.start(&mut capture, now).unwrap());
            for sequence in 0..=512 {
                gnss.record(fix(sequence));
            }
            for _ in 0..32 {
                export.service(&mut capture, &mut peer, now).unwrap();
            }
            let accepted: Vec<_> = frames(&peer.output)
                .into_iter()
                .flat_map(|bytes| decode(bytes, [None; 2]))
                .map(|record| record.sequence)
                .collect();
            assert_eq!(accepted, (0..512).collect::<Vec<_>>());
            let accepted_bytes = peer.output.len();
            peer.steps.push_back(WriteStep::Block);
            export.service(&mut capture, &mut peer, now).unwrap();
            assert!(export.next.is_none());
            assert!(!capture.retirement_pending());
            assert_eq!(peer.output.len(), accepted_bytes);
            let terminal = if version == Version::Fixed {
                vec![0xa5, 1, 0, 0]
            } else {
                vec![0xa5, version as u8, 5, 0, 3, 0, 2, 0, 0]
            };
            assert_eq!(&export.bytes[..export.len], terminal);
            gnss.record(fix(999)); // Closed epoch cannot append after its terminal.
            peer.steps
                .extend([WriteStep::Limit(1), WriteStep::Block, WriteStep::Limit(2)]);
            while export.len != 0 {
                export.service(&mut capture, &mut peer, now).unwrap();
            }
            assert_eq!(&peer.output[accepted_bytes..], terminal);
            export
                .service(&mut capture, &mut peer, now + Duration::from_secs(1))
                .unwrap();
            assert_eq!(peer.output.len(), accepted_bytes + terminal.len());

            // A new connection parses and starts a new epoch without replaying
            // the old record or terminal into its byte stream.
            let mut reopened = Export::default();
            let mut next_peer = Peer::request(version);
            request(&mut reopened, &mut next_peer);
            assert!(reopened.start(&mut capture, now).unwrap());
            gnss.record(fix(1_000));
            reopened
                .service(&mut capture, &mut next_peer, now + version.batch_interval())
                .unwrap();
            assert_eq!(decode(&next_peer.output, [None; 2])[0].sequence, 1_000);
        }
    }

    #[test]
    fn new_request_waits_for_old_admission_without_blocking_or_reusing_it() {
        let (mut capture, [primary, _, mut gnss]) = fixture([None; 2]);
        capture.enable().unwrap();
        let paused = pause_record(primary, imu(0, 10, 100));
        capture.finish();
        assert!(capture.retirement_pending());
        let now = Instant::now();
        let mut export = Export::default();
        let mut peer = Peer::request(Version::Timed);
        request(&mut export, &mut peer);
        assert!(!export.start(&mut capture, now).unwrap());
        export
            .service(&mut capture, &mut peer, now + Duration::from_secs(1))
            .unwrap();
        assert!(peer.output.is_empty());
        let _primary = paused.release();
        assert!(!capture.retire());
        assert!(export.start(&mut capture, now).unwrap());
        gnss.record(fix(20));
        export
            .service(
                &mut capture,
                &mut peer,
                now + Version::Timed.batch_interval(),
            )
            .unwrap();
        let values = decode(&peer.output, [None; 2]);
        assert_eq!(values.len(), 1);
        assert_eq!((values[0].source, values[0].sequence), (2, 20));
    }

    #[test]
    fn invalid_requests_and_transport_failures_do_not_advance_pending_bytes() {
        for invalid in [b"NXRS\x00".as_slice(), b"NXRT\x03".as_slice()] {
            let mut export = Export::default();
            let mut input = invalid;
            assert_eq!(
                export.read_request(&mut input).unwrap_err().kind(),
                io::ErrorKind::InvalidData
            );
        }
        for (step, kind) in [
            (WriteStep::Zero, io::ErrorKind::WriteZero),
            (WriteStep::Fail, io::ErrorKind::BrokenPipe),
        ] {
            let (mut capture, [_, _, mut gnss]) = fixture([None; 2]);
            let now = Instant::now();
            let mut export = Export::default();
            let mut peer = Peer::request(Version::Fixed);
            request(&mut export, &mut peer);
            export.start(&mut capture, now).unwrap();
            gnss.record(fix(1));
            peer.steps.push_back(step);
            assert_eq!(
                export
                    .service(
                        &mut capture,
                        &mut peer,
                        now + Version::Fixed.batch_interval()
                    )
                    .unwrap_err()
                    .kind(),
                kind
            );
            assert!(peer.output.is_empty());
            assert_eq!(export.sent, 0);
            assert_ne!(export.len, 0);
            capture.finish();
            assert!(!capture.retirement_pending());
        }
    }
}
