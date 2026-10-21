//! Physical-frame qualification. No synthetic source can satisfy this target.
use camera::{Camera, DeviceError, Format, PixelFormat};
use std::time::{Duration, Instant};

const FORMAT: Format = Format {
    width: 320,
    height: 240,
    pixels: PixelFormat::Rgb565,
};
const FRAME_BYTES: usize = 320 * 240 * 2;

fn capture(mut camera: impl Camera, format: Format, dump: bool) -> Result<(), DeviceError> {
    let mut buffer = vec![0; FRAME_BYTES];
    let origin = Instant::now();
    for cycle in 0..3 {
        if camera.start(format)? != format {
            let _ = camera.stop();
            return Err(DeviceError::InvalidData);
        }
        let result = (|| {
            for sequence in 0..3 {
                let deadline = Instant::now() + Duration::from_secs(3);
                loop {
                    let started = Instant::now();
                    let frame =
                        camera.poll_frame(origin.elapsed().as_millis() as u64, &mut buffer)?;
                    if started.elapsed() > Duration::from_millis(50) {
                        return Err(DeviceError::Timeout);
                    }
                    if let Some(frame) = frame {
                        if frame.format != format
                            || frame.len > buffer.len()
                            || !format.accepts_len(frame.len)
                        {
                            return Err(DeviceError::InvalidData);
                        }
                        let bytes = &buffer[..frame.len];
                        if format.pixels == PixelFormat::Jpeg
                            && (bytes.len() < 4
                                || !bytes.starts_with(&[0xff, 0xd8])
                                || !bytes.ends_with(&[0xff, 0xd9]))
                        {
                            return Err(DeviceError::InvalidData);
                        }
                        let checksum = bytes.iter().fold(2166136261u32, |value, byte| {
                            (value ^ u32::from(*byte)).wrapping_mul(16777619)
                        });
                        println!("NXRS_CAMERA_FRAME cycle={cycle} sequence={sequence} bytes={} checksum={checksum:08x}", frame.len);
                        if dump && cycle == 0 && sequence == 2 {
                            dump_frame(bytes)?;
                        }
                        break;
                    }
                    if Instant::now() >= deadline {
                        return Err(DeviceError::Timeout);
                    }
                    std::thread::sleep(Duration::from_millis(5));
                }
            }
            Ok(())
        })();
        let stopped = camera.stop();
        result?;
        stopped?;
    }
    println!("NXRS_CAMERA_PASS cycles=3 frames=9 stopped=1");
    Ok(())
}

fn dump_frame(bytes: &[u8]) -> Result<(), DeviceError> {
    use std::io::Write;
    let mut output = std::io::BufWriter::new(std::io::stdout());
    writeln!(output, "NXRS_CAMERA_HEX_BEGIN bytes={}", bytes.len()).map_err(|_| DeviceError::Io)?;
    for chunk in bytes.chunks(128) {
        for byte in chunk {
            write!(output, "{byte:02x}").map_err(|_| DeviceError::Io)?;
        }
        writeln!(output).map_err(|_| DeviceError::Io)?;
    }
    writeln!(output, "NXRS_CAMERA_HEX_END").map_err(|_| DeviceError::Io)?;
    output.flush().map_err(|_| DeviceError::Io)
}

#[cfg(target_os = "nuttx")]
fn main() {
    let mut dump = false;
    let mut format = FORMAT;
    for argument in std::env::args().skip(1) {
        match argument.as_str() {
            "--dump" => dump = true,
            "--jpeg" => format.pixels = PixelFormat::Jpeg,
            _ => {
                eprintln!("usage: camera_test [--jpeg] [--dump]");
                std::process::exit(2);
            }
        }
    }
    let result = camera::open().and_then(|camera| capture(camera, format, dump));
    if let Err(error) = result {
        eprintln!("NXRS_CAMERA_FAIL error={error:?}");
        std::process::exit(1);
    }
}

#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("camera_test requires a physical camera platform");
    std::process::exit(2);
}
