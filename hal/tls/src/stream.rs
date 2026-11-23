use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream};

/// Ordered, reliable bytes for a TLS record layer. Reads and writes must
/// return promptly and report temporary lack of progress as WouldBlock.
pub trait Stream: Read + Write {
    /// Set up nonblocking operation before the SDK can call the transport.
    fn prepare(&mut self) -> io::Result<()>;

    /// Retire this connection without waiting for the peer. Repeated calls
    /// have the same effect; no queued bytes may enter another connection.
    fn abort(&mut self);

    /// Release an owned transport. Custom transports can notify their peer in `abort`.
    fn retire(mut self)
    where
        Self: Sized,
    {
        self.abort();
    }
}

impl Stream for TcpStream {
    fn prepare(&mut self) -> io::Result<()> {
        self.set_nonblocking(true)
    }

    fn abort(&mut self) {
        let _ = self.shutdown(Shutdown::Both);
    }

    fn retire(self) {
        // Close directly. On NuttX, shutdown disables the callback that
        // reclaims queued TCP writes when the network device goes down.
        drop(self);
    }
}
