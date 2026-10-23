//! Explicit UDP baseline or TCP control; identical bounded 16-byte echo records.
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, SocketAddrV4, TcpStream, UdpSocket};
use std::time::Duration;

pub const RECORD: usize = 16;

fn context<T>(stage: &'static str, result: io::Result<T>) -> io::Result<T> {
    result.inspect_err(|e| {
        eprintln!(
            "CSI_IO_ERROR stage={stage} os_error={}",
            e.raw_os_error().unwrap_or(0)
        );
    })
}

/// A stream can split a record at any byte. Each poll performs at most one read
/// or write; backpressure never grows storage or blocks the capture consumer.
pub struct Framing {
    tx: [u8; RECORD],
    written: usize,
    rx: [u8; RECORD],
    read: usize,
}
impl Default for Framing {
    fn default() -> Self {
        Self::new()
    }
}
impl Framing {
    pub const fn new() -> Self {
        Self {
            tx: [0; RECORD],
            written: RECORD,
            rx: [0; RECORD],
            read: 0,
        }
    }
    pub fn ready(&self) -> bool {
        self.written == RECORD
    }
    pub fn enqueue(&mut self, packet: [u8; RECORD]) -> io::Result<()> {
        if !self.ready() {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        self.tx = packet;
        self.written = 0;
        Ok(())
    }
    pub fn flush(&mut self, writer: &mut impl Write) -> io::Result<()> {
        if self.ready() {
            return Ok(());
        }
        match writer.write(&self.tx[self.written..]) {
            Ok(0) => Err(io::ErrorKind::WriteZero.into()),
            Ok(n) => {
                self.written += n;
                Ok(())
            }
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                Ok(())
            }
            Err(e) => Err(e),
        }
    }
    pub fn receive(&mut self, reader: &mut impl Read) -> io::Result<Option<[u8; RECORD]>> {
        match reader.read(&mut self.rx[self.read..]) {
            Ok(0) => Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(n) => {
                self.read += n;
                if self.read == RECORD {
                    self.read = 0;
                    Ok(Some(self.rx))
                } else {
                    Ok(None)
                }
            }
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                Ok(None)
            }
            Err(e) => Err(e),
        }
    }
}

