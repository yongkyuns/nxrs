//! North-up map projection, viewport, and display tile cache.
use crate::{Key, Tile, Zoom, SIDE};

const VIEWPORT_TILES: usize = 4;

/// Projected point within a tile: fractional east/south offsets are in `0..1`.
#[derive(Clone, Copy, Debug)]
pub struct Position {
    pub key: Key,
    pub east: f64,
    pub south: f64,
}

impl Position {
    /// Project a WGS84 latitude/longitude onto the supported Web Mercator map.
    ///
    /// Coordinates must be finite, latitude within ±85.05112878 degrees and
    /// longitude within ±180 degrees. Both antimeridian endpoints map to x=0.
    pub fn project(latitude: f64, longitude: f64, zoom: Zoom) -> Option<Self> {
        if !(-85.051_128_78..=85.051_128_78).contains(&latitude)
            || !(-180.0..=180.0).contains(&longitude)
        {
            return None;
        }
        let tiles = f64::from(zoom.tiles());
        let east = (longitude + 180.0).rem_euclid(360.0) * tiles / 360.0;
        let sin_lat = latitude.to_radians().sin();
        let mercator = ((1.0 + sin_lat) / (1.0 - sin_lat)).ln() * 0.5;
        let south =
            ((1.0 - mercator / std::f64::consts::PI) * tiles / 2.0).clamp(0.0, tiles - 1e-8);
        Some(Self {
            key: Key {
                zoom,
                x: east as u32,
                y: south as u32,
            },
            east: east.fract(),
            south: south.fract(),
        })
    }

    /// Validate a position constructed through its public fields.
    pub fn is_valid(self) -> bool {
        self.key.is_valid() && (0.0..1.0).contains(&self.east) && (0.0..1.0).contains(&self.south)
    }
}

/// A one-tile-wide square centered on the fix. The tile keys are ordered
/// for downloading, with the current tile first.
#[derive(Clone, Copy)]
pub struct Viewport {
    position: Position,
    offset: [f32; 2],
    keys: [Option<Key>; VIEWPORT_TILES],
}

impl Viewport {
    /// At most four intersecting tiles in fetch order, starting under the fix.
    pub fn keys(self) -> [Option<Key>; VIEWPORT_TILES] {
        self.keys
    }

    /// Tile origin relative to this viewport, in tile-width units.
    ///
    /// Supply a tile at the viewport's zoom, normally from [`Viewport::keys`].
    /// Coordinates outside `0..1` describe a tile partially or fully offscreen.
    /// East/west distances wrap at the antimeridian.
    pub fn origin(self, key: Key) -> [f32; 2] {
        let [x, y] = self.position.key.delta(key);
        [x as f32 - self.offset[0], y as f32 - self.offset[1]]
    }

    /// Locate a valid point inside the view, rejecting a different zoom or an
    /// offscreen point. Returned coordinates are in the square `[0, 1)²`.
    pub fn project(self, point: Position) -> Option<[f32; 2]> {
        if !point.is_valid() || point.key.zoom != self.position.key.zoom {
            return None;
        }
        let within = self.coordinates(point);
        within
            .iter()
            .all(|value| (0.0..1.0).contains(value))
            .then_some(within)
    }

    fn coordinates(self, point: Position) -> [f32; 2] {
        let [x, y] = self.origin(point.key);
        [x + point.east as f32, y + point.south as f32]
    }
}

impl Key {
    fn delta(self, other: Self) -> [i32; 2] {
        let world = self.zoom.tiles() as i32;
        // Projected positions are validated before use. Explicit wrapping also
        // makes origin() safe for arbitrary public key fields without adding
        // 64-bit arithmetic to the normal embedded projection path.
        let dx = (other.x as i32).wrapping_sub(self.x as i32);
        [
            dx.wrapping_add(world / 2).rem_euclid(world) - world / 2,
            (other.y as i32).wrapping_sub(self.y as i32),
        ]
    }

