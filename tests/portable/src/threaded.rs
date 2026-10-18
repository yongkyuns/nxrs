//! Native runner qualification: bounded channels, one device owner, explicit
//! shutdown, disconnect cleanup, and joined workers. No sleep-based tests.
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use nxrs_applications::{Progress, ShutdownReport, TickReport};
use camera_service::CaptureProgress;

use crate::mocks::{CameraAction, MockCamera, MockStorage, MockTransport, FORMAT};
use crate::scenarios::Product;

enum Command {
    Tick(u64),
    Shutdown,
}

enum Reply {
    Tick(TickReport),
    Shutdown(ShutdownReport),
}

fn spawn_owner() -> (SyncSender<Command>, Receiver<Reply>, JoinHandle<Product>) {
    let (commands, command_rx) = sync_channel::<Command>(1);
    let (replies, reply_rx) = sync_channel::<Reply>(1);
    let worker = thread::spawn(move || {
        let mut product = Product::new(
            MockCamera::new((1..=50).map(CameraAction::Frame)),
            MockStorage::default(),
            MockTransport::default(),
        )
        .unwrap();
        product.camera.start(FORMAT).unwrap();
        product.recorder.start(&product.camera).unwrap();
        product.monitor.start(&product.camera).unwrap();
        while let Ok(command) = command_rx.recv() {
            match command {
                Command::Tick(time) => {
                    if replies.send(Reply::Tick(product.step(time))).is_err() {
                        break;
                    }
                }
                Command::Shutdown => {
                    let _ = replies.send(Reply::Shutdown(product.shutdown()));
                    break;
                }
            }
        }
        // A disconnected command OR reply channel is also a shutdown request.
        // This mock runner expects cleanup success; real runners must surface
        // persistent HAL cleanup failures to their supervisor, not forget them.
        let cleanup = product.shutdown();
        assert_eq!(cleanup.recorder, Ok(()));
        assert_eq!(cleanup.camera, Ok(()));
        product
    });
    (commands, reply_rx, worker)
}

pub fn exercise_owner_thread() {
    let (commands, replies, worker) = spawn_owner();
    for sequence in 1..=50 {
        commands.send(Command::Tick(sequence)).unwrap();
        let Reply::Tick(report) = replies.recv_timeout(Duration::from_secs(10)).unwrap() else {
            panic!("unexpected shutdown reply");
        };
        assert_eq!(report.camera, Ok(CaptureProgress::Published(sequence)));
        assert_eq!(
            report.recorder,
            Ok(Progress::Processed { sequence, skipped: 0 })
        );
        assert_eq!(report.monitor, report.recorder);
    }
    commands.send(Command::Shutdown).unwrap();
    let Reply::Shutdown(report) = replies.recv_timeout(Duration::from_secs(10)).unwrap() else {
        panic!("missing shutdown acknowledgement");
    };
    assert_eq!(report.recorder, Ok(()));
    assert_eq!(report.camera, Ok(()));
    let product = worker.join().expect("owner thread panicked");
    assert!(!product.camera.backend().active);
    assert_eq!(product.camera.backend().stops, 1);
    assert_eq!(product.recorder.stats().processed, 50);
    assert_eq!(product.monitor.stats().processed, 50);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_native_worker_acknowledges_shutdown_and_joins() {
        exercise_owner_thread();
    }

    #[test]
    fn disconnected_controller_still_releases_devices() {
        for drop_replies_first in [false, true] {
            let (commands, replies, worker) = spawn_owner();
            commands.send(Command::Tick(1)).unwrap();
            if drop_replies_first {
                drop(replies);
            } else {
                assert!(matches!(
                    replies.recv_timeout(Duration::from_secs(10)).unwrap(),
                    Reply::Tick(_)
                ));
            }
            drop(commands);
            let product = worker.join().expect("disconnected owner panicked");
            assert!(!product.camera.backend().active);
            assert_eq!(product.camera.backend().stops, 1);
            assert_eq!(product.recordings.backend().flushes, 1);
        }
    }
}
