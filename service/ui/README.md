# Synchronous presentation

`nxrs-ui-service` (`ui`) supplies reusable rendering and input mechanics above
the display and touch HALs. Applications own their screens, page selection,
colors, labels, gestures' meaning and scheduling deadlines.

## Ownership and progress

Construct `Surface<D>` with an already acquired `Display` and a nonzero strip
height. The surface exclusively owns that display and one RGB565 scratch
buffer. `draw` divides an on-screen region into bounded transfers, lets the
caller select smaller damaged regions, clears each scratch region to the
specified background and calls the supplied painter. The painter uses the
existing display `Raster` primitives and `TextCanvas` bitmap text extension.
Every transfer finishes synchronously before the buffer is reused. A display
provider may modify the scratch pixels during transfer.

The service starts no task, acquires no hardware and waits on no queue. Drive it
from an existing execution owner. A surface error may follow a partially drawn
frame: commit scene and damage state only after every required draw succeeds,
and invalidate the scene for a full repaint on failure. A direct write through
`display_mut` similarly requires the caller to invalidate its scene state.

`gesture::Recognizer` consumes normalized touch events and recognizes taps and
vertical swipes using caller-provided thresholds. Pass `missed = true` when
touch events were lost; this discards the old contact before interpreting the
next event. Gesture-to-action mapping remains with the application.

## Histories and chart workspaces

`History<T, CAPACITY>` reserves a bounded timestamped history once. Inserting
while full evicts the oldest value before appending. Its time-window iterator
returns normalized positions and borrowed values without allocating; the
application decides sampling frequency and how sensor states map to gaps.
A zero-capacity history is a configuration error. Dropping evicted values runs
their normal Rust destructors, so use bounded lightweight values on a timed path.

`chart::project` projects a caller's series into an existing vector. It refuses
to grow the vector when its capacity is exhausted. `None` separates traces.
`chart::damage` fills an existing strip-range array. Combine the current and
previously presented ranges with `Columns::include` before repainting, then
commit the new damage only after presentation succeeds. The caller chooses
series count, scale, plot dimensions, marker inset and workspace capacities.
Damage assumes one-pixel lines and square markers extending right/down from
each trace's first point, clipped to the plot. Pass the painter's nonzero marker
side length so its pixels and damage bounds agree. Motion Tracker uses the same
two-pixel marker constant in both calls; another renderer can choose a different
marker size.

## Cost and portability

The surface allocates exactly one `width * strip_height * 2`-byte pixel buffer
at construction. Motion Tracker continues to use eight rows: 5,120 pixel bytes
at width 320. The history retains its original bounded ring allocation, and
chart projection and damage reuse the application's original buffers. Ordinary
rendering, text, gesture and chart operations introduce no allocations, threads,
queues or synchronization. Metadata and allocator overhead are separate from
these pixel bytes. The service adds no third-party dependency; it consumes the
existing portable display and touch contracts and Rust `std`.

Unit/integration tests cover clipped transfer bounds, mutable scratch reuse,
display failure and recovery, dropped touch events, bounded history eviction,
projection capacity and damage gaps. Motion Tracker retains pixel-level tests
for partial/full redraw equivalence, chart deadlines, GNSS bars and pairing.
Target footprint and timing remain the responsibility of the existing firmware
qualification profile; host rendering tests do not establish device timing.
