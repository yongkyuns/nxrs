//! Profile-selected NuttX IMU factory.
use crate::{session::Session, DeviceError, Event, Imu};
use acquisition::readiness;
use core::sync::atomic::AtomicU8;
use device::{Binding, Device};

mod device;
#[allow(unsafe_code)]
mod ffi;
mod i2c;
mod irq;
mod signal;

static OWNED: AtomicU8 = AtomicU8::new(0);

fn binding() -> Result<Binding<'static>, DeviceError> {
    Ok(Binding {
        path: option_env!("IMU_DEVICE").ok_or(DeviceError::Unsupported)?,
        address: option_env!("IMU_ADDRESS")
            .and_then(|v| v.parse().ok())
            .ok_or(DeviceError::Unsupported)?,
        frequency: option_env!("IMU_FREQUENCY")
            .and_then(|v| v.parse().ok())
            .ok_or(DeviceError::Unsupported)?,
        irq_path: option_env!("IMU_IRQ_DEVICE").filter(|v| !v.is_empty()),
    })
}

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    let device = Device::acquire(binding()?)?;
    let cancel = readiness::Cancel::new(ffi::event_pipe).map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    let stack = option_env!("IMU_STACK_BYTES")
        .and_then(|v| v.parse().ok())
        .ok_or(DeviceError::Unsupported)?;
    Session::start(stack, cancel, sink, move |stop, emit| {
        device.run(stop, &wake, emit)
    })
}
