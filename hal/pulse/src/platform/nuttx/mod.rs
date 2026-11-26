use crate::{
    runtime::{self, Admission, Input, Lease, Output},
    Error, Event, Session, Waveform,
};
use acquisition::{nuttx, readiness};
use std::{fs::File, num::NonZeroU32, sync::Arc, time::Instant};

static ADMISSION: Admission = Admission::new();

struct PulseOutput {
    output: pwm::Output,
    lease: Arc<Lease>,
}
impl Output for PulseOutput {
    fn configure(&mut self, waveform: Waveform) -> Result<(), Error> {
        if self.lease.is_quarantined() {
            return Err(Error::Shutdown);
        }
        let frequency = NonZeroU32::new(waveform.frequency_hz()).ok_or(Error::InvalidConfig)?;
        let duty = ((u32::from(waveform.duty_percent()) * u32::from(u16::MAX) + 50) / 100) as u16;
        self.output.configure(frequency, duty).map_err(|_| {
            // A failed native start may have partial hardware effects while
            // NuttX still considers the timer stopped. Do not claim recovery.
            self.lease.quarantine();
            Error::Io
        })
    }
    fn disable(&mut self) -> Result<(), Error> {
        self.output.disable().map_err(|_| {
            self.lease.quarantine();
            Error::Shutdown
        })?;
        if self.lease.is_quarantined() {
            Err(Error::Shutdown)
        } else {
            Ok(())
        }
    }
    fn quarantine(&mut self) {
        self.lease.quarantine();
    }
}

struct EdgeInput {
    interrupt: gpio::Interrupt<Arc<Lease>>,
    cancel: Arc<File>,
}
impl Input for EdgeInput {
    fn next(&mut self) -> Result<Option<Instant>, Error> {
        if !readiness::wait(self.interrupt.file(), &self.cancel, nuttx::wait)
            .map_err(|_| Error::Io)?
        {
            return Ok(None);
        }
        let received = Instant::now();
        // GPIOC_READ acknowledges the notification. The pad may already be low
        // by this worker's receipt time; a low level does not erase that edge.
        self.interrupt.read().map_err(|_| Error::Io)?;
        Ok(Some(received))
    }
    fn finish(&mut self) -> Result<(), Error> {
        self.interrupt.unregister().map_err(|_| Error::Shutdown)
    }
}

pub fn open<F: FnMut(Event) -> bool + Send + 'static>(sink: F) -> Result<impl Session, Error> {
    let pwm_device = option_env!("NXRS_PULSE_PWM_DEVICE").ok_or(Error::Unsupported)?;
    let input_device = option_env!("NXRS_PULSE_INPUT_DEVICE").ok_or(Error::Unsupported)?;
    let stack_bytes: usize = option_env!("NXRS_PULSE_STACK_BYTES")
        .ok_or(Error::InvalidConfig)?
        .parse()
        .map_err(|_| Error::InvalidConfig)?;
    if !(8192..=262144).contains(&stack_bytes) {
        return Err(Error::InvalidConfig);
    }
    let lease = ADMISSION.acquire()?;
    let cancel = readiness::Cancel::new(nuttx::pipe).map_err(|_| Error::Io)?;
    let interrupt =
        gpio::Interrupt::open_rising_with_context(input_device, lease.clone(), |lease| {
            lease.quarantine()
        })
        .map_err(|_| Error::Io)?;
    let output = pwm::Output::open(pwm_device).map_err(|_| {
        // Native acquisition includes STOP. If it failed, output-off was not
        // established; the driver's Drop retry cannot report its result here.
        lease.quarantine();
        Error::Shutdown
    })?;
    let source = EdgeInput {
        interrupt,
        cancel: cancel.reader.clone(),
    };
    runtime::start(
        PulseOutput {
            output,
            lease: lease.clone(),
        },
        source,
        cancel,
        lease,
        stack_bytes,
        sink,
    )
}
