//! NuttX I2C resource binding. BMI270 protocol and units remain in Rust.
use crate::{DeviceError, Imu, ImuSample};
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
    _lease: Lease,
}
impl Imu for Device {
    fn sample(&mut self, now_ms: u64) -> Result<ImuSample, DeviceError> {
        self.sensor.sample(now_ms)
    }
}
impl Drop for Device {
    fn drop(&mut self) {
        if self.sensor.stop().is_err() {
            OWNED.store(2, Ordering::Release);
        }
    }
}
pub fn open() -> Result<impl Imu + Send + 'static, DeviceError> {
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
        _lease: lease,
    })
}
