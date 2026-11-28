use std::{io, time::Duration};
use watchdog::{ArmedWatchdog, DeviceError, RuntimeWatchdog};

// Leave margin for the 3 s collector dial and 10.2 s modem cleanup budgets.
// The tradeoff is a longer reset delay for a genuinely stalled owner.
const TIMEOUT: Duration = Duration::from_secs(30);

pub(super) fn run(
    work: impl FnOnce(Option<&mut dyn RuntimeWatchdog>, &mut bool) -> io::Result<()>,
) -> io::Result<()> {
    if let Ok(cause) = boot::reset_cause() {
        log::info!("motion-tracker reset cause: {cause:?}");
    }
    let mut timer = match watchdog::arm(TIMEOUT) {
        Ok(timer) => Some(timer),
        Err(DeviceError::Unsupported) => None,
        Err(error) => return Err(io::Error::other(format!("arm tracker watchdog: {error:?}"))),
    };
    let mut quiescent = true;
    let result = work(
        timer
            .as_mut()
            .map(|timer| timer as &mut dyn RuntimeWatchdog),
        &mut quiescent,
    );
    if !quiescent {
        log::error!("motion-tracker workers did not stop; watchdog remains armed");
        return result.and(Err(io::Error::other("tracker shutdown incomplete")));
    }
    if let Some(timer) = &mut timer {
        if let Err(error) = timer.disarm() {
            log::error!("motion-tracker disarm watchdog: {error:?}");
            return result.and(Err(io::Error::other("disarm tracker watchdog")));
        }
    }
    result
}

pub(super) fn progress(timer: &mut Option<&mut dyn RuntimeWatchdog>) -> io::Result<()> {
    if let Some(timer) = timer {
        timer
            .feed()
            .map_err(|error| io::Error::other(format!("feed tracker watchdog: {error:?}")))?;
    }
    Ok(())
}
