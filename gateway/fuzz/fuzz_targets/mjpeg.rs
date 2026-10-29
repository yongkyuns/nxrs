#![no_main]
use libfuzzer_sys::fuzz_target;
use nxrs_camera_ingest::protocol::{Reader, MAX_FRAME};
use std::{
    pin::Pin,
    task::{Context, Poll},
};
use tokio::io::{AsyncRead, ReadBuf};

thread_local! {
    static RUNTIME: tokio::runtime::Runtime =
        tokio::runtime::Builder::new_current_thread().build().unwrap();
}
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
        let n = self.chunk.min(output.remaining()).min(self.bytes.len());
        output.put_slice(&self.bytes[..n]);
        self.bytes = &self.bytes[n..];
        Poll::Ready(Ok(()))
    }
}
fn exercise(data: &[u8]) -> usize {
    let Some((&fragment, wire)) = data.split_first() else {
        return 0;
    };
    if wire.len() > 512 * 1024 {
        return 0;
    }
    RUNTIME.with(|runtime| {
        runtime.block_on(async {
            let source = Fragmented {
                bytes: wire,
                chunk: usize::from(fragment) + 1,
            };
            let Ok(mut reader) = Reader::start(source, 320, 240).await else {
                return 0;
            };
            let mut previous = None;
            let mut accepted = 0;
            // Slice input cannot wait for I/O. Bound accepted frames/work as well.
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
                accepted += 1;
            }
            accepted
        })
    })
}
fuzz_target!(init: {
    assert_eq!(exercise(include_bytes!("../seeds/mjpeg/two-frames")), 2);
    assert_eq!(exercise(include_bytes!("../seeds/mjpeg/metadata")), 1);
}, |data: &[u8]| { exercise(data); });
