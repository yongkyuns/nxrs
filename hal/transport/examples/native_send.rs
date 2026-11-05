//! PacketSink-only fixture; Python independently receives the UDP datagrams.
use transport::PacketSink;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let peer = std::env::args()
        .nth(1)
        .ok_or("expected numeric peer address")?;
    let mut sender = transport::udp::UdpTransport::connect(peer.parse()?)?;
    for bytes in [&[0, 127, 128, 255][..], b"transport-only"] {
        sender
            .send(bytes)
            .map_err(|error| format!("send: {error:?}"))?;
    }
    println!("TRANSPORT_ONLY_PASS");
    Ok(())
}
