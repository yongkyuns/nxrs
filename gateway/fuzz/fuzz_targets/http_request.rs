#![no_main]
use libfuzzer_sys::fuzz_target;
use std::{
    cell::Cell,
    io::{self, Read, Write},
    rc::Rc,
};
use stream_service::{HttpSession, Options, ServerStats};

struct Transport<'a> {
    bytes: &'a [u8],
    chunk: usize,
    blocked: bool,
    calls: usize,
    written: usize,
    dropped: Rc<Cell<usize>>,
}
impl Read for Transport<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        self.calls += 1;
        if self.blocked && self.calls.is_multiple_of(3) {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let n = self.chunk.min(output.len()).min(self.bytes.len());
        output[..n].copy_from_slice(&self.bytes[..n]);
        self.bytes = &self.bytes[n..];
        Ok(n)
    }
}
impl Write for Transport<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.calls += 1;
        if self.blocked && self.calls.is_multiple_of(3) {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let n = bytes.len().min(self.chunk);
        self.written += n;
        // No image is offered: only one bounded HTTP response/header is possible.
        assert!(self.written <= 4096);
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
impl Drop for Transport<'_> {
    fn drop(&mut self) {
        self.dropped.set(self.dropped.get() + 1);
    }
}
fn exercise(data: &[u8]) -> Option<ServerStats> {
    let (&mode, bytes) = data.split_first()?;
    if bytes.len() > 8192 {
        return None;
    }
    let dropped = Rc::new(Cell::new(0));
    let transport = |bytes| Transport {
        bytes,
        chunk: usize::from(mode & 127) + 1,
        blocked: mode & 128 != 0,
        calls: 0,
        written: 0,
        dropped: dropped.clone(),
    };
    let mut session = HttpSession::new([0; 64], Options::default()).unwrap();
    assert!(session.accept(transport(bytes), 0));
    assert!(!session.accept(transport(&[]), 0));
    assert_eq!(dropped.get(), 1, "busy transport must retire immediately");
    for now in 0..2048 {
        if !session.poll(now, None).active {
            break;
        }
    }
    assert!(
        !session.poll(10_000, None).active,
        "expired client must retire"
    );
    session.disconnect();
    let stats = session.stats();
    assert_eq!(
        (stats.accepted, stats.disconnected, stats.rejected_busy),
        (1, 1, 1)
    );
    assert_eq!(dropped.get(), 2);
    let _storage = session.into_buffer();
    Some(stats)
}
fuzz_target!(init: {
    assert_eq!(exercise(include_bytes!("../seeds/http_request/clock")).unwrap().errors, 0);
}, |data: &[u8]| { exercise(data); });
