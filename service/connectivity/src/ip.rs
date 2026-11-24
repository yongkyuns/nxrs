use std::{io, net::SocketAddrV4};
#[cfg(feature = "pinned-tls")]
use std::{
    net::{SocketAddr, TcpStream},
    thread::{self, JoinHandle},
    time::Duration,
};

#[derive(Clone, Copy)]
pub struct Endpoint {
    pub address: SocketAddrV4,
    pub pin: [u8; 32],
}

impl Endpoint {
    pub fn parse(address: &str, pin: &str) -> io::Result<Self> {
        let address: SocketAddrV4 = address
            .parse()
            .ok()
            .filter(|address: &SocketAddrV4| {
                address.port() != 0
                    && !address.ip().is_unspecified()
                    && !address.ip().is_multicast()
            })
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "expected IPv4:port"))?;
        let mut digest = [0; 32];
        if pin.len() != 64 || !pin.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "expected 64 hex digits for the TLS pin",
            ));
        }
        for (index, byte) in digest.iter_mut().enumerate() {
            *byte = u8::from_str_radix(&pin[2 * index..2 * index + 2], 16)
                .map_err(|_| io::ErrorKind::InvalidInput)?;
        }
        if digest == [0; 32] {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "empty TLS pin"));
        }
        Ok(Self {
            address,
            pin: digest,
        })
    }

    #[cfg(feature = "pinned-tls")]
    pub fn secure<'a>(
        self,
        identity: &'a tls::Identity,
        socket: TcpStream,
        lifetime: Duration,
    ) -> Result<tls::Connection<'a>, tls::Error> {
        identity.connect_pinned(socket, self.pin, lifetime)
    }

    #[cfg(feature = "pinned-tls")]
    pub fn connect(self, timeout: Duration) -> io::Result<TcpStream> {
        TcpStream::connect_timeout(&SocketAddr::V4(self.address), timeout)
    }
}

/// Isolates a blocking TCP connect. The owner polls for the result.
/// Dropping a pending attempt joins the worker before releasing its resources.
#[cfg(feature = "pinned-tls")]
pub struct Dial(Option<JoinHandle<io::Result<TcpStream>>>);

#[cfg(feature = "pinned-tls")]
impl Dial {
    pub fn start(
        builder: thread::Builder,
        endpoint: Endpoint,
        timeout: Duration,
    ) -> io::Result<Self> {
        builder
            .spawn(move || endpoint.connect(timeout))
            .map(|task| Self(Some(task)))
    }

    /// Return `Ok(None)` while dialing. A completed result can be read once.
    pub fn poll(&mut self) -> io::Result<Option<TcpStream>> {
        let task = self.0.as_ref().ok_or(io::ErrorKind::NotConnected)?;
        if task.is_finished() {
            self.0
                .take()
                .unwrap()
                .join()
                .map_err(|_| io::Error::other("TCP dial worker panicked"))?
                .map(Some)
        } else {
            Ok(None)
        }
    }
}

#[cfg(feature = "pinned-tls")]
impl Drop for Dial {
    fn drop(&mut self) {
        if let Some(task) = self.0.take() {
            let _ = task.join();
        }
    }
}

#[cfg(all(test, feature = "pinned-tls"))]
mod tests {
    use super::*;

    #[test]
    fn a_consumed_dial_error_cannot_look_pending() {
        let task = thread::spawn(|| Err(io::ErrorKind::ConnectionRefused.into()));
        while !task.is_finished() {
            thread::yield_now();
        }
        let mut dial = Dial(Some(task));

        assert_eq!(
            dial.poll().unwrap_err().kind(),
            io::ErrorKind::ConnectionRefused
        );
        assert_eq!(dial.poll().unwrap_err().kind(), io::ErrorKind::NotConnected);
    }
}
