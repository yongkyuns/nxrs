use crate::font::glyph;
use display::graphics::{Area, Raster};

/// Allocation-free 5-by-7 bitmap text in screen coordinates.
pub trait TextCanvas {
    /// Draw uppercase ASCII glyphs; unsupported bytes are blank. A nonpositive
    /// scale draws nothing. Glyph advance is six pixels times `scale`.
    fn text(&mut self, x: i32, y: i32, text: &[u8], scale: i32, color: u16);
}

impl TextCanvas for Raster<'_> {
    fn text(&mut self, mut x: i32, y: i32, text: &[u8], scale: i32, color: u16) {
        if scale <= 0 || !self.intersects_rows(y, 7_i32.saturating_mul(scale)) {
            return;
        }
        for &byte in text {
            for (row, bits) in glyph(byte.to_ascii_uppercase()).into_iter().enumerate() {
                for col in 0_i32..5 {
                    if bits & (1 << (4 - col)) != 0 {
                        self.fill(
                            Area {
                                x: x.saturating_add(col.saturating_mul(scale)),
                                y: y.saturating_add((row as i32).saturating_mul(scale)),
                                w: scale,
                                h: scale,
                            },
                            color,
                        );
                    }
                }
            }
            x = x.saturating_add(6_i32.saturating_mul(scale));
        }
    }
}
