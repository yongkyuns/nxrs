use camera::{Camera, Capture, DeviceError, Format, PixelFormat};
use camera_stream::LiveCamera;
use std::cell::Cell;
use std::net::TcpListener;
use std::rc::Rc;
use stream_service::Options;

struct Source {
    format: Option<Format>,
    stops: Rc<Cell<usize>>,
    negotiated_pixels: Option<PixelFormat>,
}
impl Camera for Source {
    fn start(&mut self, mut format: Format) -> Result<Format, DeviceError> {
        if let Some(pixels) = self.negotiated_pixels {
            format.pixels = pixels;
        }
        self.format = Some(format);
        Ok(format)
    }
    fn poll_frame(&mut self, now: u64, bytes: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        bytes[..4].copy_from_slice(&[0xff, 0xd8, 0xff, 0xd9]);
        Ok(Some(Capture {
            format: self.format.ok_or(DeviceError::Io)?,
            len: 4,
            timestamp_ms: now,
        }))
    }
    fn stop(&mut self) -> Result<(), DeviceError> {
        self.stops.set(self.stops.get() + 1);
        self.format = None;
        Ok(())
    }
    fn discarded_frames(&self) -> Option<u64> {
        Some(3)
    }
}

#[test]
fn capture_continues_offline_and_without_a_viewer() {
    let stops = Rc::new(Cell::new(0));
    let source = Source {
        format: None,
        stops: stops.clone(),
        negotiated_pixels: None,
    };
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut app = LiveCamera::<_, 64, _>::new(
        source,
        listener,
        [0; 64],
        [0; 64],
        [0; 64],
        Options::default(),
    )
    .unwrap();
    app.start(320, 240).unwrap();
    for now in 0..100 {
        let progress = app.poll(now, now >= 50).unwrap();
        assert!(!progress.active && !progress.frame_accepted);
    }
    let stats = app.stats();
    assert_eq!(stats.captured, 100);
    assert_eq!(stats.captured_bytes, 400);
    assert_eq!(stats.max_frame_bytes, 4);
    assert_eq!(stats.camera_discarded, Some(3));
    assert_eq!(stats.stream.accepted, 0);
    app.stop().unwrap();
    assert_eq!(stops.get(), 1);
    assert!(app.poll(100, true).is_err());
}

#[test]
fn non_jpeg_negotiation_stops_before_streaming() {
    let stops = Rc::new(Cell::new(0));
    let source = Source {
        format: None,
        stops: stops.clone(),
        negotiated_pixels: Some(PixelFormat::Gray8),
    };
    let mut app = LiveCamera::<_, 64, _>::new(
        source,
        TcpListener::bind("127.0.0.1:0").unwrap(),
        [0; 64],
        [0; 64],
        [0; 64],
        Options::default(),
    )
    .unwrap();
    assert!(matches!(
        app.start(2, 2),
        Err(camera_stream::Error::Camera(
            camera_service::Error::InvalidFormat
        ))
    ));
    assert_eq!(stops.get(), 1);
    assert!(app.poll(0, true).is_err());
    app.stop().unwrap();
    assert_eq!(stops.get(), 1);
}
