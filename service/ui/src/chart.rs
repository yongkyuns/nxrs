//! Generic fixed-workspace trace projection and strip damage accounting.
use crate::surface::rectangle;
use display::{graphics::Area, Error};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Point<const SERIES: usize> {
    pub x: u16,
    pub y: [u16; SERIES],
}

/// Half-open damaged columns. The default value is empty.
#[derive(Clone, Copy, Default, Debug, Eq, PartialEq)]
pub struct Columns {
    pub left: u16,
    pub right: u16,
}

impl Columns {
    pub fn include(&mut self, range: Self) {
        if range.left < range.right {
            self.left = if self.left == self.right {
                range.left
            } else {
                self.left.min(range.left)
            };
            self.right = self.right.max(range.right);
        }
    }
}

/// Project normalized times and symmetric values into `plot` using existing
/// storage. `None` breaks a trace. Refuse extra samples instead of growing the
/// vector; after an error the caller must discard its incomplete projection.
pub fn project<const SERIES: usize>(
    plot: Area,
    inset: u16,
    limit: f32,
    samples: impl IntoIterator<Item = (f32, Option<[f32; SERIES]>)>,
    points: &mut Vec<Option<Point<SERIES>>>,
) -> Result<(), Error> {
    rectangle(plot)?;
    // Positive infinity preserves the established plot behavior when a source
    // value or automatic scale overflows. Finite values then project to zero;
    // the caller owns any policy that marks malformed sensor samples as gaps.
    if limit.is_nan() || limit <= 0.0 || plot.h / 2 < i32::from(inset) {
        return Err(Error::InvalidRegion);
    }
    points.clear();
    let center = plot.y + plot.h / 2;
    for (fraction, signal) in samples {
        if !(0.0..=1.0).contains(&fraction) {
            return Err(Error::InvalidRegion);
        }
        if points.len() == points.capacity() {
            return Err(Error::InvalidPixelCount);
        }
        points.push(signal.map(|values| Point {
            x: (plot.x + (fraction * (plot.w - 1) as f32) as i32) as u16,
            y: values.map(|value| {
                (center
                    - ((value / limit).clamp(-1.0, 1.0) * (plot.h / 2 - i32::from(inset)) as f32)
                        as i32)
                    .clamp(plot.y, plot.y + plot.h - 1) as u16
            }),
        }));
    }
    Ok(())
}

/// Fill damaged-column ranges for one-pixel lines and square markers at the
/// start of each trace. Markers extend right/down from the point and are clipped
/// to `plot`; supply the same nonzero `marker_size` used by the painter. Storage
/// and coordinates are checked before use. Union these ranges with the last
/// presented ranges to erase old traces.
pub fn damage<const SERIES: usize>(
    plot: Area,
    strip_height: u16,
    marker_size: u16,
    points: &[Option<Point<SERIES>>],
    bands: &mut [Columns],
) -> Result<(), Error> {
    let rect = rectangle(plot)?;
    if strip_height == 0
        || marker_size == 0
        || bands.len() < usize::from(rect.size.height).div_ceil(usize::from(strip_height))
    {
        return Err(Error::InvalidRegion);
    }
    let right = rect.x + rect.size.width;
    let bottom = rect.y + rect.size.height;
    if points.iter().flatten().any(|point| {
        !(rect.x..right).contains(&point.x) || point.y.iter().any(|y| !(rect.y..bottom).contains(y))
    }) {
        return Err(Error::InvalidRegion);
    }
    bands.fill(Columns::default());
    let mut previous: Option<Point<SERIES>> = None;
    for point in points {
        if let Some(point) = *point {
            for (axis, y) in point.y.into_iter().enumerate() {
                let (columns, top, last) = if let Some(from) = previous {
                    (
                        Columns {
                            left: point.x.min(from.x),
                            right: point.x.max(from.x) + 1,
                        },
                        y.min(from.y[axis]),
                        y.max(from.y[axis]),
                    )
                } else {
                    (
                        Columns {
                            left: point.x,
                            right: point.x.saturating_add(marker_size).min(right),
                        },
                        y,
                        y.saturating_add(marker_size - 1).min(bottom - 1),
                    )
                };
                let first = usize::from((top - rect.y) / strip_height);
                let last = usize::from((last - rect.y) / strip_height);
                for band in &mut bands[first..=last] {
                    band.include(columns);
                }
            }
        }
        previous = *point;
    }
    Ok(())
}
