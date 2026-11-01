//! Independent TLS-server witness for the clock-independent exact-pin client.
use super::*;

pub fn run(directory: PathBuf, milliseconds: u64) {
    let identity = tls::open().unwrap();
    let result = exchange(&identity, &directory, milliseconds);
    println!("RESULT {result:?}");
}

pub fn exchange(
    identity: &tls::Identity,
    directory: &std::path::Path,
    milliseconds: u64,
) -> Result<(), tls::Error> {
    let address = std::fs::read_to_string(directory.join("gateway-address")).unwrap();
    let pin: [u8; 32] = public(directory.join("gateway-pin")).try_into().unwrap();
    let socket =
        std::net::TcpStream::connect_timeout(&address.parse().unwrap(), Duration::from_secs(2))
            .unwrap();
    (|| {
        let mut connection =
            identity.connect_pinned(socket, pin, Duration::from_millis(milliseconds))?;
        loop {
            match connection.write(b"credential") {
                Ok(10) => break,
                Ok(_) => return Err(tls::Error::Io),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1))
                }
                Err(_) => return Err(tls::Error::Authentication),
            }
        }
        let mut byte = [0];
        loop {
            match connection.read(&mut byte) {
                Ok(1) if byte == [42] => return Ok(()),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1))
                }
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => return Err(tls::Error::Time),
                _ => return Err(tls::Error::Io),
            }
        }
    })()
}
