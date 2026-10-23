//! Apache-2.0 MLPerf Tiny Visual Wake Words MobileNet v1, 96x96 RGB,
//! compiled to quantized Rust by MicroFlow. Model identity is pinned in models/.
use super::image;
use crate::{DeviceError as Error, Score};
use microflow::buffer::Buffer2D;
use std::vec::Vec;
include!(concat!(env!("OUT_DIR"), "/network.rs"));
pub const MODEL_SHA256: &str = "597a384c8c2c8a1276f04702f25013b7838f2f814f1ca7c174d295b73e3d6b7b";
pub const INPUT_SIDE: usize = 96;
pub const INPUT_BYTES: usize = INPUT_SIDE * INPUT_SIDE * 3;
#[derive(Default)]
pub struct MobileNet {
    decoded: Vec<u8>,
}
impl MobileNet {
    /// Reuse bounded decoded storage; no hidden decoder threads.
    pub fn preprocess(
        &mut self,
        jpeg: &[u8],
        width: u16,
        height: u16,
    ) -> Result<[u8; INPUT_BYTES], Error> {
        image::decode(jpeg, width, height, &mut self.decoded)?;
        resize_rgb::<INPUT_SIDE, INPUT_BYTES>(
            &self.decoded,
            usize::from(width),
            usize::from(height),
            3,
        )
    }
    /// Interleaved 96x96 RGB, no crop/rotation. Scale 1/255 and zero point -128
    /// make byte subtraction exact input quantization.
    pub fn rgb(&mut self, pixels: &[u8]) -> Result<Score, Error> {
        if pixels.len() != INPUT_BYTES {
            return Err(Error::Frame);
        }
        let input = [Buffer2D::from_fn(|y, x| {
            core::array::from_fn(|c| pixels[(y * INPUT_SIDE + x) * 3 + c].wrapping_sub(128) as i8)
        })];
        let output = Network::predict_quantized(input);
        let person = output[(0, 1)];
        if !person.is_finite() || !(0.0..=1.0).contains(&person) {
            return Err(Error::Model);
        }
        Score::new((person * 1000.0).round() as u16)
    }
}
impl crate::Detector for MobileNet {
    fn capabilities(&self) -> crate::Capabilities {
        crate::Capabilities::presence(96, 96)
    }
    fn detect(&mut self, image: crate::Image<'_>) -> Result<crate::DetectionResult, Error> {
        let pixels = self.preprocess(image.bytes, image.width, image.height)?;
        Ok(crate::DetectionResult {
            person_score: self.rgb(&pixels)?,
            objects: crate::Detections::default(),
        })
    }
}

fn resize_rgb<const SIDE: usize, const BYTES: usize>(
    pixels: &[u8],
    width: usize,
    height: usize,
    channels: usize,
) -> Result<[u8; BYTES], Error> {
    if BYTES != SIDE * SIDE * 3 {
        return Err(Error::Configuration);
    }
    let mut output = [0; BYTES];
    image::resize_rgb_emit::<SIDE>(pixels, width, height, channels, |index, pixel| {
        output[index] = pixel
    })?;
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn resize_preserves_grayscale_and_rgb_channel_order() {
        assert_eq!(
            resize_rgb::<INPUT_SIDE, INPUT_BYTES>(&[31; 4], 2, 2, 1).unwrap(),
            [31; INPUT_BYTES]
        );
        let red = resize_rgb::<INPUT_SIDE, INPUT_BYTES>(&[255, 0, 0], 1, 1, 3).unwrap();
        assert!(red.chunks_exact(3).all(|v| v == [255, 0, 0]));
        assert!(resize_rgb::<INPUT_SIDE, INPUT_BYTES>(&[0], 2, 2, 1).is_err());
    }
    #[test]
    fn rejects_invalid_envelopes_before_decoding() {
        assert_eq!(
            MobileNet::default().preprocess(&[255, 216, 255, 217], 1000, 1000),
            Err(Error::Frame)
        );
        assert_eq!(
            MobileNet::default().preprocess(&[], 96, 96),
            Err(Error::Frame)
        );
    }
}
