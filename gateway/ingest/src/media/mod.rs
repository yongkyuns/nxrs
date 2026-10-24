//! Single-use RTSP remux helpers. Neither helper decodes or encodes an image.
//! Its supervisor must bound startup, packet progress, cancellation and reaping.
#![deny(unsafe_code)]
#[allow(unsafe_code)]
mod ffi;

use std::{
    io::{Read, Write},
    net::SocketAddrV4,
    os::unix::fs::FileTypeExt,
};
use zeroize::Zeroizing;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    Configuration,
    Credential,
    Media,
    Output,
}
type Result<T> = std::result::Result<T, Failure>;

fn identifier(text: &str) -> bool {
    (1..=64).contains(&text.len())
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn target(args: &[String]) -> Result<(SocketAddrV4, &str, &str)> {
    if args.len() != 3 {
        return Err(Failure::Configuration);
    }
    let address: SocketAddrV4 = args[0].parse().map_err(|_| Failure::Configuration)?;
    if !address.ip().is_loopback()
        || address.port() == 0
        || !identifier(&args[1])
        || !identifier(&args[2])
    {
        return Err(Failure::Configuration);
    }
    Ok((address, &args[1], &args[2]))
}
fn credential<R: Read>(input: &mut R) -> Result<Zeroizing<[u8; 64]>> {
    let mut bytes = Zeroizing::new([0; 64]);
    input
        .read_exact(bytes.as_mut())
        .map_err(|_| Failure::Credential)?;
    if !bytes
        .iter()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
    {
        return Err(Failure::Credential);
    }
    Ok(bytes)
}
/// Establish the native clock within 32 skipped packets, then wait at most
/// another 32 packets for a timed keyframe. Clock loss after establishment
/// retires the source, including while waiting for that first keyframe.
/// The supervisor's existing startup deadline spans both phases.
enum PacketGate {
    Clock(u8),
    Keyframe(u8),
    Started,
}
impl Default for PacketGate {
    fn default() -> Self {
        Self::Clock(0)
    }
}
impl PacketGate {
    fn admit(&mut self, timed: bool, keyframe: bool) -> Result<bool> {
        match self {
            Self::Clock(skipped) if !timed => {
                if *skipped >= 32 {
                    return Err(Failure::Media);
                }
                *skipped += 1;
                Ok(false)
            }
            Self::Clock(_) if keyframe => {
                *self = Self::Started;
                Ok(true)
            }
            Self::Clock(_) => {
                *self = Self::Keyframe(1);
                Ok(false)
            }
            Self::Keyframe(_) | Self::Started if !timed => Err(Failure::Media),
            Self::Keyframe(_) if keyframe => {
                *self = Self::Started;
                Ok(true)
            }
            Self::Keyframe(skipped) => {
                if *skipped >= 32 {
                    return Err(Failure::Media);
                }
                *skipped += 1;
                Ok(false)
            }
            Self::Started => Ok(true),
        }
    }
}
pub fn publish(args: &[String]) -> Result<()> {
    let (address, user, path) = target(args)?;
    // Read exactly the 64-byte preamble without Rust Stdin's buffering: libavformat
    // subsequently owns reads from pipe:0. No TS byte may be prefetched and lost.
    // /dev/stdin is deliberately the inherited descriptor, never a config path.
    let mut input = std::fs::File::open("/dev/stdin").map_err(|_| Failure::Credential)?;
    if !input
        .metadata()
        .map_err(|_| Failure::Credential)?
        .file_type()
        .is_fifo()
    {
        return Err(Failure::Credential);
    }
    let token = credential(&mut input)?;
    drop(input);
    let token = std::str::from_utf8(token.as_ref()).map_err(|_| Failure::Credential)?;
    let url = Zeroizing::new(format!("rtsp://{user}:{token}@{address}/{path}"));
    // No SDK diagnostic may print a URL, credential, SDP or source payload.
    ffi::quiet();
    let mut source = ffi::Source::open()?;
    let mut output = ffi::Publication::open(&url, &source)?;
    let mut count = 0u64;
    while source.next()? {
        if !source.has_timestamps() {
            return Err(Failure::Media);
        }
        output.write(&mut source)?;
        count = count.checked_add(1).ok_or(Failure::Media)?;
        // Progress confirms successful SDK publication, not encoder activity.
        writeln!(std::io::stdout(), "packet={count}").map_err(|_| Failure::Output)?;
        std::io::stdout().flush().map_err(|_| Failure::Output)?;
    }
    output.finish()
}
/// Read only the RTSP credential preamble. The output is compressed MPEG-TS,
/// never a credentialed URL/playlist or decoded image.
pub fn read_rtsp(args: &[String]) -> Result<()> {
    let (address, user, path) = target(args)?;
    let mut input = std::fs::File::open("/dev/stdin").map_err(|_| Failure::Credential)?;
    if !input
        .metadata()
        .map_err(|_| Failure::Credential)?
        .file_type()
        .is_fifo()
    {
        return Err(Failure::Credential);
    }
    let token = credential(&mut input)?;
    drop(input);
    let token = std::str::from_utf8(token.as_ref()).map_err(|_| Failure::Credential)?;
    let url = Zeroizing::new(format!("rtsp://{user}:{token}@{address}/{path}"));
    ffi::quiet();
    let mut source = ffi::Source::rtsp(&url)?;
    let mut output = ffi::PipeOutput::open(&source)?;
    let mut count = 0u64;
    let mut admission = PacketGate::default();
    while source.next()? {
        // Do not invent capture times. Parent deadlines remain in force while
        // waiting for the native RTP clock; a later loss retires this source.
        if !admission.admit(source.has_timestamps(), source.is_keyframe())? {
            continue;
        }
        output.write(&mut source)?;
        count = count.checked_add(1).ok_or(Failure::Media)?;
        writeln!(std::io::stderr(), "packet={count}").map_err(|_| Failure::Output)?;
        std::io::stderr().flush().map_err(|_| Failure::Output)?;
    }
    if count == 0 {
        return Err(Failure::Media);
    }
    output.finish()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn packet_admission_preserves_clock_bound_and_loss_refusal() {
        let mut gate = PacketGate::default();
        for _ in 0..32 {
            assert_eq!(gate.admit(false, true), Ok(false));
        }
        assert_eq!(gate.admit(false, true), Err(Failure::Media));
        let mut gate = PacketGate::default();
        assert_eq!(gate.admit(false, true), Ok(false));
        assert_eq!(gate.admit(true, false), Ok(false));
        assert_eq!(gate.admit(false, true), Err(Failure::Media));
        let mut gate = PacketGate::default();
        assert_eq!(gate.admit(true, true), Ok(true));
        assert_eq!(gate.admit(true, false), Ok(true));
        assert_eq!(gate.admit(false, true), Err(Failure::Media));
    }
    #[test]
    fn first_keyframe_wait_has_its_own_bounded_prefix_after_clock_establishment() {
        let mut gate = PacketGate::default();
        for _ in 0..32 {
            assert_eq!(gate.admit(true, false), Ok(false));
        }
        assert_eq!(gate.admit(true, false), Err(Failure::Media));
        // Native RTP clock establishment may miss an earlier untimed keyframe.
        let mut gate = PacketGate::default();
        for _ in 0..32 {
            assert_eq!(gate.admit(false, true), Ok(false));
        }
        for _ in 0..32 {
            assert_eq!(gate.admit(true, false), Ok(false));
        }
        assert_eq!(gate.admit(true, true), Ok(true));
        assert_eq!(gate.admit(true, false), Ok(true));
    }
    #[test]
    fn credential_consumes_exactly_the_preamble() {
        let data = [vec![b'a'; 64], vec![0x47; 188]].concat();
        let mut input = data.as_slice();
        assert_eq!(credential(&mut input).unwrap().as_ref(), &[b'a'; 64]);
        assert_eq!(input, &[0x47; 188]);
        for invalid in [
            vec![],
            vec![b'a'; 63],
            vec![b'G'; 64],
            vec![b'\n'; 64],
            vec![0; 64],
        ] {
            assert_eq!(
                credential(&mut invalid.as_slice()).unwrap_err(),
                Failure::Credential
            );
        }
    }
    #[test]
    fn only_public_loopback_targets_and_literal_identifiers() {
        let good = ["127.0.0.1:8554", "publisher_one", "camera_one"].map(String::from);
        assert!(target(&good).is_ok());
        for address in [
            "localhost:8554",
            "192.168.1.1:8554",
            "127.0.0.1:0",
            "[::1]:8554",
            "127.0.0.1:8554/path",
        ] {
            let mut args = good.clone();
            args[0] = address.into();
            assert!(target(&args).is_err());
        }
        for bad in ["../path", "a?token=x", "a@b", "a b", "", "a\n"] {
            for i in [1, 2] {
                let mut args = good.clone();
                args[i] = bad.into();
                assert!(target(&args).is_err());
            }
        }
        assert!(target(&good[..2]).is_err());
        let mut extra = good.to_vec();
        extra.push("extra".into());
        assert!(target(&extra).is_err());
    }
}
