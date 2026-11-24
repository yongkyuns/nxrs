use std::{io, net::SocketAddrV4, time::Duration};

#[derive(Clone, Copy)]
pub struct Endpoint {
    #[cfg_attr(
        not(any(feature = "cellular-uplink", feature = "wifi-map")),
        allow(dead_code)
    )]
    pub address: SocketAddrV4,
    pub pin: [u8; 32],
}

impl Endpoint {
    pub fn parse(address: &str, pin: &str) -> io::Result<Self> {
        let address: SocketAddrV4 = address
            .parse()
            .ok()
            .filter(|address: &SocketAddrV4| {
                address.port() != 0
                    && !address.ip().is_unspecified()
                    && !address.ip().is_multicast()
            })
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "expected IPv4:port"))?;
        let mut digest = [0; 32];
        if pin.len() != 64 || !pin.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "expected 64 hex digits for the TLS pin",
            ));
        }
        for (index, byte) in digest.iter_mut().enumerate() {
            *byte = u8::from_str_radix(&pin[2 * index..2 * index + 2], 16)
                .map_err(|_| io::ErrorKind::InvalidInput)?;
        }
        if digest == [0; 32] {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "empty TLS pin"));
        }
        Ok(Self {
            address,
            pin: digest,
        })
    }
}

#[cfg_attr(not(feature = "cellular-uplink"), allow(dead_code))]
pub struct CellularConfig {
    pub apn: String,
    pub collector: Option<Endpoint>,
    pub interval: Duration,
}
