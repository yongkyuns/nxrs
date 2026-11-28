# NXM5 compact maps

`nxrs-map-service` owns bounded NXM5 raster tile decoding, geographic projection,
visible tile selection, and a four-tile cache. Its Rust crate name is `maps`.
The protocol uses 170-pixel indexed tiles at zoom levels 15–18; the view is a
one-tile-wide, north-up square. Applications can use that contract with their
own transport, screen dimensions and fetch preference. Motion Tracker supplies
its travel-direction policy and renderer.

The service is synchronous. It creates no thread, queue, socket, TLS identity,
timer, or display surface. The caller owns those resources and determines when
to advance the receiver and the view. The only external dependency is the
existing `log` facade for tile allocation failures.

## Compose a view

```rust
use maps::{Position, View, Zoom};

let mut view = View::default();
let zoom = Zoom::new(18).unwrap();
let position = Position::project(43.45, -79.68, zoom).unwrap();
let viewport = view.select(Some(position), None).unwrap();
let requested = view.wanted(viewport);
assert_eq!(requested, Some(position.key));
```

`Position::project` accepts finite latitude/longitude within Web Mercator's
latitude range and ±180 degrees longitude. East and west meet at the
antimeridian. `Position::offset_to` returns wrapped east/south displacement in
tile-width units for valid positions at the same zoom. `Viewport::keys` returns
at most four intersecting tiles with the tile under the center first. Every
geometric overlap is included, so scaling to a larger display cannot hide a
neighbor that covers only a fraction of an encoded pixel.

The optional `Direction` argument prioritizes a visible neighbor. With no hint,
horizontal precedes vertical; the diagonal is last. The caller decides whether
the preference follows motion, a user interaction or another policy. No speed,
update cadence or travel threshold is embedded in the view.
`View::wanted` preserves a still-visible in-flight request and drops obsolete
cached tiles when another tile is needed. The app passes its selected key to the
transport owner; request timing, retries, and BLE/IP choice remain there.

The caller drives `wire::Receiver::poll` with a `Read` implementation and the
wanted key. When it returns `Response::Tile(tile)`, pass the tile by value to
`view.accept(viewport, tile)`. Then borrow it with `view.tile(key)` and decode each
scanline with `tile.row(row, &mut palette_indexes)`. The RGB565 palette is exposed
on the tile; display clipping, pixel conversion, and layout belong to the caller.
`row` rejects an out-of-range index without modifying the output array.

`View::select(None, None)` clears the pending request while retaining the bounded
cache for reuse. Invalid manually constructed positions have the same effect.
The application owns any direction history and its reset policy.

## Preserve stream ownership

The public `wire` module contains the unchanged `NXM5` tile protocol:

| Field | Encoding |
| --- | --- |
| Request | `NXM5`, one-byte zoom (15–18), big-endian `u32` x and y; 13 bytes |
| Response prefix | Request bytes followed by status: 0 for a tile, 1 for unavailable |
| Tile metadata | Sixteen big-endian RGB565 palette values and 170 row lengths |
| Row payload | 85 packed palette-index bytes, or a shorter run-length row |

A packed byte holds two four-bit palette indexes. A run byte contains a four-bit
run length minus one and a four-bit palette index. Every compressed row must
expand to exactly 170 pixels. The parser validates response keys, metadata,
lengths, and retained scanlines before exposing a complete tile.

Each `poll` performs at most one read and processes at most 512 payload bytes.
The supplied reader determines whether that read can block. `WouldBlock` and
`Interrupted` retain progress; EOF and malformed input are terminal errors.
After a terminal error, partial storage is released and further polls return
the same error kind without consuming input. Replace the receiver when starting
a fresh stream; byte synchronization is not guessed after an error.

`retain` drops storage for an obsolete response immediately. The receiver still
drains its declared bounded payload before admitting the next header. Allocation
failure follows the same discard-and-drain path and emits a warning. The
transport owner decides when to retry. Network connections and TLS identity
remain with the app communications owner, so this crate introduces no independent
network lifecycle.

## Storage and execution budget

A tile holds at most 14,450 encoded pixel bytes in at most fifteen blocks of
1,024 bytes or less, plus 170 `u16` row offsets, sixteen RGB565 palette entries,
the key, and vector metadata. The view retains at most four complete tiles.
Each receiver may retain one incomplete reply. The caller must also account for
any completed tile held between the receiver and view, and for each separate
receiver it creates.

The existing fallible vector allocation strategy is preserved. This extraction
does not provide preallocated storage or claim allocation-free downloads.
Projection, request encoding, cache selection, row decoding, and cache lookups
do not allocate. No additional tile copy or queue is introduced. `log` reuses
the logger installed by the application.

Tests cover fragmented responses, both row encodings, invalid keys and row
lengths, obsolete-response draining, terminal errors, malformed public inputs,
four-tile ordering, map seams, delayed tiles, and zoom transitions. An independent
consumer test decodes NXM5 responses, hands complete tiles to the view and renders
their rows at 170-, 320- and 640-pixel output sizes through the public API.
