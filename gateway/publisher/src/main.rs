//! Single-use RTSP publisher; pixel coding belongs to the separate encoder.
//! Its supervisor must bound startup, packet progress, cancellation and reaping.
#![deny(unsafe_code)]
#[allow(unsafe_code)]
mod sdk;

use std::{
    io::{Read, Write},
    net::SocketAddrV4,
    os::unix::fs::FileTypeExt,
};
use zeroize::Zeroizing;
#[derive(Clone, Copy, PartialEq, Eq)]
enum EncodedMedia {
    MpegTs,
    AnnexB,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Failure {
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
fn publication_input(
    args: &[String],
) -> Result<(std::net::SocketAddrV4, &str, &str, EncodedMedia)> {
    let (public, format) = match args {
        [_, _, _] => (args, EncodedMedia::MpegTs),
        [_, _, _, mode] if mode == "annexb" => (&args[..3], EncodedMedia::AnnexB),
        _ => return Err(Failure::Configuration),
    };
    let (address, user, path) = target(public)?;
    Ok((address, user, path, format))
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
/// Raw access units lack a container clock. Give published packets a monotonic
/// receipt clock in the SDK output's own ticks. This clock never identifies a
/// source image or claims a capture time; the unchanged SEI owns that identity.
struct ReceiptClock {
    start: std::time::Instant,
    numerator: u32,
    denominator: u32,
    previous: Option<i64>,
}
impl ReceiptClock {
    fn new(time_base: (u32, u32)) -> Result<Self> {
        if time_base.0 == 0 || time_base.1 == 0 {
            return Err(Failure::Media);
        }
        Ok(Self {
            start: std::time::Instant::now(),
            numerator: time_base.0,
            denominator: time_base.1,
            previous: None,
        })
    }
    fn advance(&mut self, elapsed: std::time::Duration) -> Result<i64> {
        let scaled = elapsed
            .as_nanos()
            .checked_mul(u128::from(self.denominator))
            .ok_or(Failure::Media)?
            / (1_000_000_000u128 * u128::from(self.numerator));
        let observed = i64::try_from(scaled).map_err(|_| Failure::Media)?;
        let next = match self.previous {
            Some(last) => observed.max(last.checked_add(1).ok_or(Failure::Media)?),
            None => observed,
        };
        self.previous = Some(next);
        Ok(next)
    }
    fn next(&mut self) -> Result<i64> {
        self.advance(self.start.elapsed())
    }
}
fn publish(args: &[String]) -> Result<()> {
    let (address, user, path, format) = publication_input(args)?;
    // Read exactly the 64-byte preamble without Rust Stdin's buffering: libavformat
    // subsequently owns reads from pipe:0. No compressed byte may be prefetched and lost.
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
    sdk::quiet();
    let mut source = sdk::Source::open(format)?;
    let mut output = sdk::Publication::open(&url, &source)?;
    let mut receipt_clock = if format == EncodedMedia::AnnexB {
        Some(ReceiptClock::new(output.time_base()?)?)
    } else {
        None
    };
    let mut count = 0u64;
    while source.next()? {
        let timestamp = match &mut receipt_clock {
            Some(clock) => Some(clock.next()?),
            None => {
                if !source.has_timestamps() {
                    return Err(Failure::Media);
                }
                None
            }
        };
        output.write(&mut source, timestamp)?;
        count = count.checked_add(1).ok_or(Failure::Media)?;
        // Progress confirms successful SDK publication, not encoder activity.
        writeln!(std::io::stdout(), "packet={count}").map_err(|_| Failure::Output)?;
        std::io::stdout().flush().map_err(|_| Failure::Output)?;
    }
    output.finish()
}

fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if let Err(error) = publish(&args) {
        eprintln!("nxrs-rtsp-publisher: {error:?}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn receipt_ticks_follow_time_and_remain_monotonic_for_bursts() {
        let mut clock = ReceiptClock::new((1, 90_000)).unwrap();
        assert_eq!(
            clock.advance(std::time::Duration::from_millis(125)),
            Ok(11_250)
        );
        assert_eq!(
            clock.advance(std::time::Duration::from_millis(125)),
            Ok(11_251)
        );
        assert_eq!(
            clock.advance(std::time::Duration::from_millis(250)),
            Ok(22_500)
        );
        assert!(ReceiptClock::new((1, 0)).is_err());
    }
}
