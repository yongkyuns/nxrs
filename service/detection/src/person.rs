//! Apache-2.0 MLPerf Tiny Visual Wake Words MobileNet v1, 96x96 RGB,
//! compiled to quantized Rust by MicroFlow. Model identity is pinned in models/.
use crate::{Error, PersonDetector, Score};
use microflow::buffer::Buffer2D;
use zune_jpeg::{
    zune_core::{bytestream::ZCursor, colorspace::ColorSpace, options::DecoderOptions},
    JpegDecoder,
};
include!(concat!(env!("OUT_DIR"), "/network.rs"));
pub const MODEL_SHA256: &str = "597a384c8c2c8a1276f04702f25013b7838f2f814f1ca7c174d295b73e3d6b7b";
pub const INPUT_SIDE: usize = 96;
pub const INPUT_BYTES: usize = INPUT_SIDE * INPUT_SIDE * 3;
pub const MAX_WIDTH: u16 = 640;
pub const MAX_HEIGHT: u16 = 480;
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
        preprocess(jpeg, width, height, &mut self.decoded)
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
impl PersonDetector for MobileNet {
    fn detect(&mut self, jpeg: &[u8], width: u16, height: u16) -> Result<Score, Error> {
        let pixels = self.preprocess(jpeg, width, height)?;
        self.rgb(&pixels)
    }
}
/// Decode into reusable bounded RGB storage, then resize the entire image.
fn preprocess(
    jpeg: &[u8],
    width: u16,
    height: u16,
    output: &mut Vec<u8>,
) -> Result<[u8; INPUT_BYTES], Error> {
    if width == 0
        || height == 0
        || width > MAX_WIDTH
        || height > MAX_HEIGHT
        || jpeg.len() > 153600
        || jpeg.len() < 4
        || !jpeg.starts_with(&[255, 216])
        || !jpeg.ends_with(&[255, 217])
        || !baseline(jpeg)
    {
        return Err(Error::Frame);
    }
    let options = DecoderOptions::default()
        .set_max_width(usize::from(MAX_WIDTH))
        .set_max_height(usize::from(MAX_HEIGHT))
        .set_strict_mode(true)
        .jpeg_set_max_scans(1)
        .jpeg_set_out_colorspace(ColorSpace::RGB);
    let mut decoder = JpegDecoder::new_with_options(ZCursor::new(jpeg), options);
    decoder.decode_headers().map_err(|_| Error::Frame)?;
    if decoder.dimensions() != Some((usize::from(width), usize::from(height)))
        || !matches!(
            decoder.input_colorspace(),
            Some(ColorSpace::RGB | ColorSpace::YCbCr | ColorSpace::Luma)
        )
    {
        return Err(Error::Frame);
    }
    let size = decoder.output_buffer_size().ok_or(Error::Frame)?;
    if size != usize::from(width) * usize::from(height) * 3 {
        return Err(Error::Frame);
    }
    if size > output.len() {
        output
            .try_reserve_exact(size - output.len())
            .map_err(|_| Error::Model)?;
    }
    output.resize(size, 0);
    decoder.decode_into(output).map_err(|_| Error::Frame)?;
    resize_rgb(output, usize::from(width), usize::from(height), 3)
}
// Reject progressive/non-8-bit inputs before the decoder allocates pixels.
fn baseline(bytes: &[u8]) -> bool {
    let mut offset = 2;
    while offset + 4 <= bytes.len() {
        if bytes[offset] != 255 {
            return false;
        }
        while offset < bytes.len() && bytes[offset] == 255 {
            offset += 1;
        }
        let Some(&marker) = bytes.get(offset) else {
            return false;
        };
        offset += 1;
        let Some(length_bytes) = bytes.get(offset..offset + 2) else {
            return false;
        };
        let length = usize::from(u16::from_be_bytes([length_bytes[0], length_bytes[1]]));
        if length < 2 || length > bytes.len() - offset {
            return false;
        }
        if (0xc0..=0xcf).contains(&marker) && !matches!(marker, 0xc4 | 0xc8 | 0xcc) {
            return marker == 0xc0 && bytes.get(offset + 2) == Some(&8);
        }
        if marker == 0xda || marker == 0xd9 {
            return false;
        }
        offset += length;
    }
    false
}

fn resize_rgb(
    pixels: &[u8],
    width: usize,
    height: usize,
    channels: usize,
) -> Result<[u8; INPUT_BYTES], Error> {
    if width == 0
        || height == 0
        || !matches!(channels, 1 | 3)
        || pixels.len() != width * height * channels
    {
        return Err(Error::Frame);
    }
    let channel = |x: usize, y: usize, c: usize| -> u32 {
        let p = (y * width + x) * channels;
        if channels == 1 {
            u32::from(pixels[p])
        } else {
            u32::from(pixels[p + c])
        }
    };
    let mut output = [0; INPUT_BYTES];
    for y in 0..INPUT_SIDE {
        // Pixel-center mapping, clamped at image edges; Q8 interpolation.
        let sy = (((2 * y + 1) * height * 128) / INPUT_SIDE)
            .saturating_sub(128)
            .min((height - 1) * 256);
        let (y0, dy) = (sy / 256, (sy % 256) as u32);
        let y1 = (y0 + 1).min(height - 1);
        for x in 0..INPUT_SIDE {
            let sx = (((2 * x + 1) * width * 128) / INPUT_SIDE)
                .saturating_sub(128)
                .min((width - 1) * 256);
            let (x0, dx) = (sx / 256, (sx % 256) as u32);
            let x1 = (x0 + 1).min(width - 1);
            for c in 0..3 {
                let top = channel(x0, y0, c) * (256 - dx) + channel(x1, y0, c) * dx;
                let bottom = channel(x0, y1, c) * (256 - dx) + channel(x1, y1, c) * dx;
                output[(y * INPUT_SIDE + x) * 3 + c] =
                    ((top * (256 - dy) + bottom * dy + 32768) >> 16) as u8;
            }
        }
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn resize_preserves_grayscale_and_rgb_channel_order() {
        assert_eq!(resize_rgb(&[31; 4], 2, 2, 1).unwrap(), [31; INPUT_BYTES]);
        let red = resize_rgb(&[255, 0, 0], 1, 1, 3).unwrap();
        assert!(red.chunks_exact(3).all(|v| v == [255, 0, 0]));
        assert!(resize_rgb(&[0], 2, 2, 1).is_err());
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
