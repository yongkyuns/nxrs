//! Serial-only model qualification. No network endpoint, camera or Wi-Fi owner.
use detector::{Detector, Image};
use std::{
    io::Read,
    time::{Duration, Instant},
};
const LIMIT: usize = 153600;
unsafe extern "C" {
    fn nxrs_detector_console_mode(saved: *mut u32, raw: i32) -> i32;
    fn nxrs_stream_console_ready() -> i32;
}
struct Raw([u32; 6]);
impl Raw {
    fn enter() -> Result<Self, String> {
        let mut value = Self([0; 6]);
        // SAFETY: target-header termios translation receives six writable fields.
        if unsafe { nxrs_detector_console_mode(value.0.as_mut_ptr(), 1) } != 0 {
            return Err("raw console mode".into());
        }
        Ok(value)
    }
}
impl Drop for Raw {
    fn drop(&mut self) {
        // SAFETY: restore only the fields captured by this native console owner.
        let _ = unsafe { nxrs_detector_console_mode(self.0.as_mut_ptr(), 0) };
    }
}
fn read_exact(input: &mut impl Read, buffer: &mut [u8]) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(30);
    let mut offset = 0;
    while offset < buffer.len() {
        if Instant::now() >= deadline {
            return Err(format!(
                "batch serial deadline received={offset} expected={}",
                buffer.len()
            ));
        }
        // SAFETY: target-header readiness observes this task's standard input only.
        match unsafe { nxrs_stream_console_ready() } {
            n if n < 0 => return Err("batch serial closed".into()),
            0 => std::thread::yield_now(),
            _ => match input.read(&mut buffer[offset..]) {
                Ok(0) => return Err("batch serial EOF".into()),
                Ok(n) => offset += n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => return Err(e.to_string()),
            },
        }
    }
    Ok(())
}
fn batch() -> Result<(), String> {
    let _raw = Raw::enter()?;
    let mut model = detector::open(detector::Config::default()).map_err(|e| e.to_string())?;
    let mut bytes = vec![0; LIMIT];
    let mut input = std::fs::File::open("/dev/console").map_err(|e| e.to_string())?;
    let mut count = 0u32;
    println!("NXRS_DETECT_BATCH_READY");
    loop {
        let mut header = [0u8; 16];
        read_exact(&mut input, &mut header)?;
        if &header[..4] != b"NXP1" {
            return Err("batch serial magic".into());
        }
        let index = u32::from_le_bytes(header[4..8].try_into().unwrap());
        let length = u32::from_le_bytes(header[8..12].try_into().unwrap()) as usize;
        if length == 0 {
            break;
        }
        let width = u16::from_le_bytes(header[12..14].try_into().unwrap());
        let height = u16::from_le_bytes(header[14..16].try_into().unwrap());
        if index != count || length > LIMIT {
            return Err("batch serial bounds/sequence".into());
        }
        // Explicit bounded flow control prevents the USB console's small RX
        // ring from becoming an implicit unbounded dataset transport.
        println!("NXRS_DETECT_CHUNK index={index} offset=0");
        let mut offset = 0;
        while offset < length {
            let end = (offset + 32).min(length);
            read_exact(&mut input, &mut bytes[offset..end])?;
            offset = end;
            println!("NXRS_DETECT_CHUNK index={index} offset={offset}");
        }
        let start = Instant::now();
        let result = model
            .detect(Image::jpeg(&bytes[..length], width, height))
            .map_err(|e| e.to_string())?;
        println!(
            "NXRS_DETECT_RESULT index={index} score={} inference_us={} boxes={} truncated={}",
            result.person_score.permille(),
            start.elapsed().as_micros(),
            result.objects.len(),
            u8::from(result.objects.truncated)
        );
        count += 1;
    }
    drop(model);
    println!("NXRS_DETECT_BATCH_PASS count={count}");
    Ok(())
}
pub(super) fn run() -> Result<(), String> {
    std::thread::Builder::new()
        .name("detector-evaluation".into())
        .stack_size(512 * 1024)
        .spawn(batch)
        .map_err(|e| e.to_string())?
        .join()
        .map_err(|_| "batch worker panic".to_owned())?
}
