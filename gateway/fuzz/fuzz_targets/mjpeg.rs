#![no_main]
use libfuzzer_sys::fuzz_target;
use nxrs_camera_ingest::protocol::{Reader, MAX_FRAME};
use std::{
    cell::RefCell,
    pin::Pin,
    task::{Context, Poll},
};
use tokio::io::{AsyncRead, ReadBuf};

thread_local! {
    static RUNTIME: RefCell<tokio::runtime::Runtime> = RefCell::new(
        tokio::runtime::Builder::new_current_thread().build().unwrap());
}

// The first byte controls fragmentation independently of the response bytes.
struct Fragmented<'a> {
    bytes: &'a [u8],
    chunk: usize,
}
impl AsyncRead for Fragmented<'_> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
        output: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let count = self.chunk.min(output.remaining()).min(self.bytes.len());
        output.put_slice(&self.bytes[..count]);
        self.bytes = &self.bytes[count..];
        Poll::Ready(Ok(()))
    }
}

fuzz_target!(|data: &[u8]| {
    let Some((&fragment, wire)) = data.split_first() else {
        return;
    };
    if wire.len() > 512 * 1024 {
        return;
    }
    RUNTIME.with(|runtime| {
        runtime.borrow_mut().block_on(async {
            let source = Fragmented {
                bytes: wire,
                chunk: usize::from(fragment) + 1,
            };
            let Ok(mut reader) = Reader::start(source, 320, 240).await else {
                return;
            };
            let mut previous = None;
            // A finite slice cannot stall I/O; cap frames/work per fuzz iteration too.
            for _ in 0..8 {
                let Ok(frame) = reader.next_frame().await else {
                    break;
                };
                assert!((4..=MAX_FRAME).contains(&frame.length));
                assert_eq!(reader.payload(frame).len(), frame.length);
                assert!(frame.sequence > 0);
                if let Some((sequence, capture)) = previous {
                    assert!(frame.sequence > sequence && frame.capture_ms >= capture);
                }
                previous = Some((frame.sequence, frame.capture_ms));
            }
        })
    });
});
