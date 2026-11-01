//! Bounded camera/gateway bootstrap records carried inside exact-pin TLS.
#![forbid(unsafe_code)]
use std::io::{self, Read, Write};

pub const MAGIC: &[u8; 4] = b"NXB1";
pub const MAX_CSR: usize = 1024;
pub const MAX_ENROLLMENT: usize = 33792;

fn invalid() -> io::Error {
    io::ErrorKind::InvalidData.into()
}

/// One independently commissioned camera. Never serialize this with Debug.
pub struct Commission {
    pub gateway: std::net::SocketAddrV4,
    pub certificate_sha256: [u8; 32],
    pub token: [u8; 32],
    pub site: String,
    pub camera: String,
    pub gateway_id: String,
}
impl Commission {
    pub const MAX_BYTES: usize = 4 + 6 + 64 + 3 * 65;
    pub fn encode(&self) -> io::Result<Vec<u8>> {
        if self.gateway.port() == 0
            || !(self.gateway.ip().is_private() || self.gateway.ip().is_loopback())
            || self.certificate_sha256 == [0; 32]
            || self.token == [0; 32]
        {
            return Err(invalid());
        }
        let mut out = b"NXC1".to_vec();
        out.extend_from_slice(&self.gateway.ip().octets());
        out.extend_from_slice(&self.gateway.port().to_be_bytes());
        out.extend_from_slice(&self.certificate_sha256);
        out.extend_from_slice(&self.token);
        for name in [&self.site, &self.camera, &self.gateway_id] {
            if !identifier(name) {
                return Err(invalid());
            }
            out.push(name.len() as u8);
            out.extend_from_slice(name.as_bytes());
        }
        Ok(out)
    }
    pub fn decode(mut bytes: &[u8]) -> io::Result<Self> {
        let original = bytes;
        if bytes.len() < 74 || bytes.len() > Self::MAX_BYTES || take(&mut bytes, 4)? != b"NXC1" {
            return Err(invalid());
        }
        let ip = <[u8; 4]>::try_from(take(&mut bytes, 4)?).map_err(|_| invalid())?;
        let port = u16::from_be_bytes(take(&mut bytes, 2)?.try_into().unwrap());
        let certificate_sha256 = take(&mut bytes, 32)?.try_into().unwrap();
        let token = take(&mut bytes, 32)?.try_into().unwrap();
        let mut name = || -> io::Result<String> {
            let length = take(&mut bytes, 1)?[0] as usize;
            String::from_utf8(take(&mut bytes, length)?.to_vec()).map_err(|_| invalid())
        };
        let result = Self {
            gateway: std::net::SocketAddrV4::new(std::net::Ipv4Addr::from(ip), port),
            certificate_sha256,
            token,
            site: name()?,
            camera: name()?,
            gateway_id: name()?,
        };
        if !bytes.is_empty() || result.encode()? != original {
            return Err(invalid());
        }
        Ok(result)
    }
    pub fn own_uri(&self) -> String {
        format!("urn:nxrs:camera:{}:{}", self.site, self.camera)
    }
    pub fn peer_uri(&self) -> String {
        format!(
            "urn:nxrs:ingest:{}:{}:{}",
            self.site, self.gateway_id, self.camera
        )
    }
}
pub fn identifier(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}
fn take<'a>(bytes: &mut &'a [u8], length: usize) -> io::Result<&'a [u8]> {
    let value = bytes.get(..length).ok_or_else(invalid)?;
    *bytes = &bytes[length..];
    Ok(value)
}

/// Public enrollment material; identities are checked against commissioning.
pub struct Enrollment {
    pub issuer_sha256: [u8; 32],
    pub own_uri: String,
    pub peer_uri: String,
    pub own: Vec<u8>,
    pub issuer: Vec<u8>,
    pub approved_peer: Vec<u8>,
    pub revocations: Vec<u8>,
}
impl Enrollment {
    pub fn encode(&self) -> io::Result<Vec<u8>> {
        if [&self.own_uri, &self.peer_uri]
            .iter()
            .any(|s| s.len() > 256 || !s.bytes().all(|b| (0x21..=0x7e).contains(&b)))
        {
            return Err(invalid());
        }
        let mut out = b"NXTE".to_vec();
        out.extend_from_slice(&self.issuer_sha256);
        for field in [
            self.own_uri.as_bytes(),
            self.peer_uri.as_bytes(),
            &self.own,
            &self.issuer,
            &self.approved_peer,
            &self.revocations,
        ] {
            if field.is_empty() || field.len() > 8192 {
                return Err(invalid());
            }
            out.extend_from_slice(&(field.len() as u16).to_be_bytes());
            out.extend_from_slice(field);
        }
        if out.len() > MAX_ENROLLMENT {
            return Err(invalid());
        }
        Ok(out)
    }
    pub fn decode(bytes: &[u8]) -> io::Result<Self> {
        if !(48..=MAX_ENROLLMENT).contains(&bytes.len()) {
            return Err(invalid());
        }
        let mut input = bytes;
        if take(&mut input, 4)? != b"NXTE" {
            return Err(invalid());
        }
        let issuer_sha256 = take(&mut input, 32)?.try_into().unwrap();
        let mut field = || -> io::Result<Vec<u8>> {
            let n = u16::from_be_bytes(take(&mut input, 2)?.try_into().unwrap()) as usize;
            if n == 0 || n > 8192 {
                return Err(invalid());
            }
            Ok(take(&mut input, n)?.to_vec())
        };
        let mut name = || -> io::Result<String> {
            let bytes = field()?;
            if bytes.len() > 256 || !bytes.iter().all(|b| (0x21..=0x7e).contains(b)) {
                return Err(invalid());
            }
            String::from_utf8(bytes).map_err(|_| invalid())
        };
        let result = Self {
            issuer_sha256,
            own_uri: name()?,
            peer_uri: name()?,
            own: field()?,
            issuer: field()?,
            approved_peer: field()?,
            revocations: field()?,
        };
        if !input.is_empty() {
            return Err(invalid());
        }
        Ok(result)
    }
}

