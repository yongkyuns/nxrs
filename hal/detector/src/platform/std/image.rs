use crate::DeviceError as Error;
use std::vec::Vec;
pub(crate) const MAX_WIDTH: u16 = 640;
pub(crate) const MAX_HEIGHT: u16 = 480;
use zune_jpeg::{
    JpegDecoder,
    zune_core::{bytestream::ZCursor, colorspace::ColorSpace, options::DecoderOptions},
};
/// Decode into reusable bounded RGB storage, then resize the entire image.
pub(crate) fn decode(
    jpeg: &[u8],
    width: u16,
    height: u16,
    output: &mut Vec<u8>,
) -> Result<(), Error> {
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
    Ok(())
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

pub(crate) fn resize_rgb_emit<const SIDE: usize>(
    pixels: &[u8],
    width: usize,
    height: usize,
    channels: usize,
    mut emit: impl FnMut(usize, u8),
) -> Result<(), Error> {
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
    if SIDE == 0 {
        return Err(Error::Configuration);
    }
    for y in 0..SIDE {
        // Pixel-center mapping, clamped at image edges; Q8 interpolation.
        let sy = (((2 * y + 1) * height * 128) / SIDE)
            .saturating_sub(128)
            .min((height - 1) * 256);
        let (y0, dy) = (sy / 256, (sy % 256) as u32);
        let y1 = (y0 + 1).min(height - 1);
        for x in 0..SIDE {
            let sx = (((2 * x + 1) * width * 128) / SIDE)
                .saturating_sub(128)
                .min((width - 1) * 256);
            let (x0, dx) = (sx / 256, (sx % 256) as u32);
            let x1 = (x0 + 1).min(width - 1);
            for c in 0..3 {
                let top = channel(x0, y0, c) * (256 - dx) + channel(x1, y0, c) * dx;
                let bottom = channel(x0, y1, c) * (256 - dx) + channel(x1, y1, c) * dx;
                emit(
                    (y * SIDE + x) * 3 + c,
                    ((top * (256 - dy) + bottom * dy + 32768) >> 16) as u8,
                );
            }
        }
    }
    Ok(())
}
