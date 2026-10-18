//! Identical echo payloads with transport-correct I/O and bounded shutdown.
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, SocketAddrV4, TcpStream, UdpSocket};
use std::time::{Duration, Instant};

const IO_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy)]
pub(super) enum Transport {
    Udp,
    Tcp,
    NativeUdp,
    NativeTcp,
}

impl Transport {
    pub(super) fn parse(name: &str) -> Option<Self> {
        match name {
            "udp" => Some(Self::Udp),
            "tcp" => Some(Self::Tcp),
            "udp-c" => Some(Self::NativeUdp),
            "tcp-c" => Some(Self::NativeTcp),
            _ => None,
        }
    }
}

pub(super) enum EchoSocket {
    Udp(UdpSocket),
    Tcp(TcpStream),
    #[cfg(target_os = "nuttx")]
    NativeTcp(super::native_socket::Socket),
    #[cfg(target_os = "nuttx")]
    NativeUdp(super::native_socket::Socket),
}

impl EchoSocket {
    #[cfg(target_os = "nuttx")]
    pub(super) fn report_tcp_state(&self, sequence: u32, stage: &str) {
        use std::os::fd::AsRawFd;

        let fd = match self {
            Self::Tcp(socket) => socket.as_raw_fd(),
            Self::NativeTcp(socket) => socket.raw_fd(),
            _ => return,
        };
        if let Some(values) = super::native_socket::tcp_state(fd) {
            println!(
                "NXRS_RADIO_TCP_STATE stage={stage} sequence={sequence} available=1 state={} rto_halfsecs={} timer_halfsecs={} retransmits={} unacked_bytes={} peer_window={} rtt_mean_scaled={} rtt_deviation_scaled={}",
                values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7],
            );
        } else {
            println!("NXRS_RADIO_TCP_STATE stage={stage} sequence={sequence} available=0");
        }
    }

    #[cfg(target_os = "nuttx")]
    pub(super) fn report_receive_queue(&self, sequence: u32, stage: &str, elapsed_ms: u32) {
        use std::os::fd::AsRawFd;

        let (fd, datagram) = match self {
            Self::Udp(socket) => (socket.as_raw_fd(), true),
            Self::Tcp(socket) => (socket.as_raw_fd(), false),
            Self::NativeUdp(socket) => (socket.raw_fd(), true),
            Self::NativeTcp(socket) => (socket.raw_fd(), false),
        };
        let Some(values) = super::native_socket::snapshot(fd) else {
            println!("NXRS_RADIO_RX_QUEUE stage={stage} sequence={sequence} available=0");
            return;
        };
        // A TCP timeout may be mid-frame: its remaining prefix is not a sequence.
        let sequence_valid = datagram && values[3] == 0 && values[2] == 4;
        let head_sequence = if sequence_valid { values[4] } else { 0 };
        println!(
            "NXRS_RADIO_RX_QUEUE stage={stage} sequence={sequence} elapsed_ms={elapsed_ms} queued_bytes={} ioctl_errno={} peek_bytes={} peek_errno={} head_sequence_valid={} head_sequence={head_sequence}",
            values[0], values[1], values[2], values[3], u32::from(sequence_valid),
        );
    }

    pub(super) fn open(peer: SocketAddrV4, transport: Transport) -> io::Result<Self> {
        if matches!(transport, Transport::NativeTcp | Transport::NativeUdp) {
            #[cfg(target_os = "nuttx")]
            return if matches!(transport, Transport::NativeTcp) {
                super::native_socket::Socket::open(peer, true).map(Self::NativeTcp)
            } else {
                super::native_socket::Socket::open(peer, false).map(Self::NativeUdp)
            };
            #[cfg(not(target_os = "nuttx"))]
            return Err(io::ErrorKind::Unsupported.into());
        }
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
            #[cfg(target_os = "nuttx")]
            Self::NativeTcp(socket) => socket.transfer(data, true),
            #[cfg(target_os = "nuttx")]
            Self::NativeUdp(socket) => socket.send_datagram(data),
        }
    }

    pub(super) fn receive(&mut self, data: &mut [u8], expected: usize) -> io::Result<usize> {
        match self {
            Self::Udp(socket) => socket.recv(data),
            Self::Tcp(stream) => transfer(stream, &mut data[..expected], false),
            #[cfg(target_os = "nuttx")]
            Self::NativeTcp(socket) => socket.transfer(&mut data[..expected], false),
            #[cfg(target_os = "nuttx")]
            Self::NativeUdp(socket) => socket.receive_datagram(data),
        }
    }

    pub(super) fn finish(self) -> io::Result<()> {
        #[cfg(target_os = "nuttx")]
        if let Self::NativeTcp(socket) = self {
            return socket.finish();
        }
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
                .into())
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
        assert!(matches!(
            Transport::parse("udp-c"),
            Some(Transport::NativeUdp)
        ));
        assert!(matches!(Transport::parse("tcp"), Some(Transport::Tcp)));
        assert!(matches!(
            Transport::parse("tcp-c"),
            Some(Transport::NativeTcp)
        ));
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
