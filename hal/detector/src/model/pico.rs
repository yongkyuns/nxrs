//! Fixed, MIT-licensed PICO_S8_V1 person model. Rust owns JPEG preparation,
//! confidence decoding and bounded box postprocessing; a supplied kernel owns
//! only synchronous quantized numerical execution.
#[allow(unsafe_code)]
#[path = "../compute/esp_dl/ffi/mod.rs"]
mod compute;
use super::image;
use crate::{Bounds, Detection, Detections, DeviceError as Error, Score};
pub(crate) use compute::Engine;
use std::{boxed::Box, vec::Vec};
pub const INPUT_SIDE: usize = 224;
pub const INPUT_BYTES: usize = INPUT_SIDE * INPUT_SIDE * 3;
pub const CELLS: usize = 28 * 28 + 14 * 14 + 7 * 7;
pub const OUTPUT_BYTES: usize = CELLS * 33;
const CANDIDATES: usize = 128;
#[derive(Clone, Copy)]
pub(crate) enum Stage {
    Decode,
    Resize,
    Kernel,
    Postprocess,
    Complete,
}
/// An intentional fixed-model tensor protocol, independent of OS, SDK and
/// accelerator. Scores precede boxes, each in stage order 28², 14², 7², NHWC.
/// Exponents are (-8,-7,-7) for scores and (-3,-3,-4) for 32-bin box logits.
pub trait Kernel: Send + 'static {
    fn run(
        &mut self,
        input: &[i8; INPUT_BYTES],
        output: &mut [i8; OUTPUT_BYTES],
    ) -> Result<(), Error>;
}
pub struct Pico<K> {
    kernel: K,
    decoded: Vec<u8>,
    input: Box<[i8; INPUT_BYTES]>,
    output: Box<[i8; OUTPUT_BYTES]>,
    threshold: Score,
}
impl<K: Kernel> Pico<K> {
    pub fn new(kernel: K) -> Result<Self, Error> {
        let mut input = Vec::new();
        input
            .try_reserve_exact(INPUT_BYTES)
            .map_err(|_| Error::Model)?;
        input.resize(INPUT_BYTES, 0);
        let mut output = Vec::new();
        output
            .try_reserve_exact(OUTPUT_BYTES)
            .map_err(|_| Error::Model)?;
        output.resize(OUTPUT_BYTES, 0);
        Ok(Self {
            kernel,
            input: input
                .into_boxed_slice()
                .try_into()
                .map_err(|_| Error::Model)?,
            decoded: Vec::new(),
            output: output
                .into_boxed_slice()
                .try_into()
                .map_err(|_| Error::Model)?,
            threshold: Score::new(500)?,
        })
    }
    /// Object output threshold; presence continues to expose the maximum score
    /// so a caller's portable Monitor retains ownership of enter/leave policy.
    pub fn set_box_threshold(&mut self, threshold: Score) {
        self.threshold = threshold;
    }
    /// A typed observation hook lets qualification time the actual pipeline.
    /// Normal acquisition supplies an empty observer, erased by monomorphization.
    pub(crate) fn detect_observed(
        &mut self,
        image: crate::Image<'_>,
        mut observe: impl FnMut(Stage),
    ) -> Result<crate::DetectionResult, Error> {
        observe(Stage::Decode);
        image::decode(image.bytes, image.width, image.height, &mut self.decoded)?;
        observe(Stage::Resize);
        // Fill a reusable heap tensor directly; 224x224 RGB arrays must not
        // accumulate in nested stack frames on the MCU.
        let input = &mut *self.input;
        image::resize_rgb_emit::<INPUT_SIDE>(
            &self.decoded,
            usize::from(image.width),
            usize::from(image.height),
            3,
            |index, pixel| {
                input[index] = u16::from(pixel).div_ceil(2).min(127) as i8;
            },
        )?;
        observe(Stage::Kernel);
        self.kernel.run(&self.input, &mut self.output)?;
        observe(Stage::Postprocess);
        let result = crate::DetectionResult {
            person_score: maximum(&self.output)?,
            objects: boxes(&self.output, self.threshold)?,
        };
        observe(Stage::Complete);
        Ok(result)
    }
}
impl<K: Kernel> crate::Detector for Pico<K> {
    fn capabilities(&self) -> crate::Capabilities {
        crate::Capabilities::localized(224, 224)
    }
    fn detect(&mut self, image: crate::Image<'_>) -> Result<crate::DetectionResult, Error> {
        self.detect_observed(image, |_| {})
    }
}
fn confidence(raw: i8, stage: usize) -> Result<Score, Error> {
    if raw < 0 {
        return Err(Error::Model);
    }
    let scale = if stage == 0 { 256.0 } else { 128.0 };
    Score::new((1000.0 * (f32::from(raw) / scale).sqrt()).round() as u16)
}
fn maximum(output: &[i8; OUTPUT_BYTES]) -> Result<Score, Error> {
    let mut offset = 0;
    let mut best = Score::new(0)?;
    for (stage, side) in [28, 14, 7].into_iter().enumerate() {
        for &raw in &output[offset..offset + side * side] {
            let score = confidence(raw, stage)?;
            if score.permille() > best.permille() {
                best = score;
            }
        }
        offset += side * side;
    }
    Ok(best)
}
fn integral(values: &[i8], scale: f32) -> f32 {
    let maximum = *values.iter().max().unwrap();
    let mut sum = 0.0;
    let mut weighted = 0.0;
    for (i, &value) in values.iter().enumerate() {
        let weight = ((f32::from(value) - f32::from(maximum)) * scale).exp();
        sum += weight;
        weighted += i as f32 * weight;
    }
    weighted / sum
}
fn overlap(a: Bounds, b: Bounds) -> bool {
    let w = a.right.min(b.right).saturating_sub(a.left.max(b.left));
    let h = a.bottom.min(b.bottom).saturating_sub(a.top.max(b.top));
    let intersection = u64::from(w) * u64::from(h);
    let area = |r: Bounds| u64::from(r.right - r.left) * u64::from(r.bottom - r.top);
    intersection * 2 > area(a) + area(b) - intersection
}
fn boxes(output: &[i8; OUTPUT_BYTES], threshold: Score) -> Result<Detections, Error> {
    let mut candidates: [Option<Detection>; CANDIDATES] = [None; CANDIDATES];
    let mut count = 0;
    let mut truncated = false;
    let mut score_offset = 0;
    let mut box_offset = CELLS;
    for (stage, side) in [28, 14, 7].into_iter().enumerate() {
        let stride = 224.0 / side as f32;
        let scale = if stage == 2 { 1.0 / 16.0 } else { 1.0 / 8.0 };
        for cell in 0..side * side {
            let score = confidence(output[score_offset + cell], stage)?;
            if score.permille() < threshold.permille() {
                continue;
            }
            let logits = &output[box_offset + cell * 32..box_offset + (cell + 1) * 32];
            let x = (cell % side) as f32 + 0.5;
            let y = (cell / side) as f32 + 0.5;
            let coordinate =
                |value: f32| (value * stride / 224.0 * 1000.0).round().clamp(0.0, 1000.0) as u16;
            let bounds = Bounds {
                left: coordinate(x - integral(&logits[..8], scale)),
                top: coordinate(y - integral(&logits[8..16], scale)),
                right: coordinate(x + integral(&logits[16..24], scale)),
                bottom: coordinate(y + integral(&logits[24..], scale)),
            };
            if bounds.left >= bounds.right || bounds.top >= bounds.bottom {
                continue;
            }
            let detection = Detection { score, bounds };
            let insert = candidates[..count]
                .iter()
                .position(|p| p.unwrap().score.permille() < score.permille())
                .unwrap_or(count);
            if count == CANDIDATES {
                truncated = true;
            }
            if insert < CANDIDATES {
                let end = count.min(CANDIDATES - 1);
                candidates.copy_within(insert..end, insert + 1);
                candidates[insert] = Some(detection);
                count = (count + 1).min(CANDIDATES);
            }
        }
        score_offset += side * side;
        box_offset += side * side * 32;
    }
    let mut result = Detections::default();
    for detection in candidates[..count].iter().flatten().copied() {
        if result
            .boxes
            .iter()
            .flatten()
            .any(|other| overlap(detection.bounds, other.bounds))
        {
            continue;
        }
        if let Some(slot) = result.boxes.iter_mut().find(|slot| slot.is_none()) {
            *slot = Some(detection);
        } else {
            truncated = true;
        }
    }
    result.truncated = truncated;
    Ok(result)
}

