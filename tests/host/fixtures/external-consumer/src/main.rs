//! A handwritten product, outside the NXRS workspace and build frontend.
#![forbid(unsafe_code)]

use archive::{recording::FileRecorder, recording::StorageLimits, Storage};
use frames::{Camera, Format, Frame, PixelFormat};
use motion::Imu;
use packets::PacketSink;
use position::Gnss;
use std::{fs, net::UdpSocket, path::PathBuf, sync::mpsc, time::Duration};

fn main() {
    let directory = PathBuf::from(std::env::args().nth(1).expect("output directory"));
    fs::create_dir_all(&directory).unwrap();
    let source = directory.join("frames.raw");
    fs::write(&source, [3, 5, 7, 11]).unwrap();
    let format = Format {
        width: 2,
        height: 2,
        pixels: PixelFormat::Gray8,
    };
    let (mut camera, count) =
        frames::replay::open(source.to_str().unwrap(), format, 10, 4).unwrap();
    assert_eq!(count, 1);
    assert_eq!(camera.start(format).unwrap(), format);
    let mut bytes = [0; 4];
    let capture = camera.poll_frame(0, &mut bytes).unwrap().unwrap();

    let records = directory.join("records");
    let mut recorder = FileRecorder::create_new(
        &records,
        StorageLimits {
            queued_records: 1,
            max_frame_bytes: 4,
            max_records: 1,
        },
    )
    .unwrap();
    recorder
        .append(Frame {
            sequence: 1,
            capture,
            bytes: &bytes,
        })
        .unwrap();
    recorder.finish().unwrap();
    let saved =
        archive::recording::read_record(records.join("00000000000000000001.rcam"), 4).unwrap();
    assert_eq!(saved.bytes, bytes);

    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut sender = packets::udp::UdpTransport::connect(receiver.local_addr().unwrap()).unwrap();
    sender.send(&bytes).unwrap();
    let mut received = [0; 4];
    assert_eq!(receiver.recv(&mut received).unwrap(), 4);
    assert_eq!(received, bytes);
    camera.stop().unwrap();

    if std::env::args().any(|argument| argument == "--sensors") {
        let (send, receive) = mpsc::channel();
        let mut imu = motion::open(move |event| {
            send.send(event).unwrap();
            Ok(())
        })
        .unwrap();
        assert!(matches!(
            receive.recv_timeout(Duration::from_secs(3)).unwrap(),
            motion::Event::Sample(_)
        ));
        imu.request_stop().unwrap();
        imu.join().unwrap();
        assert!(imu.is_finished());

        let (send, receive) = mpsc::channel();
        let mut gnss = position::open(move |event| {
            send.send(event).unwrap();
            Ok(())
        })
        .unwrap();
        assert!(matches!(
            receive.recv_timeout(Duration::from_secs(3)).unwrap(),
            position::Event::Fix(_)
        ));
        gnss.request_stop().unwrap();
        gnss.join().unwrap();
        assert!(gnss.is_finished());
    }

    // Link selected radio contracts/providers without accessing host radios.
    #[cfg(feature = "wifi")]
    assert!(std::mem::size_of::<wireless::Event>() > 0);
    #[cfg(feature = "ble")]
    assert!(std::mem::size_of::<bluetooth::Event>() > 0);
    println!("external consumer: replay, recording, UDP and selected contracts passed");
}
