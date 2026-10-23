//! Transport-only fixture; Python independently receives the UDP datagrams.
#[cfg(all(hal_enabled, not(target_os = "nuttx")))]
use transport::Transport;

#[cfg(all(hal_enabled, not(target_os = "nuttx")))]
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

#[cfg(not(all(hal_enabled, not(target_os = "nuttx"))))]
fn main() {
    eprintln!("This example requires the matching desktop OS profile in NXRS_PLATFORM");
    std::process::exit(2);
}
