//! Opt-in qualification of the actual detector pipeline. Product callers use
//! `crate::open`; no clock, timing storage or native ABI is added to that API.
use crate::model::pico::{Kernel, Pico, Stage};
use crate::{Capabilities, Config, DetectionResult, Detector, DeviceError, Image};
use std::time::Instant;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Timings {
    pub decode_us: u64,
    pub resize_us: u64,
    pub kernel_us: u64,
    pub postprocess_us: u64,
    pub total_us: u64,
}

pub trait Profiled: Detector {
    /// Last attempted call. Consumers must also check the detection result.
    fn timings(&self) -> Timings;
}

pub fn open(config: Config) -> Result<impl Profiled, DeviceError> {
    let model = crate::model::pico::with_kernel(config, crate::platform::kernel()?)?;
    Ok(Measured {
        model,
        last: Timings::default(),
    })
}

struct Measured<K> {
    model: Pico<K>,
    last: Timings,
}
impl<K: Kernel> Detector for Measured<K> {
    fn capabilities(&self) -> Capabilities {
        self.model.capabilities()
    }
    fn detect(&mut self, image: Image<'_>) -> Result<DetectionResult, DeviceError> {
        let started = Instant::now();
        let mut previous = None;
        let mut stages = [0; 4];
        let result = self.model.detect_observed(image, |stage| {
            let now = Instant::now();
            if let Some((index, begin)) = previous {
                stages[index] = now.duration_since(begin).as_micros() as u64;
            }
            previous = match stage {
                Stage::Decode => Some((0, now)),
                Stage::Resize => Some((1, now)),
                Stage::Kernel => Some((2, now)),
                Stage::Postprocess => Some((3, now)),
                Stage::Complete => None,
            };
        });
        if let Some((index, begin)) = previous {
            stages[index] = begin.elapsed().as_micros() as u64;
        }
        self.last = Timings {
            decode_us: stages[0],
            resize_us: stages[1],
            kernel_us: stages[2],
            postprocess_us: stages[3],
            total_us: started.elapsed().as_micros() as u64,
        };
        result
    }
}
impl<K: Kernel> Profiled for Measured<K> {
    fn timings(&self) -> Timings {
        self.last
    }
}
