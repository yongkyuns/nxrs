use display::graphics::{Area, Raster};
use maps::{Tile, SIDE};

pub(super) trait CanvasExt {
    fn map(&mut self, area: Area, origin: (i32, i32), tile: &Tile);
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
            if tile.row(row, &mut colors).is_err() {
                continue;
            }
            for (offset, pixel) in pixels.iter_mut().enumerate() {
                let column = (left - x0 + offset as i32) as usize * SIDE / area.w as usize;
                *pixel = tile.palette[usize::from(colors[column])];
            }
        }
    }
}
