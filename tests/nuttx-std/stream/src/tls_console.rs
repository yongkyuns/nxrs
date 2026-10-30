//! Trusted USB provisioning for the physical camera TLS qualification fixture.

const MAX_ENROLLMENT: usize = 33_792;
const MAX_FIELD: usize = 8_192;
const MAX_NAME: usize = 256;

fn invalid() -> camera_stream::Error {
    camera_stream::Error::Tls(tls::Error::InvalidInput)
}

struct Enrollment {
    policy: tls::CertificatePolicy,
    own: Vec<u8>,
    issuer: Vec<u8>,
    approved_peer: Vec<u8>,
    revocations: Vec<u8>,
}

fn parse_enrollment(blob: &[u8]) -> Result<Enrollment, camera_stream::Error> {
    if blob.len() < 36 || blob.len() > MAX_ENROLLMENT || &blob[..4] != b"NXTE" {
        return Err(invalid());
    }
    let issuer_hash = blob[4..36].try_into().map_err(|_| invalid())?;
    let mut offset: usize = 36;
    let mut field = || -> Result<&[u8], camera_stream::Error> {
        let end = offset.checked_add(2).ok_or_else(invalid)?;
        let length = u16::from_be_bytes(
            blob.get(offset..end)
                .ok_or_else(invalid)?
                .try_into()
                .map_err(|_| invalid())?,
        ) as usize;
        offset = end;
        if length == 0 || length > MAX_FIELD {
            return Err(invalid());
        }
        let end = offset.checked_add(length).ok_or_else(invalid)?;
        let value = blob.get(offset..end).ok_or_else(invalid)?;
        offset = end;
        Ok(value)
    };
    let own_uri = field()?;
    let peer_uri = field()?;
    for name in [own_uri, peer_uri] {
        if name.len() > MAX_NAME || !name.iter().all(|b| (0x21..=0x7e).contains(b)) {
            return Err(invalid());
        }
    }
    let own_uri = std::str::from_utf8(own_uri)
        .map_err(|_| invalid())?
        .to_owned();
    let peer_uri = std::str::from_utf8(peer_uri)
        .map_err(|_| invalid())?
        .to_owned();
    let own = field()?.to_vec();
    let issuer = field()?.to_vec();
    let peer = field()?.to_vec();
    let crl = field()?.to_vec();
    if offset != blob.len() {
        return Err(invalid());
    }
    let policy = tls::CertificatePolicy {
        issuer_sha256: issuer_hash,
        own_uri,
        peer_uri,
    };
    policy.validate().map_err(camera_stream::Error::Tls)?;
    Ok(Enrollment {
        policy,
        own,
        issuer,
        approved_peer: peer,
        revocations: crl,
    })
}

#[derive(Debug, PartialEq, Eq)]
enum Response {
    Time(u64),
    Stop,
}

fn parse_time_response(bytes: &[u8], sequence: u64) -> Result<Response, tls::Error> {
    if bytes.len() != 20 {
        return Err(tls::Error::Time);
    }
    if u64::from_be_bytes(bytes[4..12].try_into().map_err(|_| tls::Error::Time)?) != sequence {
        return Err(tls::Error::Time);
    }
    let value = u64::from_be_bytes(bytes[12..20].try_into().map_err(|_| tls::Error::Time)?);
    match (&bytes[..4], value) {
        (b"NXT1", value) => Ok(Response::Time(value)),
        (b"NXS1", 0) => Ok(Response::Stop),
        _ => Err(tls::Error::Time),
    }
}