pub fn request(output: &mut impl Write, token: &[u8; 32], csr: &[u8]) -> io::Result<()> {
    if csr.len() > MAX_CSR {
        return Err(invalid());
    }
    output.write_all(MAGIC)?;
    output.write_all(token)?;
    output.write_all(&(csr.len() as u16).to_be_bytes())?;
    output.write_all(csr)
}
pub fn response(output: &mut impl Write, unix_seconds: u64, enrollment: &[u8]) -> io::Result<()> {
    if enrollment.len() > MAX_ENROLLMENT {
        return Err(invalid());
    }
    output.write_all(MAGIC)?;
    output.write_all(&unix_seconds.to_be_bytes())?;
    output.write_all(&(enrollment.len() as u16).to_be_bytes())?;
    output.write_all(enrollment)
}
pub fn read_response(input: &mut impl Read) -> io::Result<(u64, Vec<u8>)> {
    let mut header = [0; 14];
    input.read_exact(&mut header)?;
    if &header[..4] != MAGIC {
        return Err(invalid());
    }
    let seconds = u64::from_be_bytes(header[4..12].try_into().unwrap());
    let n = u16::from_be_bytes(header[12..14].try_into().unwrap()) as usize;
    if n > MAX_ENROLLMENT {
        return Err(invalid());
    }
    let mut bytes = vec![0; n];
    input.read_exact(&mut bytes)?;
    Ok((seconds, bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn commissioning_is_bounded_and_never_accepts_partial_or_trailing_records() {
        let value = Commission {
            gateway: "192.168.1.3:8444".parse().unwrap(),
            certificate_sha256: [1; 32],
            token: [2; 32],
            site: "s".into(),
            camera: "c".into(),
            gateway_id: "g".into(),
        };
        let bytes = value.encode().unwrap();
        assert_eq!(
            &bytes[..10],
            &[b'N', b'X', b'C', b'1', 192, 168, 1, 3, 0x20, 0xfc]
        );
        assert_eq!(Commission::decode(&bytes).unwrap().encode().unwrap(), bytes);
        for end in 0..bytes.len() {
            assert!(Commission::decode(&bytes[..end]).is_err());
        }
        let mut trailing = bytes.clone();
        trailing.push(0);
        assert!(Commission::decode(&trailing).is_err());
        let mut zero = bytes.clone();
        zero[42..74].fill(0);
        assert!(Commission::decode(&zero).is_err());
    }
    #[test]
    fn enrollment_and_time_response_enforce_exact_fields_and_bounds() {
        let value = Enrollment {
            issuer_sha256: [3; 32],
            own_uri: "urn:c".into(),
            peer_uri: "urn:g".into(),
            own: vec![1],
            issuer: vec![2],
            approved_peer: vec![3],
            // Preserve the pre-existing USB envelope's 8 KiB per-DER bound.
            revocations: vec![4; 8192],
        };
        let bytes = value.encode().unwrap();
        assert_eq!(Enrollment::decode(&bytes).unwrap().peer_uri, "urn:g");
        for end in 0..bytes.len() {
            assert!(Enrollment::decode(&bytes[..end]).is_err());
        }
        let mut oversized = b"NXB1".to_vec();
        oversized.extend_from_slice(&123u64.to_be_bytes());
        oversized.extend_from_slice(&u16::MAX.to_be_bytes());
        assert!(read_response(&mut oversized.as_slice()).is_err());
        let mut output = vec![];
        response(&mut output, 123, &bytes).unwrap();
        assert_eq!(read_response(&mut output.as_slice()).unwrap(), (123, bytes));
        assert!(request(&mut vec![], &[1; 32], &vec![0; MAX_CSR + 1]).is_err());
    }
}