    fn adjacent(self, axis: usize, step: i32) -> Option<Self> {
        let world = self.zoom.tiles() as i32;
        match axis {
            0 => Some(Self {
                x: (self.x as i32 + step).rem_euclid(world) as u32,
                ..self
            }),
            _ => {
                let y = self.y as i32 + step;
                (0..world).contains(&y).then_some(Self {
                    y: y as u32,
                    ..self
                })
            }
        }
    }
}

/// Synchronous viewport selection and a cache of at most four complete tiles.
///
/// The caller owns drawing and fetch timing. The receiver retains the current
/// reply until it is complete and handed to [`View::accept`]. No worker, queue,
/// or additional tile copy is introduced by the view.
#[derive(Default)]
pub struct View {
    tiles: [Option<Tile>; VIEWPORT_TILES],
    last: Option<Position>,
    heading: Option<(usize, i32)>,
    pending: Option<Key>,
}

impl View {
    /// Center a one-tile-wide view on a valid position and remember travel
    /// direction for fetch ordering. Speed is in metres per second; negative
    /// or nonfinite speed disables the direction hint without hiding the map.
    ///
    /// `None` or an invalid position clears the current fix, heading and pending
    /// request. Previously received tiles remain bounded in the cache for reuse.
    pub fn select(&mut self, fix: Option<(Position, f32)>) -> Option<Viewport> {
        let Some((position, speed)) = fix.filter(|(position, _)| position.is_valid()) else {
            self.last = None;
            self.heading = None;
            self.pending = None;
            return None;
        };
        if let Some(last) = self
            .last
            .replace(position)
            .filter(|last| last.key.zoom == position.key.zoom)
        {
            let [x, y] = last.key.delta(position.key);
            let delta = [
                f64::from(x) + position.east - last.east,
                f64::from(y) + position.south - last.south,
            ];
            let axis = usize::from(delta[1].abs() > delta[0].abs());
            let travel = delta[axis];
            if !speed.is_finite() || speed < 0.8 || travel.abs() >= 1.0 {
                self.heading = None;
            } else if travel.abs() >= 0.0005 {
                self.heading = Some((axis, if travel > 0.0 { 1 } else { -1 }));
            }
        } else {
            self.heading = None;
        }
        let offset = [position.east as f32 - 0.5, position.south as f32 - 0.5];
        let step = offset.map(|value| if value > 0.0 { 1 } else { -1 });
        let neighbor = |axis: usize| {
            // A sliver that rounds to zero display pixels needs no download.
            (offset[axis].abs() >= 0.5 / SIDE as f32)
                .then(|| position.key.adjacent(axis, step[axis]))
                .flatten()
        };
        let horizontal = neighbor(0);
        let vertical = neighbor(1);
        let diagonal = horizontal.zip(vertical).map(|(x, y)| Key { y: y.y, ..x });
        let mut keys = [Some(position.key), horizontal, vertical, diagonal];
        if horizontal.is_none() || self.heading == Some((1, step[1])) {
            keys.swap(1, 2);
        }
        Some(Viewport {
            position,
            offset,
            keys,
        })
    }

    /// Select the next missing visible tile, preserving an in-flight visible
    /// request. Obsolete cached tiles are released when more data is needed.
    pub fn wanted(&mut self, viewport: Viewport) -> Option<Key> {
        let visible = viewport.keys();
        let missing = |key: &Key| self.tile(*key).is_none();
        // Finish a still-visible reply even if the motion estimate changes.
        // The tile under the marker goes first if it has not arrived yet.
        let missing = visible[0]
            .filter(missing)
            .or_else(|| {
                self.pending
                    .filter(|key| visible.contains(&Some(*key)) && missing(key))
            })
            .or_else(|| visible[1..].iter().flatten().copied().find(missing));
        self.pending = missing;
        if missing.is_some() {
            for slot in &mut self.tiles {
                if slot
                    .as_ref()
                    .is_some_and(|tile| !visible.contains(&Some(tile.key)))
                {
                    *slot = None;
                }
            }
        }
        missing
    }

