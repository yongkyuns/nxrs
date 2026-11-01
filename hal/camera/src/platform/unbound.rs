pub fn replay(
    _: &str,
    _: crate::Format,
    _: u64,
    _: usize,
) -> std::io::Result<(crate::replay::ReplayCamera, u64)> {
    Err(std::io::ErrorKind::Unsupported.into())
}
