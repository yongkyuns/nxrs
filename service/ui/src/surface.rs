use display::graphics::{Area, Raster};
use display::{Display, Error, Rect, Size};

/// Exclusive synchronous display owner with one preallocated RGB565 strip.
pub struct Surface<D> {
    display: D,
    pixels: Box<[u16]>,
    size: Size,
    strip_height: u16,
}

impl<D: Display> Surface<D> {
    /// Reserve `width * strip_height` pixels once. Both dimensions and the
    /// strip height must be nonzero; a strip cannot exceed the display height.
    pub fn new(display: D, strip_height: u16) -> Result<Self, Error> {
        let size = display.size();
        if size.width == 0 || strip_height == 0 || strip_height > size.height {
            return Err(Error::InvalidRegion);
        }
        Ok(Self {
            display,
            pixels: vec![0; usize::from(size.width) * usize::from(strip_height)].into_boxed_slice(),
            size,
            strip_height,
        })
    }

    pub fn size(&self) -> Size {
        self.size
    }

    pub fn strip_height(&self) -> u16 {
        self.strip_height
    }

    pub fn display(&self) -> &D {
        &self.display
    }

    /// Direct display access invalidates any scene state retained by the caller.
    pub fn display_mut(&mut self) -> &mut D {
        &mut self.display
    }

    pub fn into_display(self) -> D {
        self.display
    }

    /// Paint selected portions of `area` in strips without allocating.
    ///
    /// `clip` can skip a strip or return a nonempty subrectangle of it. The
    /// scratch buffer is refilled before every paint because display transfers
    /// may modify it. An error can leave a partial frame on the display; the
    /// caller must only commit its scene/damage state after this returns `Ok`.
    pub fn draw(
        &mut self,
        area: Area,
        background: u16,
        clip: impl Fn(Area) -> Option<Area>,
        paint: impl Fn(&mut Raster<'_>),
    ) -> Result<(), Error> {
        let rect = rectangle(area)?;
        if u32::from(rect.x) + u32::from(rect.size.width) > u32::from(self.size.width)
            || u32::from(rect.y) + u32::from(rect.size.height) > u32::from(self.size.height)
        {
            return Err(Error::InvalidRegion);
        }
        let end = area.y + area.h;
        let mut y = area.y;
        while y < end {
            let height = i32::from(self.strip_height).min(end - y);
            let band = Area {
                y,
                h: height,
                ..area
            };
            y += height;
            let Some(strip) = clip(band) else {
                continue;
            };
            let rect = rectangle(strip)?;
            if strip.x < band.x
                || strip.y < band.y
                || strip.x + strip.w > band.x + band.w
                || strip.y + strip.h > band.y + band.h
            {
                return Err(Error::InvalidRegion);
            }
            let count = usize::from(rect.size.width) * usize::from(rect.size.height);
            let pixels = &mut self.pixels[..count];
            pixels.fill(background);
            paint(&mut Raster::new(rect, pixels)?);
            self.display.draw_strip(rect, pixels)?;
        }
        Ok(())
    }
}

pub(crate) fn rectangle(area: Area) -> Result<Rect, Error> {
    let x = u16::try_from(area.x).map_err(|_| Error::InvalidRegion)?;
    let y = u16::try_from(area.y).map_err(|_| Error::InvalidRegion)?;
    let width = u16::try_from(area.w).map_err(|_| Error::InvalidRegion)?;
    let height = u16::try_from(area.h).map_err(|_| Error::InvalidRegion)?;
    if width == 0
        || height == 0
        || x.checked_add(width).is_none()
        || y.checked_add(height).is_none()
    {
        return Err(Error::InvalidRegion);
    }
    Ok(Rect {
        x,
        y,
        size: Size { width, height },
    })
}
