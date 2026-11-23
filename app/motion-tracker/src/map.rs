//! A north-up Web Mercator tile for the currently visible GNSS fix.
use std::io::{self, Read};

pub const SIDE: usize = 170;
const PALETTE_BYTES: usize = 32;
const ROW_BYTES: usize = SIDE / 2;
const PIXEL_BYTES: usize = ROW_BYTES * SIDE;
const METADATA_BYTES: usize = PALETTE_BYTES + SIDE;
const BLOCK_BYTES: usize = 1024;
const BLOCKS: usize = PIXEL_BYTES.div_ceil(BLOCK_BYTES);
const MAGIC: &[u8; 4] = b"NXM5";
pub const REQUEST_BYTES: usize = 13;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Zoom(u8);

impl Zoom {
    pub fn new(level: u8) -> Option<Self> {
        (15..=18).contains(&level).then_some(Self(level))
    }

    pub fn level(self) -> u8 {
        self.0
    }

    fn tiles(self) -> u32 {
        1 << self.level()
    }
}

impl Default for Zoom {
    fn default() -> Self {
        Self(18)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Key {
    pub zoom: Zoom,
    pub x: u32,
    pub y: u32,
}

impl Key {
    pub fn request(self) -> [u8; REQUEST_BYTES] {
        let mut request = [0; REQUEST_BYTES];
        request[..4].copy_from_slice(MAGIC);
        request[4] = self.zoom.level();
        request[5..9].copy_from_slice(&self.x.to_be_bytes());
        request[9..13].copy_from_slice(&self.y.to_be_bytes());
        request
    }
}

#[derive(Clone, Copy)]
pub struct Position {
    pub key: Key,
    pub east: f64,
    pub south: f64,
}

impl Position {
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
}

/// Follow along the dominant direction so at most one neighboring tile
/// intersects the viewport. Tile-relative coordinates keep the seam continuous.
#[derive(Clone, Copy)]
pub struct Viewport {
    position: Position,
    offset: [f32; 2],
    neighbor: Option<Key>,
}

impl Viewport {
    pub fn keys(self) -> [Option<Key>; 2] {
        [Some(self.position.key), self.neighbor]
    }

    pub fn origin(self, key: Key) -> [f32; 2] {
        let [x, y] = self.position.key.delta(key);
        [x as f32 - self.offset[0], y as f32 - self.offset[1]]
    }

    pub fn marker(self) -> [f32; 2] {
        self.coordinates(self.position)
            .map(|value| value.clamp(0.0, 1.0 - f32::EPSILON))
    }

    pub fn project(self, point: Position) -> Option<[f32; 2]> {
        if !self.keys().contains(&Some(point.key)) {
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
        let dx = other.x as i32 - self.x as i32;
        [
            (dx + world / 2).rem_euclid(world) - world / 2,
            other.y as i32 - self.y as i32,
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

/// The display owns two complete tiles. Communications owns only the tile
/// being assembled or waiting to be handed over.
#[derive(Default)]
pub struct View {
    tiles: [Option<Tile>; 2],
    last: Option<Position>,
    heading: Option<(usize, i32)>,
}

impl View {
    pub fn select(&mut self, fix: Option<(Position, f32)>) -> Option<Viewport> {
        let Some((position, speed)) = fix else {
            self.last = None;
            self.heading = None;
            return None;
        };
        if let Some(last) = self.last.replace(position) {
            let [x, y] = last.key.delta(position.key);
            let delta = [
                f64::from(x) + position.east - last.east,
                f64::from(y) + position.south - last.south,
            ];
            let axis = usize::from(delta[1].abs() > delta[0].abs());
            let travel = delta[axis];
            if travel.abs() >= 1.0 {
                self.heading = None;
            } else if speed >= 0.8 && travel.abs() >= 0.0005 {
                self.heading = Some((axis, if travel > 0.0 { 1 } else { -1 }));
            }
        }
        let mut viewport = Viewport {
            position,
            offset: [0.0; 2],
            neighbor: None,
        };
        if let Some((axis, heading)) = self.heading {
            let offset = ([position.east, position.south][axis] - 0.5) as f32;
            let step = if offset > 0.0 { 1 } else { -1 };
            if let Some(neighbor) = position.key.adjacent(axis, step) {
                // Only ask for the area ahead. Keep the previous tile visible
                // behind us after a crossing if it is already available.
                if offset != 0.0 && (step == heading || self.tile(neighbor).is_some()) {
                    viewport.offset[axis] = offset;
                    viewport.neighbor = Some(neighbor);
                }
            }
        }
        Some(viewport)
    }

    pub fn wanted(&mut self, viewport: Viewport) -> Option<Key> {
        let visible = viewport.keys();
        let missing = visible
            .into_iter()
            .flatten()
            .find(|key| self.tile(*key).is_none());
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

    pub fn tile(&self, key: Key) -> Option<&Tile> {
        self.tiles.iter().flatten().find(|tile| tile.key == key)
    }
}

pub struct Tile {
    pub key: Key,
    pub palette: [u16; 16],
    pixels: Pixels,
}

impl Tile {
    pub fn row(&self, row: usize, colors: &mut [u8; SIDE]) {
        self.pixels.row(row, colors);
    }
}

// Keep validated scanlines in their wire encoding. Small blocks avoid
// requiring a contiguous tile allocation during BLE export and PPP.
struct Pixels {
    blocks: Vec<Vec<u8>>,
    row_ends: Vec<u16>,
}

impl Pixels {
    fn allocate() -> Result<Self, std::collections::TryReserveError> {
        let mut blocks = Vec::new();
        let mut row_ends = Vec::new();
        blocks.try_reserve_exact(BLOCKS)?;
        row_ends.try_reserve_exact(SIDE)?;
        Ok(Self { blocks, row_ends })
    }

    fn push(&mut self, mut bytes: &[u8]) -> Result<(), std::collections::TryReserveError> {
        let mut offset = usize::from(self.row_ends.last().copied().unwrap_or(0));
        while !bytes.is_empty() {
            if offset % BLOCK_BYTES == 0 {
                let mut block = Vec::new();
                block.try_reserve_exact((PIXEL_BYTES - offset).min(BLOCK_BYTES))?;
                self.blocks.push(block);
            }
            let length = bytes.len().min(BLOCK_BYTES - offset % BLOCK_BYTES);
            self.blocks
                .last_mut()
                .unwrap()
                .extend_from_slice(&bytes[..length]);
            offset += length;
            bytes = &bytes[length..];
        }
        self.row_ends.push(offset as u16);
        Ok(())
    }

    fn row(&self, y: usize, colors: &mut [u8; SIDE]) {
        let start = y
            .checked_sub(1)
            .map_or(0, |index| usize::from(self.row_ends[index]));
        let end = usize::from(self.row_ends[y]);
        let bytes =
            (start..end).map(|offset| self.blocks[offset / BLOCK_BYTES][offset % BLOCK_BYTES]);
        if end - start == ROW_BYTES {
            for (pair, byte) in colors.chunks_exact_mut(2).zip(bytes) {
                pair.copy_from_slice(&[byte >> 4, byte & 0x0f]);
            }
        } else {
            let mut offset = 0;
            for run in bytes {
                let next = offset + usize::from(run >> 4) + 1;
                colors[offset..next].fill(run & 0x0f);
                offset = next;
            }
        }
    }
}

struct Scanline {
    bytes: [u8; ROW_BYTES],
    row: usize,
    filled: usize,
}

impl Scanline {
    fn append(&mut self, pixels: &mut Pixels, lengths: &[u8], mut bytes: &[u8]) -> io::Result<()> {
        while !bytes.is_empty() {
            let length = usize::from(lengths[self.row]);
            let count = (length - self.filled).min(bytes.len());
            self.bytes[self.filled..self.filled + count].copy_from_slice(&bytes[..count]);
            self.filled += count;
            bytes = &bytes[count..];
            if self.filled == length {
                let row = &self.bytes[..length];
                if length < ROW_BYTES
                    && row
                        .iter()
                        .map(|run| usize::from(run >> 4) + 1)
                        .sum::<usize>()
                        != SIDE
                {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                pixels.push(row).map_err(|_| io::ErrorKind::OutOfMemory)?;
                self.row += 1;
                self.filled = 0;
            }
        }
        Ok(())
    }
}

/// One response can be split at any byte. Fixed metadata bounds the payload.
pub struct Receiver {
    header: [u8; REQUEST_BYTES + 1],
    received: usize,
    remaining: usize,
    pixels: Option<Pixels>,
    metadata: [u8; METADATA_BYTES],
    scanline: Scanline,
}

impl Default for Receiver {
    fn default() -> Self {
        Self {
            header: [0; REQUEST_BYTES + 1],
            received: 0,
            remaining: 0,
            pixels: None,
            metadata: [0; METADATA_BYTES],
            scanline: Scanline {
                bytes: [0; ROW_BYTES],
                row: 0,
                filled: 0,
            },
        }
    }
}

pub enum Response {
    Tile(Tile),
    Unavailable(Key),
}

impl Receiver {
    /// Free storage for an obsolete transfer even if the stream is blocked.
    /// Continue draining its remaining bytes before admitting another header.
    pub fn retain(&mut self, wanted: Option<Key>) {
        if self.pixels.is_some() && wanted != self.key() {
            self.pixels = None;
        }
    }

    pub fn poll(
        &mut self,
        source: &mut dyn Read,
        wanted: Option<Key>,
    ) -> io::Result<Option<Response>> {
        let end = if self.received < self.header.len() {
            self.received += read(source, &mut self.header[self.received..])?;
            if self.received < self.header.len() {
                return Ok(None);
            }
            let key = self.key().ok_or(io::ErrorKind::InvalidData)?;
            if &self.header[..4] != MAGIC {
                return Err(io::ErrorKind::InvalidData.into());
            }
            match self.header[REQUEST_BYTES] {
                0 => {
                    self.scanline.row = 0;
                    self.scanline.filled = 0;
                    if wanted == Some(key) {
                        self.pixels = Pixels::allocate()
                            .inspect_err(|_| {
                                eprintln!("motion-tracker map: insufficient tile storage")
                            })
                            .ok();
                    }
                    return Ok(None);
                }
                1 => Response::Unavailable(key),
                _ => return Err(io::ErrorKind::InvalidData.into()),
            }
        } else {
            self.retain(wanted);
            let offset = self.received - self.header.len();
            if offset < METADATA_BYTES {
                self.received += read(source, &mut self.metadata[offset..])?;
                if self.received == self.header.len() + METADATA_BYTES {
                    let lengths = &self.metadata[PALETTE_BYTES..];
                    if lengths
                        .iter()
                        .any(|&length| length == 0 || usize::from(length) > ROW_BYTES)
                    {
                        return Err(io::ErrorKind::InvalidData.into());
                    }
                    self.remaining = lengths.iter().map(|&length| usize::from(length)).sum();
                }
                return Ok(None);
            }
            let mut buffer = [0; 512];
            let limit = self.remaining.min(buffer.len());
            let count = read(source, &mut buffer[..limit])?;
            self.remaining -= count;
            if let Some(pixels) = &mut self.pixels {
                match self.scanline.append(
                    pixels,
                    &self.metadata[PALETTE_BYTES..],
                    &buffer[..count],
                ) {
                    Ok(()) => {}
                    Err(error) if error.kind() == io::ErrorKind::OutOfMemory => {
                        eprintln!("motion-tracker map: insufficient tile storage");
                        self.pixels = None;
                    }
                    Err(error) => return Err(error),
                }
            }
            if self.remaining != 0 {
                return Ok(None);
            }
            let Some(pixels) = self.pixels.take() else {
                self.received = 0;
                return Ok(None);
            };
            Response::Tile(Tile {
                key: self.key().unwrap(),
                palette: std::array::from_fn(|index| {
                    u16::from_be_bytes([self.metadata[index * 2], self.metadata[index * 2 + 1]])
                }),
                pixels,
            })
        };
        self.received = 0;
        Ok(Some(end))
    }

    fn key(&self) -> Option<Key> {
        let zoom = Zoom::new(self.header[4])?;
        let x = u32::from_be_bytes(self.header[5..9].try_into().unwrap());
        let y = u32::from_be_bytes(self.header[9..13].try_into().unwrap());
        (x < zoom.tiles() && y < zoom.tiles()).then_some(Key { zoom, x, y })
    }
}

fn read(source: &mut dyn Read, buffer: &mut [u8]) -> io::Result<usize> {
    match source.read(buffer) {
        Ok(0) => Err(io::ErrorKind::UnexpectedEof.into()),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
            ) =>
        {
            Ok(0)
        }
        result => result,
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
    fn rows_preserve_palette_indexes_in_both_encodings() {
        let mut pixels = Pixels::allocate().unwrap();
        let mut literal = [0x12; ROW_BYTES];
        literal[0] = 0x34;
        pixels.push(&literal).unwrap();
        let repeated = [0xab; ROW_BYTES];
        let mut flat = [0xfa; 11];
        flat[10] = 0x9a;
        pixels.push(&flat).unwrap();
        pixels.push(&repeated).unwrap();
        let mut row = [0; SIDE];
        pixels.row(0, &mut row);
        assert_eq!(&row[..6], &[3, 4, 1, 2, 1, 2]);
        pixels.row(1, &mut row);
        assert_eq!(row, [10; SIDE]);
        pixels.row(2, &mut row);
        assert_eq!(&row[..6], &[10, 11, 10, 11, 10, 11]);
    }

    #[test]
    fn requests_the_visible_tile_ahead_and_keeps_the_seam_on_crossing() {
        let west = tile_key(32767, 16384);
        let east = tile_key(0, 16384);
        let point = |key, x| Position {
            key,
            east: x,
            south: 0.75,
        };
        let mut map = View::default();

        let first = map.select(Some((point(west, 0.70), 0.0))).unwrap();
        assert_eq!(first.keys(), [Some(west), None]);
        assert_eq!(map.wanted(first), Some(west));
        map.accept(
            first,
            Tile {
                key: west,
                palette: [0; 16],
                pixels: Pixels::allocate().unwrap(),
            },
        );

        let parked = map.select(Some((point(west, 0.71), 0.1))).unwrap();
        assert_eq!(map.wanted(parked), None);
        let closing = map.select(Some((point(west, 0.90), 5.0))).unwrap();
        assert_eq!(map.wanted(closing), Some(east));
        assert!((closing.origin(east)[0] - 0.6).abs() < 0.0001);
        map.accept(
            closing,
            Tile {
                key: east,
                palette: [0; 16],
                pixels: Pixels::allocate().unwrap(),
            },
        );

        let crossed = map.select(Some((point(east, 0.1), 5.0))).unwrap();
        assert_eq!(crossed.keys(), [Some(east), Some(west)]);
        assert_eq!(map.wanted(crossed), None);
        assert!((crossed.origin(east)[0] - 0.4).abs() < 0.0001);
        assert_eq!(crossed.marker(), [0.5, 0.75]);
        assert!(map.select(None).is_none());
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
        map.accept(
            first,
            Tile {
                key: old,
                palette: [0; 16],
                pixels: Pixels::allocate().unwrap(),
            },
        );
        let closing = map.select(Some((point(old, 0.8), 3.0))).unwrap();
        assert_eq!(map.wanted(closing), Some(new));
        let crossed = map.select(Some((point(new, 0.05), 3.0))).unwrap();
        assert_eq!(map.wanted(crossed), Some(new));
        assert_eq!(crossed.keys(), [Some(new), Some(old)]);
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
    fn fragmented_wire_only_publishes_complete_tiles_and_checks_keys() {
        let key = Key {
            zoom: Zoom::new(18).unwrap(),
            x: 0x0002_fffe,
            y: 0x0003_ffff,
        };
        assert_eq!(&key.request(), b"NXM5\x12\0\x02\xff\xfe\0\x03\xff\xff");
        let mut receiver = Receiver::default();
        let mut frame = key.request().to_vec();
        frame.push(0);
        frame.extend([0xab; PALETTE_BYTES]);
        frame.extend([ROW_BYTES as u8; SIDE]);
        frame.extend(vec![0xab; PIXEL_BYTES]);
        for part in frame[..frame.len() - 1].chunks(17) {
            let mut data = part;
            while !data.is_empty() {
                assert!(receiver.poll(&mut data, Some(key)).unwrap().is_none());
            }
        }
        let Some(Response::Tile(tile)) = receiver
            .poll(&mut &frame[frame.len() - 1..], Some(key))
            .unwrap()
        else {
            panic!("expected a complete response");
        };
        assert_eq!(tile.key, key);
        assert_eq!(tile.palette, [0xabab; 16]);
        let mut row = [0; SIDE];
        for y in 0..SIDE {
            tile.row(y, &mut row);
            assert!(row.chunks_exact(2).all(|pair| pair == [10, 11]));
        }
        let mut unavailable = key.request().to_vec();
        unavailable.push(1);
        assert!(matches!(
            receiver.poll(&mut unavailable.as_slice(), Some(key)).unwrap(),
            Some(Response::Unavailable(received)) if received == key
        ));
        unavailable[10] = 4;
        assert!(receiver
            .poll(&mut unavailable.as_slice(), Some(key))
            .is_err());
    }
    #[test]
    fn rejects_scanlines_with_invalid_lengths_or_pixel_counts() {
        let key = tile_key(1, 2);
        let mut frame = key.request().to_vec();
        frame.push(0);
        frame.extend([0; PALETTE_BYTES]);
        for length in [0, ROW_BYTES as u8 + 1, 1] {
            let mut packet = frame.clone();
            packet.extend([length; SIDE]);
            packet.extend([0xfa; SIDE]);
            let mut data = packet.as_slice();
            let mut receiver = Receiver::default();
            assert!(receiver.poll(&mut data, Some(key)).unwrap().is_none());
            let result = receiver.poll(&mut data, Some(key));
            let invalid = if length == 1 {
                assert!(result.unwrap().is_none());
                receiver.poll(&mut data, Some(key))
            } else {
                result
            };
            assert!(matches!(invalid, Err(error) if error.kind() == io::ErrorKind::InvalidData));
        }
    }

    #[test]
    fn abandoning_a_partial_tile_releases_it_and_preserves_stream_framing() {
        let old = tile_key(1, 2);
        let new = tile_key(3, 4);
        let mut receiver = Receiver::default();
        let mut frame = old.request().to_vec();
        frame.push(0);
        frame.extend([0; PALETTE_BYTES]);
        frame.extend([ROW_BYTES as u8; SIDE]);
        frame.extend(vec![0; PIXEL_BYTES]);
        frame.extend(new.request());
        frame.push(1);
        let mut data = frame.as_slice();
        assert!(receiver.poll(&mut data, Some(old)).unwrap().is_none());
        assert!(receiver.pixels.is_some());
        receiver.retain(Some(new));
        assert!(receiver.pixels.is_none());
        let response = loop {
            if let Some(response) = receiver.poll(&mut data, Some(new)).unwrap() {
                break response;
            }
        };
        assert!(matches!(response, Response::Unavailable(key) if key == new));
        assert!(data.is_empty());
    }
}
