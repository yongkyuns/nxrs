//! Qualification-only observation of the actual portable HAL implementation.
use detector::qualification::Profiled;
use detector::{Capabilities, DetectionResult, Detector, DeviceError, Image};
unsafe extern "C" {
    fn nxrs_stream_metrics(values: *mut u32, times: *mut u64) -> i32;
    fn nxrs_stream_cpu_frequency() -> u32;
}

fn clock() -> Result<[u64; 3], DeviceError> {
    let mut values = [0u32; 2];
    let mut times = [0u64; 3];
    // SAFETY: the existing fixture witness fills the exact scalar arrays. Its
    // CPU clock observes this calling detector worker, including attributed ISRs.
    if unsafe { nxrs_stream_metrics(values.as_mut_ptr(), times.as_mut_ptr()) } != 0 {
        return Err(DeviceError::Model);
    }
    Ok(times)
}

pub(super) fn open() -> Result<impl Detector, DeviceError> {
    Ok(Reporting(detector::qualification::open(
        detector::Config::default(),
    )?))
}
struct Reporting<T>(T);
impl<T: Profiled> Detector for Reporting<T> {
    fn capabilities(&self) -> Capabilities {
        self.0.capabilities()
    }
    fn detect(&mut self, image: Image<'_>) -> Result<DetectionResult, DeviceError> {
        let before = clock()?;
        let result = self.0.detect(image);
        let after = clock()?;
        let wall = after[0].checked_sub(before[0]).ok_or(DeviceError::Model)?;
        let cpu = after[2].checked_sub(before[2]).ok_or(DeviceError::Model)?;
        let t = self.0.timings();
        println!(
            "NXRS_DETECTION_PROFILE decode_us={} resize_us={} kernel_us={} postprocess_us={} total_us={}",
            t.decode_us, t.resize_us, t.kernel_us, t.postprocess_us, t.total_us
        );
        // SAFETY: SDK scalar frequency observation, with no state mutation.
        let frequency = unsafe { nxrs_stream_cpu_frequency() };
        println!("NXRS_DETECTION_CPU wall_us={wall} cpu_us={cpu} frequency_hz={frequency}");
        result
    }
}
