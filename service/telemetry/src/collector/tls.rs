//! TLS adapter for a caller-connected collector socket.
use super::Reports;
use crate::Snapshot;
use std::io::{self, Read};
use std::net::TcpStream;
use std::time::{Duration, Instant};

const SESSION_LIFETIME: Duration = Duration::from_secs(3600);

#[inline(never)]
fn tls_error(error: tls::Error) -> io::Error {
    io::Error::other(format!("collector TLS: {error:?}"))
}

/// A pinned TLS session that borrows the caller's identity and owns its socket.
/// It creates no worker and performs no dialing, waiting or reconnects. Keep the
/// same [`Reports`] across replacement sessions to preserve its report cadence.
pub struct Upload<'a> {
    connection: tls::Connection<'a>,
    ready: bool,
    product: &'static str,
}

impl<'a> Upload<'a> {
    /// Take an already connected socket and begin a nonblocking TLS handshake.
    /// `pin` is the expected peer certificate's SHA-256 digest. Address selection
    /// and connection establishment belong to the caller.
    /// The caller keeps `identity` alive until this upload is dropped. The TLS
    /// session has a one-hour lifetime; `service` reports expiry as an error.
    /// `product` is a diagnostic label, not part of the collector wire format.
    pub fn open(
        identity: &'a tls::Identity,
        socket: TcpStream,
        pin: [u8; 32],
        product: &'static str,
    ) -> io::Result<Self> {
        let connection = identity
            .connect_pinned(socket, pin, SESSION_LIFETIME)
            .map_err(tls_error)?;
        Ok(Self {
            connection,
            ready: false,
            product,
        })
    }

    /// Whether `service` has completed the authenticated TLS handshake.
    /// This reports observed readiness without probing current socket liveness.
    /// It is valid until `service` returns an error; discard the upload then.
    pub fn is_ready(&self) -> bool {
        self.ready
    }

    /// Advance the handshake or a due report without waiting for the socket.
    /// A true result means one complete report was written and flushed. False
    /// means the handshake, write or flush needs another turn, or no report is
    /// due. The snapshot closure runs only when a ready stream needs a report.
    ///
    /// Drive pending work from the caller's readiness or polling loop; the next
    /// report deadline alone does not describe pending TLS or write work. On an
    /// error, drop this upload, call [`Reports::reset_stream`], then reconnect
    /// under the caller's retry policy. The report allowance is not refreshed.
    pub fn service(
        &mut self,
        reports: &mut Reports,
        now: Instant,
        snapshot: impl FnOnce() -> Snapshot,
    ) -> io::Result<bool> {
        if !self.ready {
            if !self.connection.poll().map_err(tls_error)? {
                return Ok(false);
            }
            self.ready = true;
            log::info!("{} collector TLS online", self.product);
        }
        if !reports.publication.in_flight() {
            // Detect a closed peer even with no sample waiting. Finish any
            // pending write before invoking another TLS operation.
            match self.connection.read(&mut [0]) {
                Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                Ok(_) => return Err(io::ErrorKind::InvalidData.into()),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error),
            }
        }
        reports.service(&mut self.connection, now, snapshot)
    }
}
