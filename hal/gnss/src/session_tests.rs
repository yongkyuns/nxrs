//! Task-context admission/lifecycle witnesses; not IRQ or hardware qualification.
use crate::{DeliveryError, DeviceError, Rejected};
use acquisition::{Wake, Worker};
use std::{
    io,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::sync_channel,
        Arc,
    },
    time::{Duration, Instant},
};
#[derive(Default)]
struct Cancel;
impl Wake for Cancel {
    fn wake(&self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn full_measurements_do_not_block_acquisition_and_loss_is_visible_after_join() {
    let (ready, received) = sync_channel(1);
    let mut worker = Worker::start(
        "full-sink",
        16384,
        Cancel,
        move |event: u8| {
            let _ = ready.try_send(());
            Err(Rejected {
                value: event,
                error: DeliveryError::Full,
            })
        },
        |_| 255,
        |_, emit| {
            for value in 0..100 {
                assert!(emit(value));
            }
            Ok(())
        },
    )
    .unwrap();
    received.recv_timeout(Duration::from_secs(1)).unwrap();
    let deadline = Instant::now() + Duration::from_secs(1);
    while !worker.is_finished() {
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert_eq!(worker.join(DeviceError::Io), Ok(()));
    assert_eq!(worker.dropped(), 100);
}
#[test]
fn fault_survives_full_sink_and_repeated_join_even_when_stop_cancels_notification() {
    let (ready, received) = sync_channel(1);
    let admit = Arc::new(AtomicBool::new(false));
    let sink_admit = admit.clone();
    let mut worker = Worker::start(
        "fault-sink",
        16384,
        Cancel,
        move |event: u8| {
            let _ = ready.try_send(());
            if sink_admit.load(Ordering::Acquire) {
                Ok(())
            } else {
                Err(Rejected {
                    value: event,
                    error: DeliveryError::Full,
                })
            }
        },
        |_| 255,
        |_, _| Err(DeviceError::InvalidData),
    )
    .unwrap();
    received.recv_timeout(Duration::from_secs(1)).unwrap();
    assert!(!worker.is_finished());
    worker.request_stop().unwrap();
    assert_eq!(worker.join(DeviceError::Io), Err(DeviceError::InvalidData));
    assert_eq!(worker.join(DeviceError::Io), Err(DeviceError::InvalidData));
    assert!(worker.is_finished());
}
#[test]
fn mock_stop_wakes_its_wait_and_no_delivery_outlives_join() {
    use crate::Session;
    let (sender, receiver) = sync_channel(1);
    let mut session = crate::mock_tests::open(move |event| {
        sender.try_send(event).map_err(|error| Rejected {
            value: match error {
                std::sync::mpsc::TrySendError::Full(value)
                | std::sync::mpsc::TrySendError::Disconnected(value) => value,
            },
            error: DeliveryError::Full,
        })
    })
    .unwrap();
    session.request_stop().unwrap();
    session.join().unwrap();
    assert!(session.is_finished());
    // The producer endpoint has been dropped by the joined worker.
    while receiver.try_recv().is_ok() {}
    assert!(matches!(
        receiver.try_recv(),
        Err(std::sync::mpsc::TryRecvError::Disconnected)
    ));
}

#[test]
fn retained_terminal_event_is_delivered_when_capacity_returns() {
    let (sender, receiver) = sync_channel(1);
    sender.send(0_u8).unwrap();
    let (attempt, attempted) = sync_channel(1);
    let mut worker = Worker::start(
        "fault-retry",
        16384,
        Cancel,
        move |event| {
            sender.try_send(event).map_err(|error| {
                let _ = attempt.try_send(());
                Rejected {
                    value: match error {
                        std::sync::mpsc::TrySendError::Full(value)
                        | std::sync::mpsc::TrySendError::Disconnected(value) => value,
                    },
                    error: DeliveryError::Full,
                }
            })
        },
        |_| 255,
        |_, _| Err(DeviceError::Io),
    )
    .unwrap();
    attempted.recv_timeout(Duration::from_secs(1)).unwrap();
    assert_eq!(receiver.recv().unwrap(), 0);
    assert_eq!(receiver.recv_timeout(Duration::from_secs(1)).unwrap(), 255);
    assert_eq!(worker.join(DeviceError::Io), Err(DeviceError::Io));
}
