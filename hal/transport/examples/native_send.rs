//! Transport-only fixture; Python independently receives the UDP datagrams.
#[cfg(nxrs_hal_platform = "std")]
use transport::Transport;

#[cfg(nxrs_hal_platform = "std")]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let peer = std::env::args()
        .nth(1)
        .ok_or("expected numeric peer address")?;
    let mut sender = transport::transport(&peer)?;
    for bytes in [&[0, 127, 128, 255][..], b"transport-only"] {
        sender
            .send(bytes)
            .map_err(|error| format!("send: {error:?}"))?;
    }
    println!("TRANSPORT_ONLY_PASS");
    Ok(())
}

#[cfg(not(nxrs_hal_platform = "std"))]
fn main() {
    eprintln!("This example requires NXRS_PLATFORM=std");
    std::process::exit(2);
}