pub enum Traffic {
    Udp(UdpSocket),
    Tcp(TcpStream, Framing),
}
impl Traffic {
    pub fn shutdown_write(&mut self) -> io::Result<()> {
        match self {
            Self::Udp(_) => Ok(()),
            Self::Tcp(stream, f) => {
                let result = stream.shutdown(Shutdown::Write);
                if !f.ready() || f.read != 0 {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                result
            }
        }
    }
    pub fn peer_closed(&mut self) -> io::Result<bool> {
        match self {
            Self::Udp(_) => Ok(true),
            Self::Tcp(stream, _) => match stream.read(&mut [0; 1]) {
                Ok(0) => Ok(true),
                Ok(_) => Err(io::ErrorKind::InvalidData.into()),
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    Ok(false)
                }
                Err(e) => Err(e),
            },
        }
    }
    pub fn connect(peer: SocketAddrV4, tcp: bool) -> io::Result<Self> {
        if tcp {
            let stream = context(
                "tcp_connect",
                TcpStream::connect_timeout(&SocketAddr::V4(peer), Duration::from_secs(2)),
            )?;
            context("tcp_nodelay", stream.set_nodelay(true))?;
            context("tcp_nonblocking", stream.set_nonblocking(true))?;
            Ok(Self::Tcp(stream, Framing::new()))
        } else {
            let socket = context(
                "udp_bind",
                UdpSocket::bind((std::net::Ipv4Addr::UNSPECIFIED, 0)),
            )?;
            context("udp_connect", socket.connect(peer))?;
            context("udp_nonblocking", socket.set_nonblocking(true))?;
            Ok(Self::Udp(socket))
        }
    }
    pub fn ready(&self) -> bool {
        match self {
            Self::Udp(_) => true,
            Self::Tcp(_, f) => f.ready(),
        }
    }
    pub fn send(&mut self, data: [u8; RECORD]) -> io::Result<()> {
        match self {
            Self::Udp(socket) => {
                if socket.send(&data)? != RECORD {
                    return Err(io::ErrorKind::WriteZero.into());
                }
                Ok(())
            }
            Self::Tcp(stream, f) => {
                f.enqueue(data)?;
                f.flush(stream)
            }
        }
    }
    pub fn flush(&mut self) -> io::Result<()> {
        match self {
            Self::Udp(_) => Ok(()),
            Self::Tcp(stream, f) => f.flush(stream),
        }
    }
    pub fn receive(&mut self) -> io::Result<Option<[u8; RECORD]>> {
        match self {
            Self::Tcp(stream, f) => f.receive(stream),
            Self::Udp(socket) => {
                let mut packet = [0; RECORD + 1];
                match socket.recv(&mut packet) {
                    Ok(RECORD) => Ok(Some(packet[..RECORD].try_into().unwrap())),
                    Ok(_) => Err(io::ErrorKind::InvalidData.into()),
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => Ok(None),
                    Err(e) => Err(e),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tcp_loopback_preserves_records_through_partial_echoes() {
        let listener = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
        let peer = match listener.local_addr().unwrap() {
            SocketAddr::V4(v) => v,
            _ => unreachable!(),
        };
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            stream
                .set_write_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut record = [0; RECORD];
            stream.read_exact(&mut record).unwrap();
            for fragment in record.chunks(3) {
                stream.write_all(fragment).unwrap();
            }
        });
        let mut traffic = Traffic::connect(peer, true).unwrap();
        let packet = [37; RECORD];
        traffic.send(packet).unwrap();
        let end = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            traffic.flush().unwrap();
            if let Some(echo) = traffic.receive().unwrap() {
                assert_eq!(echo, packet);
                break;
            }
            assert!(std::time::Instant::now() < end);
            std::thread::sleep(Duration::from_millis(1));
        }
        server.join().unwrap();
    }
    struct PartialWriter {
        bytes: Vec<u8>,
        calls: usize,
    }
    impl Write for PartialWriter {
        fn write(&mut self, data: &[u8]) -> io::Result<usize> {
            self.calls += 1;
            if self.calls == 2 {
                return Err(io::ErrorKind::WouldBlock.into());
            }
            let n = data.len().min(3);
            self.bytes.extend_from_slice(&data[..n]);
            Ok(n)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    #[test]
    fn partial_write_and_backpressure_preserve_the_record_without_overwrite() {
        let mut f = Framing::new();
        let a = [7; RECORD];
        f.enqueue(a).unwrap();
        let mut writer = PartialWriter {
            bytes: Vec::new(),
            calls: 0,
        };
        f.flush(&mut writer).unwrap();
        f.flush(&mut writer).unwrap();
        assert_eq!(writer.calls, 2);
        assert_eq!(writer.bytes, [7; 3]);
        assert_eq!(
            f.enqueue([9; RECORD]).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        for _ in 0..5 {
            f.flush(&mut writer).unwrap();
        }
        assert!(f.ready());
        assert_eq!(writer.bytes, a);
    }
    struct PartialReader {
        bytes: std::io::Cursor<Vec<u8>>,
        calls: usize,
    }
    impl Read for PartialReader {
        fn read(&mut self, data: &mut [u8]) -> io::Result<usize> {
            self.calls += 1;
            if self.calls == 2 {
                return Err(io::ErrorKind::Interrupted.into());
            }
            let n = data.len().min(3);
            self.bytes.read(&mut data[..n])
        }
    }
    #[test]
    fn split_and_coalesced_records_are_reassembled_and_eof_is_not_success() {
        let a = [1; RECORD];
        let b = [2; RECORD];
        let mut reader = PartialReader {
            bytes: std::io::Cursor::new([a, b].concat()),
            calls: 0,
        };
        let mut f = Framing::new();
        let mut received = Vec::new();
        for _ in 0..13 {
            if let Some(p) = f.receive(&mut reader).unwrap() {
                received.push(p);
            }
        }
        assert_eq!(received, [a, b]);
        assert_eq!(
            f.receive(&mut reader).unwrap_err().kind(),
            io::ErrorKind::UnexpectedEof
        );
        let mut truncated = std::io::Cursor::new([3; 15]);
        assert!(f.receive(&mut truncated).unwrap().is_none());
        assert_eq!(
            f.receive(&mut truncated).unwrap_err().kind(),
            io::ErrorKind::UnexpectedEof
        );
    }
    #[test]
    fn zero_length_write_is_terminal() {
        let mut f = Framing::new();
        f.enqueue([0; RECORD]).unwrap();
        let mut full: &mut [u8] = &mut [];
        assert_eq!(
            f.flush(&mut full).unwrap_err().kind(),
            io::ErrorKind::WriteZero
        );
    }
}
