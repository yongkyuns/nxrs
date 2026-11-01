unsafe extern "C" {
    fn execution_current_priority(priority: i32) -> i32;
}
pub(super) fn configure(priority: i32) -> Result<(), crate::Error> {
    // SAFETY: target-header translation affects only the calling thread.
    if unsafe { execution_current_priority(priority) } == 0 {
        Ok(())
    } else {
        Err(crate::Error::Io)
    }
}
