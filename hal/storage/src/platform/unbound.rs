pub fn storage(
    _: &str,
    _: usize,
    _: usize,
    _: u64,
) -> std::io::Result<crate::recording::FileRecorder> {
    Err(std::io::ErrorKind::Unsupported.into())
}