impl Kernel for Engine {
    fn run(
        &mut self,
        input: &[i8; INPUT_BYTES],
        output: &mut [i8; OUTPUT_BYTES],
    ) -> core::result::Result<(), crate::DeviceError> {
        self.run(input, output);
        Ok(())
    }
}
pub(crate) fn with_kernel<K: Kernel>(
    config: crate::Config,
    kernel: K,
) -> core::result::Result<Pico<K>, crate::DeviceError> {
    let mut model = Pico::new(kernel)?;
    model.set_box_threshold(config.box_threshold);
    Ok(model)
}

pub(crate) fn kernel() -> Result<Engine, crate::DeviceError> {
    Engine::new().map_err(|_| crate::DeviceError::Model)
}
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    with_kernel(config, kernel()?)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stage_scales_and_geometric_confidence_match_sdk() {
        assert_eq!(confidence(64, 0).unwrap().permille(), 500);
        assert_eq!(confidence(100, 1).unwrap().permille(), 884);
        assert_eq!(confidence(-1, 2), Err(Error::Model));
        assert!((integral(&[0; 8], 0.125) - 3.5).abs() < 1e-6);
    }
    #[test]
    fn suppression_bounds_and_zero_threshold_are_explicit() {
        let mut out = Box::new([0; OUTPUT_BYTES]);
        assert!(boxes(&out, Score::new(500).unwrap())
            .unwrap()
            .boxes
            .iter()
            .all(Option::is_none));
        out[14 * 28 + 14] = 100;
        let result = boxes(&out, Score::new(500).unwrap()).unwrap();
        let first = result.boxes[0].unwrap();
        assert_eq!(result.boxes.iter().flatten().count(), 1);
        assert!(first.bounds.left < first.bounds.right && first.bounds.right <= 1000);
        let a = Bounds {
            left: 0,
            top: 0,
            right: 500,
            bottom: 500,
        };
        assert!(overlap(a, a));
        assert!(!overlap(
            a,
            Bounds {
                left: 501,
                right: 1000,
                ..a
            }
        ));
    }
}
