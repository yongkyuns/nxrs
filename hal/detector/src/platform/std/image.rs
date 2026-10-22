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
    emit: impl FnMut(usize, u8),
) -> Result<(), Error> {
    if width == 0
        || height == 0
        || !matches!(channels, 1 | 3)
        || width
            .checked_mul(height)
            .and_then(|n| n.checked_mul(channels))
            != Some(pixels.len())
    {
        return Err(Error::Frame);
    }
    if SIDE == 0 {
        return Err(Error::Configuration);
    }
    if channels == 1 {
        resize_channels::<SIDE, 1>(pixels, width, height, emit);
    } else {
        resize_channels::<SIDE, 3>(pixels, width, height, emit);
    }
    Ok(())
}

fn resize_channels<const SIDE: usize, const CHANNELS: usize>(
    pixels: &[u8],
    width: usize,
    height: usize,
    mut emit: impl FnMut(usize, u8),
) {
    // Horizontal coordinates are identical for every output row. Calculate
    // them once and reuse bounded row/pixel slices across the RGB channels.
    let horizontal: [(usize, usize, u32); SIDE] = core::array::from_fn(|x| {
        let sx = (((2 * x + 1) * width * 128) / SIDE)
            .saturating_sub(128)
            .min((width - 1) * 256);
        let x0 = sx / 256;
        (
            x0 * CHANNELS,
            (x0 + 1).min(width - 1) * CHANNELS,
            (sx % 256) as u32,
        )
    });
    let row_bytes = width * CHANNELS;
    for y in 0..SIDE {
        // Pixel-center mapping, clamped at image edges; Q8 interpolation.
        let sy = (((2 * y + 1) * height * 128) / SIDE)
            .saturating_sub(128)
            .min((height - 1) * 256);
        let (y0, dy) = (sy / 256, (sy % 256) as u32);
        let y1 = (y0 + 1).min(height - 1);
        let row0 = &pixels[y0 * row_bytes..(y0 + 1) * row_bytes];
        let row1 = &pixels[y1 * row_bytes..(y1 + 1) * row_bytes];
        for (x, &(left, right, dx)) in horizontal.iter().enumerate() {
            let a: &[u8; CHANNELS] = row0[left..left + CHANNELS].try_into().unwrap();
            let b: &[u8; CHANNELS] = row0[right..right + CHANNELS].try_into().unwrap();
            let lower_left: &[u8; CHANNELS] = row1[left..left + CHANNELS].try_into().unwrap();
            let d: &[u8; CHANNELS] = row1[right..right + CHANNELS].try_into().unwrap();
            let weights = [
                (256 - dx) * (256 - dy),
                dx * (256 - dy),
                (256 - dx) * dy,
                dx * dy,
            ];
            for c in 0..3 {
                let channel = if CHANNELS == 1 { 0 } else { c };
                let value = u32::from(a[channel]) * weights[0]
                    + u32::from(b[channel]) * weights[1]
                    + u32::from(lower_left[channel]) * weights[2]
                    + u32::from(d[channel]) * weights[3];
                emit((y * SIDE + x) * 3 + c, ((value + 32768) >> 16) as u8);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Frozen scalar pixel-center/Q8 definition, independent of the row/axis
    // optimization. This protects quantized model inputs, not just final labels.
    fn reference<const SIDE: usize>(
        pixels: &[u8],
        width: usize,
        height: usize,
        channels: usize,
    ) -> Vec<u8> {
        let mut result = Vec::new();
        for y in 0..SIDE {
            let sy = (((2 * y + 1) * height * 128) / SIDE)
                .saturating_sub(128)
                .min((height - 1) * 256);
            for x in 0..SIDE {
                let sx = (((2 * x + 1) * width * 128) / SIDE)
                    .saturating_sub(128)
                    .min((width - 1) * 256);
                let (dx, dy) = ((sx % 256) as u32, (sy % 256) as u32);
                for c in 0..3 {
                    let at = |x: usize, y: usize| {
                        u32::from(
                            pixels[(y * width + x) * channels + if channels == 1 { 0 } else { c }],
                        )
                    };
                    let (x0, y0) = (sx / 256, sy / 256);
                    let (x1, y1) = ((x0 + 1).min(width - 1), (y0 + 1).min(height - 1));
                    let top = at(x0, y0) * (256 - dx) + at(x1, y0) * dx;
                    let bottom = at(x0, y1) * (256 - dx) + at(x1, y1) * dx;
                    result.push(((top * (256 - dy) + bottom * dy + 32768) >> 16) as u8);
                }
            }
        }
        result
    }

    fn compare<const SIDE: usize>(pixels: &[u8], width: usize, height: usize, channels: usize) {
        let mut actual = std::vec![0; SIDE * SIDE * 3];
        resize_rgb_emit::<SIDE>(pixels, width, height, channels, |i, v| actual[i] = v).unwrap();
        assert_eq!(
            actual,
            reference::<SIDE>(pixels, width, height, channels),
            "{width}x{height} channels={channels} output={SIDE}"
        );
    }

    #[test]
    fn resized_model_inputs_preserve_q8_rounding_edges_and_channel_order() {
        let mut state = 1729u32;
        for (width, height) in [
            (1, 1),
            (2, 3),
            (3, 2),
            (37, 59),
            (320, 240),
            (640, 480),
            (639, 479),
        ] {
            for channels in [1, 3] {
                let pixels: Vec<_> = (0..width * height * channels)
                    .map(|_| {
                        state = state.wrapping_mul(1664525).wrapping_add(1013904223);
                        (state >> 24) as u8
                    })
                    .collect();
                compare::<1>(&pixels, width, height, channels);
                compare::<3>(&pixels, width, height, channels);
                compare::<96>(&pixels, width, height, channels);
                compare::<224>(&pixels, width, height, channels);
            }
        }
    }

    #[test]
    fn invalid_geometry_and_output_size_are_rejected_before_indexing() {
        assert_eq!(
            resize_rgb_emit::<0>(&[0], 1, 1, 1, |_, _| {}),
            Err(Error::Configuration)
        );
        for (width, height, channels) in [(0, 1, 1), (1, 0, 1), (1, 1, 2), (usize::MAX, 2, 3)] {
            assert_eq!(
                resize_rgb_emit::<224>(&[], width, height, channels, |_, _| {}),
                Err(Error::Frame)
            );
        }
    }
}
