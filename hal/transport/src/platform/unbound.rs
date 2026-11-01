pub fn transport(_: &str) -> std::io::Result<crate::udp::UdpTransport> {
    Err(std::io::ErrorKind::Unsupported.into())
}
