//! NuttX I2C resource binding. BMI270 protocol and units remain in Rust.
use crate::acquisition::Worker;
use crate::{DeviceError, Event, EventSink, Imu};
#[path = "../../../../../driver/nuttx/readiness.rs"]
mod readiness;
use core::sync::atomic::{AtomicU8, Ordering};
use embedded_hal::{
    delay::DelayNs,
    i2c::{ErrorKind, ErrorType, I2c, Operation},
};
use std::{
    fs::{File, OpenOptions},
    os::fd::AsRawFd,
    time::Duration,
};
#[allow(unsafe_code)]
mod ffi;
mod irq;
mod signal;
static OWNED: AtomicU8 = AtomicU8::new(0);
struct Lease;
impl Drop for Lease {
    fn drop(&mut self) {
        let _ = OWNED.compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}
#[derive(Default)]
struct Delay;
impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}
struct Bus {
    file: File,
    frequency: u32,
}
impl ErrorType for Bus {
    type Error = ErrorKind;
}
impl I2c for Bus {
    fn transaction(
        &mut self,
        address: u8,
        operations: &mut [Operation<'_>],
    ) -> Result<(), Self::Error> {
        let empty = &[][..];
        let mut no_read = [];
        let (write, read): (&[u8], &mut [u8]) = match operations {
            [Operation::Write(bytes)] => (bytes, &mut no_read),
            [Operation::Read(bytes)] => (empty, bytes),
            [Operation::Write(write), Operation::Read(read)] => (write, read),
            [] => return Ok(()),
            _ => return Err(ErrorKind::Other),
        };
        if write.len() > 64 || read.len() > 64 {
            return Err(ErrorKind::Other);
        }
        ffi::transfer(self.file.as_raw_fd(), address, self.frequency, write, read)
            .map_err(|_| ErrorKind::Other)
    }
}
struct Device {
    sensor: crate::bmi270::Sensor<Bus, Delay>,
    signal: signal::Signal,
    cleaned: bool,
    _lease: Lease,
}
impl Device {
    fn cleanup(&mut self) -> Result<(), DeviceError> {
        if self.cleaned {
            return Ok(());
        }
        self.cleaned = true;
        let stopped = self.sensor.stop();
        let detached = self.signal.close();
        if stopped.is_err() || detached.is_err() {
            OWNED.store(2, Ordering::Release);
        }
        stopped.and(detached)
    }
}
impl Drop for Device {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}
fn acquire() -> Result<Device, DeviceError> {
    // An omitted IRQ resource explicitly selects ODR-derived wakes. A configured
    // IRQ that cannot be acquired remains an error; it never changes modes.
    let irq_path = option_env!("IMU_IRQ_DEVICE").filter(|v| !v.is_empty());
    OWNED
        .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|state| {
            if state == 1 {
                DeviceError::Busy
            } else {
                DeviceError::Io
            }
        })?;
    let lease = Lease;
    let path = option_env!("IMU_DEVICE").ok_or(DeviceError::Unsupported)?;
    let address = option_env!("IMU_ADDRESS")
        .and_then(|v| v.parse::<u8>().ok())
        .ok_or(DeviceError::Unsupported)?;
    let frequency = option_env!("IMU_FREQUENCY")
        .and_then(|v| v.parse::<u32>().ok())
        .ok_or(DeviceError::Unsupported)?;
    let irq = irq_path
        .map(|path| {
            let file = OpenOptions::new().read(true).open(path).map_err(|error| {
                if error.kind() == std::io::ErrorKind::NotFound {
                    DeviceError::Unsupported
                } else {
                    DeviceError::Io
                }
            })?;
            irq::Irq::register(file)
        })
        .transpose()?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .map_err(|_| DeviceError::Io)?;
    let sensor =
        crate::bmi270::Sensor::open(Bus { file, frequency }, address).map_err(|error| {
            // An interrupted configuration may have left the sensor active. Do not
            // admit a second owner when its final state was not verified.
            OWNED.store(2, Ordering::Release);
            error
        })?;
    Ok(Device {
        sensor,
        signal: signal::Signal::new(irq, crate::bmi270::FIFO_WAKE_PERIOD),
        cleaned: false,
        _lease: lease,
    })
}

struct Session(Worker<readiness::Cancel, DeviceError>);
impl Imu for Session {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        self.0.request_stop().map_err(|_| DeviceError::Io)
    }
    fn join(&mut self) -> Result<(), DeviceError> {
        self.request_stop()?;
        self.0.join(DeviceError::Io)
    }
    fn is_finished(&self) -> bool {
        self.0.is_finished()
    }
    fn dropped(&self) -> u32 {
        self.0.dropped()
    }
}
pub fn open<S: EventSink<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    let mut device = acquire()?;
    let cancel = readiness::Cancel::new().map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    let stack = option_env!("IMU_STACK_BYTES")
        .and_then(|v| v.parse().ok())
        .ok_or(DeviceError::Unsupported)?;
    Worker::start(
        "imu-acquire",
        stack,
        cancel,
        sink,
        Event::Failed,
        move |stop, emit| {
            let origin = std::time::Instant::now();
            let result = (|| {
                while !stop.load(Ordering::Acquire) {
                    if !device.signal.wait(&wake)? {
                        break;
                    }
                    let now = origin.elapsed().as_millis().try_into().unwrap_or(u64::MAX);
                    let Some(count) = device
                        .sensor
                        .drain(now, |sample| emit(Event::Sample(sample)))?
                    else {
                        break;
                    };
                    device.signal.drained(count)?;
                }
                Ok(())
            })();
            device.cleanup().and(result)
        },
    )
    .map(Session)
    .map_err(|_| DeviceError::Io)
}