    /// Retain a complete visible tile, dropping duplicate or obsolete replies.
    pub fn accept(&mut self, viewport: Viewport, tile: Tile) {
        let visible = viewport.keys();
        if !visible.contains(&Some(tile.key)) || self.tile(tile.key).is_some() {
            return;
        }
        if let Some(slot) = self.tiles.iter_mut().find(|slot| {
            slot.as_ref()
                .is_none_or(|tile| !visible.contains(&Some(tile.key)))
        }) {
            *slot = Some(tile);
        }
    }

    /// Borrow an already cached tile without copying or decoding it.
    pub fn tile(&self, key: Key) -> Option<&Tile> {
        self.tiles.iter().flatten().find(|tile| tile.key == key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tile_key(x: u32, y: u32) -> Key {
        Key {
            zoom: Zoom::new(15).unwrap(),
            x,
            y,
        }
    }

    #[test]
    fn requests_the_visible_tile_ahead_and_keeps_the_seam_on_crossing() {
        let west = tile_key(32767, 16384);
        let east = tile_key(0, 16384);
        let point = |key, x| Position {
            key,
            east: x,
            south: 0.5,
        };
        let mut map = View::default();

        let first = map.select(Some((point(west, 0.5), 0.0))).unwrap();
        assert_eq!(first.keys(), [Some(west), None, None, None]);
        assert_eq!(map.wanted(first), Some(west));
        map.accept(first, Tile::blank_for_test(west));

        let parked = map.select(Some((point(west, 0.5), 0.1))).unwrap();
        assert_eq!(map.wanted(parked), None);
        let closing = map.select(Some((point(west, 0.90), 5.0))).unwrap();
        assert_eq!(map.wanted(closing), Some(east));
        assert!((closing.origin(east)[0] - 0.6).abs() < 0.0001);
        map.accept(closing, Tile::blank_for_test(east));

        let crossed = map.select(Some((point(east, 0.1), 5.0))).unwrap();
        assert_eq!(crossed.keys(), [Some(east), Some(west), None, None]);
        assert_eq!(map.wanted(crossed), None);
        assert!((crossed.origin(east)[0] - 0.4).abs() < 0.0001);
        assert_eq!(crossed.project(point(east, 0.1)), Some([0.5, 0.5]));
        assert!(map.select(None).is_none());
    }

    #[test]
    fn centers_a_stationary_fix_and_loads_all_four_visible_tiles() {
        let key = tile_key(20, 30);
        let point = Position {
            key,
            east: 0.75,
            south: 0.25,
        };
        let mut map = View::default();
        let viewport = map.select(Some((point, 0.0))).unwrap();
        assert_eq!(viewport.project(point), Some([0.5, 0.5]));
        let expected = [key, tile_key(21, 30), tile_key(20, 29), tile_key(21, 29)];
        assert_eq!(viewport.keys(), expected.map(Some));
        assert_eq!(viewport.origin(expected[0]), [-0.25, 0.25]);
        assert_eq!(viewport.origin(expected[3]), [0.75, -0.75]);
        for key in expected {
            assert_eq!(map.wanted(viewport), Some(key));
            map.accept(viewport, Tile::blank_for_test(key));
        }
        assert_eq!(map.wanted(viewport), None);
        assert!(expected.into_iter().all(|key| map.tile(key).is_some()));
    }

    #[test]
    fn old_tile_can_remain_visible_when_the_new_tile_is_delayed() {
        let old = tile_key(20, 30);
        let new = tile_key(20, 31);
        let point = |key, south| Position {
            key,
            east: 0.5,
            south,
        };
        let mut map = View::default();
        let first = map.select(Some((point(old, 0.7), 0.0))).unwrap();
        map.accept(first, Tile::blank_for_test(old));
        let closing = map.select(Some((point(old, 0.8), 3.0))).unwrap();
        assert_eq!(map.wanted(closing), Some(new));
        let crossed = map.select(Some((point(new, 0.05), 3.0))).unwrap();
        assert_eq!(map.wanted(crossed), Some(new));
        assert_eq!(crossed.keys(), [Some(new), Some(old), None, None]);
        assert!(map.tile(old).is_some());
    }

    #[test]
    fn projects_equator_and_wraps_antimeridian() {
        let wide = Zoom::new(15).unwrap();
        let origin = Position::project(0.0, 0.0, wide).unwrap();
        assert_eq!(origin.key, tile_key(16384, 16384));
        assert_eq!((origin.east, origin.south), (0.0, 0.0));
        let negative = Position::project(45.0, -180.0, wide).unwrap();
        let positive = Position::project(45.0, 180.0, wide).unwrap();
        assert_eq!(negative.key, positive.key);
        assert!(positive.key.y < origin.key.y);
        let zoom = Zoom::new(18).unwrap();
        let detail = Position::project(0.0, 0.0, zoom).unwrap();
        assert_eq!(
            detail.key,
            Key {
                zoom,
                x: 131072,
                y: 131072
            }
        );
        assert_eq!(detail.key.adjacent(0, -1).unwrap().x, 131071);
        let edge = Position::project(0.0, 180.0, zoom).unwrap();
        assert_eq!(edge.key.adjacent(0, -1).unwrap().x, 262143);
        assert!(Position::project(90.0, 0.0, wide).is_none());
        assert!(Position::project(0.0, f64::NAN, wide).is_none());
    }

    #[test]
    fn malformed_public_positions_are_rejected_without_corrupting_the_view() {
        let good = Position {
            key: tile_key(1, 2),
            east: 0.5,
            south: 0.5,
        };
        let mut view = View::default();
        let viewport = view.select(Some((good, 1.0))).unwrap();
        let invalid = [
            Position {
                east: f64::NAN,
                ..good
            },
            Position {
                south: f64::INFINITY,
                ..good
            },
            Position { east: 1.0, ..good },
            Position {
                south: -0.1,
                ..good
            },
            Position {
                key: tile_key(u32::MAX, 2),
                ..good
            },
            Position {
                key: tile_key(1, u32::MAX),
                ..good
            },
        ];
        for point in invalid {
            assert!(view.select(Some((point, 1.0))).is_none());
            assert!(viewport.project(point).is_none());
            assert!(view.last.is_none());
            assert!(view.pending.is_none());
            assert!(view.heading.is_none());
        }
        let recovered = view.select(Some((good, f32::NAN))).unwrap();
        assert_eq!(recovered.project(good), Some([0.5, 0.5]));
        assert_eq!(view.wanted(recovered), Some(good.key));

        // The unchecked origin helper also remains overflow-safe for arbitrary
        // public key fields, even though callers normally use viewport keys.
        assert!(viewport
            .origin(tile_key(u32::MAX, u32::MAX))
            .into_iter()
            .all(f32::is_finite));
    }

    #[test]
    fn changing_zoom_restarts_direction_hints_and_selects_new_tiles() {
        let position = Position {
            key: tile_key(10, 20),
            east: 0.75,
            south: 0.75,
        };
        let mut view = View::default();
        view.select(Some((position, 1.0))).unwrap();
        view.select(Some((
            Position {
                south: 0.8,
                ..position
            },
            1.0,
        )))
        .unwrap();
        assert_eq!(view.heading, Some((1, 1)));
        let zoom = Zoom::new(16).unwrap();
        let point = Position {
            key: Key::new(zoom, 21, 41).unwrap(),
            east: 0.5,
            south: 0.6,
        };
        let viewport = view.select(Some((point, 1.0))).unwrap();
        assert_eq!(view.heading, None);
        assert_eq!(view.wanted(viewport), Some(point.key));
        assert!(viewport.project(position).is_none());
    }
}
