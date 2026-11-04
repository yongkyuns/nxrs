#[cfg(any(
    target_os = "linux",
    target_os = "macos",
    target_os = "windows",
    target_os = "nuttx"
))]
pub fn storage(
    _: &str,
    _: usize,
    _: usize,
    _: u64,
) -> std::io::Result<crate::recording::FileRecorder> {
    Err(std::io::ErrorKind::Unsupported.into())
}
