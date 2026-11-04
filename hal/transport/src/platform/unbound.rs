#[cfg(any(
    target_os = "linux",
    target_os = "macos",
    target_os = "windows",
    target_os = "nuttx"
))]
pub fn transport(_: &str) -> std::io::Result<crate::udp::UdpTransport> {
    Err(std::io::ErrorKind::Unsupported.into())
}
