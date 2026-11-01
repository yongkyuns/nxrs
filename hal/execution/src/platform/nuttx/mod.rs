#[allow(unsafe_code)]
mod ffi;
pub fn configure_current(priority: crate::Priority) -> Result<(), crate::Error> {
    ffi::configure(match priority {
        crate::Priority::Background => env!("EXECUTION_BACKGROUND_PRIORITY")
            .parse()
            .map_err(|_| crate::Error::Io)?,
        crate::Priority::Normal => env!("EXECUTION_NORMAL_PRIORITY")
            .parse()
            .map_err(|_| crate::Error::Io)?,
    })
}
