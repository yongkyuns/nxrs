//! Exercise the production NuttX IMU provider through the test-owned devices.
use imu::{DeviceError, Event, Imu, ImuSample};
use messaging::{bounded, DeliveryError, Inbox, Rejected};
use std::{
    sync::mpsc::TrySendError,
    thread,
    time::{Duration, Instant},
};

unsafe extern "C" {
    fn bmi270_fixture_install() -> i32;
    fn bmi270_fixture_irq_waiting() -> bool;
    fn bmi270_fixture_push(overflow: bool) -> i32;
    fn bmi270_fixture_idle() -> bool;
}

const DEADLINE: Duration = Duration::from_secs(3);
const FRAMES: u64 = 6;

fn open() -> (impl Imu, Inbox<Event>) {
    let (sender, inbox) = bounded(FRAMES as usize);
    let device = imu::open(move |event| {
        sender.try_send(event).map_err(|error| match error {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        })
    })
    .expect("open NuttX BMI270");
    (device, inbox)
}

fn publish(overflow: bool) {
    let end = Instant::now() + DEADLINE;
    // SAFETY: The fixture synchronizes its GPIO and bus state with the worker.
    while !unsafe { bmi270_fixture_irq_waiting() } {
        assert!(Instant::now() < end, "worker did not poll the GPIO");
        thread::sleep(Duration::from_millis(1));
    }
    // SAFETY: Only the test thread publishes. The fixture owns the devices.
    assert_eq!(unsafe { bmi270_fixture_push(overflow) }, 0);
}

fn check_sample(sample: ImuSample, index: u64) {
    let index_f = index as f32;
    let expected_accel = [
        -4096.0 - 256.0 * index_f,
        2048.0 + 256.0 * index_f,
        8192.0 - 512.0 * index_f,
    ];
    let expected_gyro = [
        1024.0 + 128.0 * index_f,
        -2048.0 - 128.0 * index_f,
        512.0 + 64.0 * index_f,
    ];
    assert_eq!(sample.sequence, index + 1);
    assert_eq!(sample.sample_interval_us, 10_000);
    for (value, raw) in sample.accel_mps2.into_iter().zip(expected_accel) {
        assert!((value - raw * (4.0 * 9.80665 / 32768.0)).abs() < 1e-4);
    }
    for (value, raw) in sample.gyro_rps.into_iter().zip(expected_gyro) {
        assert!((value - raw * (2000.0_f32.to_radians() / 32768.0)).abs() < 1e-4);
    }
}

fn finish(device: &mut impl Imu, expected: Result<(), DeviceError>) {
    assert_eq!(device.join(), expected);
    assert_eq!(device.dropped(), 0);
    // SAFETY: The joined worker can no longer access the fixture.
    assert!(unsafe { bmi270_fixture_idle() });
}

fn sample_burst() {
    let (mut device, inbox) = open();
    publish(false);
    for index in 0..FRAMES {
        let Event::Sample(sample) = inbox.wait(Some(DEADLINE)).expect("FIFO sample") else {
            panic!("unexpected IMU fault");
        };
        check_sample(sample, index);
    }
    finish(&mut device, Ok(()));
    assert!(
        inbox.try_recv().is_err(),
        "unexpected event after the FIFO burst"
    );
}

fn main() {
    // SAFETY: The fixture registers test-owned device nodes before opening the HAL.
    assert_eq!(unsafe { bmi270_fixture_install() }, 0);

    sample_burst();
    let (mut device, inbox) = open();
    publish(true);
    assert_eq!(
        inbox.wait(Some(DEADLINE)),
        Ok(Event::Failed(DeviceError::Full))
    );
    finish(&mut device, Err(DeviceError::Full));
    assert_eq!(device.join(), Err(DeviceError::Full));
    sample_burst();

    println!("IMU_QEMU PASS samples=12 overflow=1 reopens=2");
}
