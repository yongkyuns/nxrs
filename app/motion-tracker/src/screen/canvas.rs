use super::font::glyph;

#[derive(Clone, Copy)]
pub(super) struct Area {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl Area {
    pub fn contains(self, x: i32, y: i32) -> bool {
        (self.x..self.x + self.w).contains(&x) && (self.y..self.y + self.h).contains(&y)
    }
}

pub(super) struct Canvas<'a> {
    pub pixels: &'a mut [u16],
    pub top: i32,
    pub width: usize,
}

impl Canvas<'_> {
    pub fn intersects_rows(&self, y: i32, height: i32) -> bool {
        y + height > self.top && y < self.top + (self.pixels.len() / self.width) as i32
    }

    pub fn fill(&mut self, area: Area, color: u16) {
        let left = area.x.max(0) as usize;
        let right = (area.x + area.w).clamp(0, self.width as i32) as usize;
        let bottom = self.top + (self.pixels.len() / self.width) as i32;
        if right <= left {
            return;
        }
        for y in area.y.max(self.top)..(area.y + area.h).min(bottom) {
            let row = (y - self.top) as usize * self.width;
            self.pixels[row + left..row + right].fill(color);
        }
    }

    pub fn line(&mut self, mut start: (i32, i32), end: (i32, i32), color: u16) {
        let bottom = self.top + (self.pixels.len() / self.width) as i32;
        if start.1.max(end.1) < self.top || start.1.min(end.1) >= bottom {
            return;
        }
        let dx = (end.0 - start.0).abs();
        let dy = -(end.1 - start.1).abs();
        let sx = if start.0 < end.0 { 1 } else { -1 };
        let sy = if start.1 < end.1 { 1 } else { -1 };
        let mut err = dx + dy;
        loop {
            self.fill(
                Area {
                    x: start.0,
                    y: start.1,
                    w: 1,
                    h: 1,
                },
                color,
            );
            if start == end {
                break;
            }
            let delta = 2 * err;
            if delta >= dy {
                err += dy;
                start.0 += sx;
            }
            if delta <= dx {
                err += dx;
                start.1 += sy;
            }
        }
    }

    pub fn map(&mut self, area: Area, origin: (i32, i32), tile: &crate::map::Tile) {
        use crate::map::SIDE;
        let mut colors = [0; SIDE];
        let (x0, y0) = origin;
        let bottom = self.top + (self.pixels.len() / self.width) as i32;
        let left = area.x.max(x0);
        let right = (area.x + area.w).min(x0 + area.w);
        for y in area.y.max(self.top).max(y0)..(area.y + area.h).min(bottom).min(y0 + area.h) {
            let row = (y - y0) as usize * SIDE / area.h as usize;
            let start = (y - self.top) as usize * self.width;
            tile.row(row, &mut colors);
            for x in left..right {
                let column = (x - x0) as usize * SIDE / area.w as usize;
                self.pixels[start + x as usize] = tile.palette[usize::from(colors[column])];
            }
        }
    }

    pub fn text(&mut self, mut x: i32, y: i32, text: &[u8], scale: i32, color: u16) {
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
