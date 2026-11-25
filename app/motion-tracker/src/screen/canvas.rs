use super::font::glyph;
use crate::communications::{Tile, SIDE};
use display::graphics::{Area, Raster};

pub(super) trait CanvasExt {
    fn map(&mut self, area: Area, origin: (i32, i32), tile: &Tile);
    fn text(&mut self, x: i32, y: i32, text: &[u8], scale: i32, color: u16);
}

impl CanvasExt for Raster<'_> {
    fn map(&mut self, area: Area, origin: (i32, i32), tile: &Tile) {
        let mut colors = [0; SIDE];
        let (x0, y0) = origin;
        let left = area.x.max(x0);
        let top = area.y.max(y0);
        let visible = Area {
            x: left,
            y: top,
            w: (area.x + area.w).min(x0 + area.w) - left,
            h: (area.y + area.h).min(y0 + area.h) - top,
        };
        for (left, y, pixels) in self.rows_mut(visible) {
            let row = (y - y0) as usize * SIDE / area.h as usize;
            tile.row(row, &mut colors);
            for (offset, pixel) in pixels.iter_mut().enumerate() {
                let column = (left - x0 + offset as i32) as usize * SIDE / area.w as usize;
                *pixel = tile.palette[usize::from(colors[column])];
            }
        }
    }

    fn text(&mut self, mut x: i32, y: i32, text: &[u8], scale: i32, color: u16) {
        if !self.intersects_rows(y, 7 * scale) {
            return;
        }
        for &byte in text {
            for (row, bits) in glyph(byte.to_ascii_uppercase()).into_iter().enumerate() {
                for col in 0..5 {
                    if bits & (1 << (4 - col)) != 0 {
                        self.fill(
                            Area {
                                x: x + col * scale,
                                y: y + row as i32 * scale,
                                w: scale,
                                h: scale,
                            },
                            color,
                        );
                    }
                }
            }
            x += 6 * scale;
        }
    }
}