#[cfg(target_os = "nuttx")]
mod device {
    use super::*;
    use camera_stream::TlsIdentity;
    use std::{
        io::{Read, Write},
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        },
        time::{Duration, Instant},
    };

    struct Raw([u32; 6]);
    impl Raw {
        fn enter() -> Result<Self, camera_stream::Error> {
            let mut saved = Self([0; 6]);
            // SAFETY: target-header translation saves the six termios fields before
            // switching this TLS worker's standard input to raw mode.
            if unsafe { nxrs_detector_console_mode(saved.0.as_mut_ptr(), 1) } != 0 {
                return Err(camera_stream::Error::Tls(tls::Error::Unsupported));
            }
            Ok(saved)
        }
    }
    impl Drop for Raw {
        fn drop(&mut self) {
            // SAFETY: restore only the termios fields captured by this owner.
            let _ = unsafe { nxrs_detector_console_mode(self.0.as_mut_ptr(), 0) };
        }
    }

    unsafe extern "C" {
        fn nxrs_detector_console_mode(saved: *mut u32, raw: i32) -> i32;
        fn nxrs_stream_console_ready() -> i32;
        fn nxrs_stream_worker_stack(values: *mut u32) -> i32;
    }

    fn read_exact(
        input: &mut impl Read,
        bytes: &mut [u8],
        deadline: Instant,
    ) -> Result<(), camera_stream::Error> {
        let mut offset = 0;
        while offset < bytes.len() {
            if Instant::now() >= deadline {
                return Err(camera_stream::Error::Tls(tls::Error::Time));
            }
            // SAFETY: target-header readiness observes this task's standard input.
            match unsafe { nxrs_stream_console_ready() } {
                n if n < 0 => return Err(camera_stream::Error::Tls(tls::Error::Unsupported)),
                0 => std::thread::sleep(Duration::from_millis(1)),
                _ => match input.read(&mut bytes[offset..]) {
                    Ok(0) => return Err(camera_stream::Error::Tls(tls::Error::Unsupported)),
                    Ok(n) => offset += n,
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                    Err(_) => return Err(camera_stream::Error::Tls(tls::Error::Unsupported)),
                },
            }
        }
        Ok(())
    }

    pub fn initialize(
        stop: Arc<AtomicBool>,
    ) -> Result<TlsIdentity<impl FnMut() -> Result<tls::TimeLease, tls::Error>>, camera_stream::Error>
    {
        let _raw = Raw::enter()?;
        let mut input = std::io::stdin();
        let mut output = std::io::stdout();
        let mut identity = tls::open().map_err(camera_stream::Error::Tls)?;
        let mut csr = [0; 1024];
        let csr_len = identity
            .request("camera", &mut csr)
            .map_err(camera_stream::Error::Tls)?;
        write!(output, "NXRS_TLS_CSR bytes={csr_len}\n").map_err(|_| invalid())?;
        output.write_all(&csr[..csr_len]).map_err(|_| invalid())?;
        output.flush().map_err(|_| invalid())?;

        writeln!(output, "NXRS_TLS_ENROLL_READY max={MAX_ENROLLMENT}").map_err(|_| invalid())?;
        output.flush().map_err(|_| invalid())?;
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut length = [0; 4];
        read_exact(&mut input, &mut length, deadline)?;
        let length = u32::from_be_bytes(length) as usize;
        if !(36..=MAX_ENROLLMENT).contains(&length) {
            return Err(invalid());
        }
        let mut blob = vec![0; length];
        let mut offset = 0;
        loop {
            writeln!(output, "NXRS_TLS_INPUT_ACK offset={offset}").map_err(|_| invalid())?;
            output.flush().map_err(|_| invalid())?;
            if offset == length {
                break;
            }
            let end = (offset + 32).min(length);
            read_exact(&mut input, &mut blob[offset..end], deadline)?;
            offset = end;
        }
        let enrollment = parse_enrollment(&blob)?;

        let mut sequence = 0u64;
        let raw = _raw;
        let time = move || {
            let _keep_console_raw = &raw;
            sequence = sequence.checked_add(1).ok_or(tls::Error::Time)?;
            let started = Instant::now();
            let mut stdout = std::io::stdout();
            writeln!(stdout, "NXRS_TLS_TIME_REQUEST sequence={sequence}")
                .and_then(|_| stdout.flush())
                .map_err(|_| tls::Error::Time)?;
            if sequence > 1 {
                let mut stack = [0u32; 2];
                // SAFETY: scalar observation reports this TLS worker's configured stack.
                if unsafe { nxrs_stream_worker_stack(stack.as_mut_ptr()) } == 0 {
                    writeln!(
                        stdout,
                        "NXRS_TLS_STACK budget={} used={}",
                        stack[0], stack[1]
                    )
                    .and_then(|_| stdout.flush())
                    .map_err(|_| tls::Error::Time)?;
                }
            }
            let deadline = started + Duration::from_secs(1);
            let mut response = [0; 20];
            read_exact(&mut std::io::stdin(), &mut response, deadline)
                .map_err(|_| tls::Error::Time)?;
            let observed = Instant::now();
            let uncertainty = observed.duration_since(started);
            if uncertainty >= Duration::from_secs(1) {
                return Err(tls::Error::Time);
            }
            let unix_seconds = match parse_time_response(&response, sequence)? {
                Response::Time(value) => value,
                Response::Stop => {
                    stop.store(true, Ordering::Release);
                    return Err(tls::Error::Time);
                }
            };
            tls::TimeLease::from_trusted_with_uncertainty(
                unix_seconds,
                Duration::from_secs(30),
                observed,
                uncertainty,
            )
        };
        Ok(TlsIdentity {
            identity,
            policy: enrollment.policy,
            own: enrollment.own,
            issuer: enrollment.issuer,
            approved_peer: enrollment.approved_peer,
            revocations: enrollment.revocations,
            time,
        })
    }
}

