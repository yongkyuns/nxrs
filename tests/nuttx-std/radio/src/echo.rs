//! Identical echo payloads with transport-correct I/O and bounded shutdown.
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, SocketAddrV4, TcpStream, UdpSocket};
use std::time::{Duration, Instant};

const IO_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy)]
pub(super) enum Transport {
    Udp,
    Tcp,
}

impl Transport {
    pub(super) fn parse(name: &str) -> Option<Self> {
        match name {
            "udp" => Some(Self::Udp),
            "tcp" => Some(Self::Tcp),
            _ => None,
        }
    }
}

pub(super) enum EchoSocket {
    Udp(UdpSocket),
    Tcp(TcpStream),
}

impl EchoSocket {
    pub(super) fn open(peer: SocketAddrV4, transport: Transport) -> io::Result<Self> {
        if matches!(transport, Transport::Tcp) {
            let stream = TcpStream::connect_timeout(&SocketAddr::V4(peer), IO_TIMEOUT)?;
            stream.set_nodelay(true)?;
            Ok(Self::Tcp(stream))
        } else {
            let socket = UdpSocket::bind("0.0.0.0:0")?;
            socket.connect(peer)?;
            socket.set_read_timeout(Some(IO_TIMEOUT))?;
            socket.set_write_timeout(Some(IO_TIMEOUT))?;
            Ok(Self::Udp(socket))
        }
    }

    pub(super) fn send(&mut self, data: &mut [u8]) -> io::Result<usize> {
        match self {
            Self::Udp(socket) => socket.send(data),
            Self::Tcp(stream) => transfer(stream, data, true),
        }
    }

    pub(super) fn receive(&mut self, data: &mut [u8], expected: usize) -> io::Result<usize> {
        match self {
            Self::Udp(socket) => socket.recv(data),
            Self::Tcp(stream) => transfer(stream, &mut data[..expected], false),
        }
    }

    pub(super) fn finish(self) -> io::Result<()> {
        if let Self::Tcp(mut stream) = self {
            // NuttX close queues FIN asynchronously. Keep Wi-Fi up until the
            // peer sees our half-close and closes its end of the stream too.
            stream.shutdown(Shutdown::Write)?;
            match transfer(&mut stream, &mut [0], false) {
                Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => Ok(()),
                Err(error) => Err(error),
                Ok(_) => Err(io::ErrorKind::InvalidData.into()),
            }
        } else {
            Ok(())
        }
    }
}

fn transfer(stream: &mut TcpStream, data: &mut [u8], send: bool) -> io::Result<usize> {
    // TCP can split a message. Bound the entire direction, not each fragment.
    let deadline = Instant::now() + IO_TIMEOUT;
    let mut offset = 0;
    while offset < data.len() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(io::ErrorKind::TimedOut.into());
        }
        let result = if send {
            stream.set_write_timeout(Some(remaining))?;
            stream.write(&data[offset..])
        } else {
            stream.set_read_timeout(Some(remaining))?;
            stream.read(&mut data[offset..])
        };
        match result {
            Ok(0) => {
                return Err(if send {
                    io::ErrorKind::WriteZero
                } else {
                    io::ErrorKind::UnexpectedEof
                }
                .into());
            }
            Ok(length) => offset += length,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(offset)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    fn listener() -> (TcpListener, SocketAddrV4) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let SocketAddr::V4(peer) = listener.local_addr().unwrap() else {
            unreachable!()
        };
        (listener, peer)
    }

    #[test]
    fn transport_selection_is_explicit() {
        assert!(matches!(Transport::parse("udp"), Some(Transport::Udp)));
        assert!(Transport::parse("udp-c").is_none());
        assert!(matches!(Transport::parse("tcp"), Some(Transport::Tcp)));
        assert!(Transport::parse("tcp-c").is_none());
        assert!(Transport::parse("other").is_none());
    }

    #[test]
    fn echoes_then_confirms_both_tcp_half_closes() {
        let (listener, peer) = listener();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(IO_TIMEOUT)).unwrap();
            let mut packet = [0; 16];
            stream.read_exact(&mut packet).unwrap();
            stream.write_all(&packet).unwrap();
            assert_eq!(stream.read(&mut [0]).unwrap(), 0);
            // Drop sends the peer's FIN. The client must observe it.
        });
        let mut socket = EchoSocket::open(peer, Transport::Tcp).unwrap();
        let mut sent = [42; 16];
        let mut received = [0; 17];
        assert_eq!(socket.send(&mut sent).unwrap(), sent.len());
        assert_eq!(
            socket.receive(&mut received, sent.len()).unwrap(),
            sent.len()
        );
        assert_eq!(received[..sent.len()], sent);
        socket.finish().unwrap();
        server.join().unwrap();
    }

    #[test]
    fn unexpected_trailing_data_is_not_successful_shutdown() {
        let (listener, peer) = listener();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(IO_TIMEOUT)).unwrap();
            assert_eq!(stream.read(&mut [0]).unwrap(), 0);
            stream.write_all(&[1]).unwrap();
        });
        let socket = EchoSocket::open(peer, Transport::Tcp).unwrap();
        assert_eq!(
            socket.finish().unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        server.join().unwrap();
    }

    #[test]
    fn shutdown_does_not_wait_forever_for_an_unclosed_peer() {
        let (listener, peer) = listener();
        let socket = EchoSocket::open(peer, Transport::Tcp).unwrap();
        let (_unclosed, _) = listener.accept().unwrap();
        let start = Instant::now();
        let error = socket.finish().unwrap_err();
        assert!(matches!(
            error.kind(),
            io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
        ));
        assert!(start.elapsed() < IO_TIMEOUT + Duration::from_secs(1));
    }
}
