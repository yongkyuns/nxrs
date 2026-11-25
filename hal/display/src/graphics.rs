//! Clipped drawing primitives for packed RGB565 regions.

use crate::{Error, Rect};

/// A rectangle in screen coordinates. Pixels at the right and bottom edges are excluded.
#[derive(Clone, Copy)]
pub struct Area {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl Area {
    pub fn contains(self, x: i32, y: i32) -> bool {
        (self.x..self.x.saturating_add(self.w)).contains(&x)
            && (self.y..self.y.saturating_add(self.h)).contains(&y)
    }
}

/// A packed RGB565 rectangle with an origin in screen coordinates.
pub struct Raster<'a> {
    pixels: &'a mut [u16],
    area: Area,
}

impl<'a> Raster<'a> {
    pub fn new(rect: Rect, pixels: &'a mut [u16]) -> Result<Self, Error> {
        let (width, height) = (rect.size.width, rect.size.height);
        if width == 0 || height == 0 {
            return Err(Error::InvalidRegion);
        }
        if pixels.len() != usize::from(width) * usize::from(height) {
            return Err(Error::InvalidPixelCount);
        }
        Ok(Self {
            pixels,
            area: Area {
                x: rect.x.into(),
                y: rect.y.into(),
                w: width.into(),
                h: height.into(),
            },
        })
    }

    pub fn area(&self) -> Area {
        self.area
    }

    pub fn intersects_rows(&self, y: i32, height: i32) -> bool {
        height > 0 && y.saturating_add(height) > self.area.y && y < self.area.y + self.area.h
    }

    /// Return the screen origin and writable pixels of each visible row of `area`.
    pub fn rows_mut(&mut self, area: Area) -> impl Iterator<Item = (i32, i32, &mut [u16])> {
        let left = area.x.clamp(self.area.x, self.area.x + self.area.w);
        let right = area
            .x
            .saturating_add(area.w)
            .clamp(left, self.area.x + self.area.w);
        let top = area.y.clamp(self.area.y, self.area.y + self.area.h);
        let bottom = area
            .y
            .saturating_add(area.h)
            .clamp(top, self.area.y + self.area.h);
        let columns = (left - self.area.x) as usize..(right - self.area.x) as usize;
        self.pixels
            .chunks_exact_mut(self.area.w as usize)
            .skip((top - self.area.y) as usize)
            .take(if left == right {
                0
            } else {
                (bottom - top) as usize
            })
            .enumerate()
            .map(move |(offset, row)| (left, top + offset as i32, &mut row[columns.clone()]))
    }

    pub fn fill(&mut self, area: Area, color: u16) {
        for (_, _, row) in self.rows_mut(area) {
            row.fill(color);
        }
    }

    pub fn line(&mut self, start: (i32, i32), end: (i32, i32), color: u16) {
        let right = self.area.x + self.area.w - 1;
        let bottom = self.area.y + self.area.h - 1;
        if start.0.max(end.0) < self.area.x
            || start.0.min(end.0) > right
            || start.1.max(end.1) < self.area.y
            || start.1.min(end.1) > bottom
        {
            return;
        }
        if start.0 == end.0 || start.1 == end.1 {
            let x = start.0.min(end.0).max(self.area.x);
            let y = start.1.min(end.1).max(self.area.y);
            self.fill(
                Area {
                    x,
                    y,
                    w: start.0.max(end.0).min(right) - x + 1,
                    h: start.1.max(end.1).min(bottom) - y + 1,
                },
                color,
            );
            return;
        }

        let dx = (i64::from(end.0) - i64::from(start.0)).unsigned_abs();
        let dy = (i64::from(end.1) - i64::from(start.1)).unsigned_abs();
        let x_major = dx >= dy;
        let (major, minor, len, rise, lower, upper, major_end, minor_end) = if x_major {
            (start.0, start.1, dx, dy, self.area.x, right, end.0, end.1)
        } else {
            (start.1, start.0, dy, dx, self.area.y, bottom, end.1, end.0)
        };
        let origin = i64::from(major);
        let direction = if major < major_end { 1 } else { -1 };
        let minor_direction = if minor < minor_end { 1 } else { -1 };
        let (first, last) = if direction > 0 {
            (i64::from(lower) - origin, i64::from(upper) - origin)
        } else {
            (origin - i64::from(upper), origin - i64::from(lower))
        };
        let first = first.max(0) as u64;
        let last = (last as u64).min(len);
        // Advance Bresenham directly to the first visible major-axis step.
        // The rounded product fits in u64 even for the full i32 coordinate range.
        let (offset, mut error) = if first == 0 {
            (0, len / 2)
        } else {
            let numerator = first * rise + len / 2;
            (numerator / len, numerator % len)
        };
        let mut major = origin + direction * first as i64;
        let mut minor = i64::from(minor) + minor_direction * offset as i64;
        for _ in first..=last {
            let (x, y) = if x_major {
                (major, minor)
            } else {
                (minor, major)
            };
            if self.area.contains(x as i32, y as i32) {
                let column = (x - i64::from(self.area.x)) as usize;
                let row = (y - i64::from(self.area.y)) as usize;
                self.pixels[row * self.area.w as usize + column] = color;
            }
            major += direction;
            error += rise;
            if error >= len {
                error -= len;
                minor += minor_direction;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Size;

    #[test]
    fn clipped_lines_match_the_same_lines_drawn_into_a_full_buffer() {
        let full = Rect {
            x: 0,
            y: 0,
            size: Size {
                width: 12,
                height: 12,
            },
        };
        let strip = Rect {
            x: 3,
            y: 4,
            size: Size {
                width: 5,
                height: 4,
            },
        };
        for start in [(0, 0), (10, 1), (11, 11), (2, 9), (3, 4)] {
            for y in 0..12 {
                for x in 0..12 {
                    let mut screen = [0; 144];
                    Raster::new(full, &mut screen)
                        .unwrap()
                        .line(start, (x, y), 1);
                    let mut pixels = [0; 20];
                    Raster::new(strip, &mut pixels)
                        .unwrap()
                        .line(start, (x, y), 1);
                    assert!(pixels
                        .chunks_exact(5)
                        .zip(screen.chunks_exact(12).skip(4))
                        .all(|(strip, row)| strip == &row[3..8]));
                }
            }
        }
    }
}