#[cfg(target_os = "nuttx")]
pub use device::initialize;

#[cfg(test)]
mod tests {
    use super::*;

    fn field(blob: &mut Vec<u8>, bytes: &[u8]) {
        blob.extend_from_slice(&(bytes.len() as u16).to_be_bytes());
        blob.extend_from_slice(bytes);
    }

    fn enrollment(name: &[u8], fields: [&[u8]; 4]) -> Vec<u8> {
        let mut blob = b"NXTE".to_vec();
        blob.extend_from_slice(&[7; 32]);
        field(&mut blob, name);
        field(&mut blob, b"urn:gateway");
        for value in fields {
            field(&mut blob, value);
        }
        blob
    }

    #[test]
    fn enrollment_and_time_parsers_enforce_bounds_and_exact_identity() {
        let blob = enrollment(b"urn:camera", [b"own", b"issuer", b"peer", b"crl"]);
        let parsed = parse_enrollment(&blob).unwrap();
        assert_eq!(parsed.policy.issuer_sha256, [7; 32]);
        assert_eq!(parsed.policy.own_uri, "urn:camera");
        assert_eq!(parsed.policy.peer_uri, "urn:gateway");
        assert_eq!(parsed.own, b"own");
        assert_eq!(parsed.issuer, b"issuer");
        assert_eq!(parsed.approved_peer, b"peer");
        assert_eq!(parsed.revocations, b"crl");

        let empty_field = {
            let mut bad = b"NXTE".to_vec();
            bad.extend_from_slice(&[7; 32]);
            field(&mut bad, b"");
            bad
        };
        let too_long_name = enrollment(&vec![b'a'; 257], [b"own", b"issuer", b"peer", b"crl"]);
        let non_ascii_name = enrollment(b"urn:\xff", [b"own", b"issuer", b"peer", b"crl"]);
        let oversized_field = enrollment(b"urn:camera", [&vec![1; 8193], b"i", b"p", b"c"]);
        for malformed in [
            &blob[..35],
            &blob[..blob.len() - 1],
            &[&blob[..], b"x"].concat(),
            &empty_field,
            &too_long_name,
            &non_ascii_name,
            &oversized_field,
        ] {
            assert!(parse_enrollment(malformed).is_err());
        }

        let mut response = b"NXT1".to_vec();
        response.extend_from_slice(&4u64.to_be_bytes());
        response.extend_from_slice(&1_800_000_000u64.to_be_bytes());
        assert_eq!(
            parse_time_response(&response, 4),
            Ok(Response::Time(1_800_000_000))
        );
        assert_eq!(parse_time_response(&response, 5), Err(tls::Error::Time));
        response[..4].copy_from_slice(b"NXS1");
        assert_eq!(parse_time_response(&response, 4), Err(tls::Error::Time));
        response[12..].copy_from_slice(&0u64.to_be_bytes());
        assert_eq!(parse_time_response(&response, 4), Ok(Response::Stop));
        assert_eq!(parse_time_response(&response, 5), Err(tls::Error::Time));
        assert_eq!(
            parse_time_response(&response[..19], 4),
            Err(tls::Error::Time)
        );
        response[0] = b'X';
        assert_eq!(parse_time_response(&response, 4), Err(tls::Error::Time));
    }
}
