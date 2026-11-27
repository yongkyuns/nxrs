use display::{
    graphics::{Area, Raster},
    Display, Error, Rect, Size,
};
use std::time::{Duration, Instant};
use touch::{Event, Point};
use ui::{
    chart,
    gesture::{Gesture, Recognizer},
    History, Surface, TextCanvas,
};

struct Panel {
    pixels: [u16; 48],
    writes: Vec<Rect>,
    fail_on: Option<usize>,
}

impl Panel {
    fn new() -> Self {
        Self {
            pixels: [99; 48],
            writes: Vec::new(),
            fail_on: None,
        }
    }
}

impl Display for Panel {
    fn size(&self) -> Size {
        Size {
            width: 8,
            height: 6,
        }
    }

    fn draw(&mut self, rect: Rect, pixels: &[u16]) -> Result<(), Error> {
        self.writes.push(rect);
        if self.fail_on == Some(self.writes.len()) {
            return Err(Error::Io);
        }
        for (offset, row) in pixels
            .chunks_exact(usize::from(rect.size.width))
            .enumerate()
        {
            let first = (usize::from(rect.y) + offset) * 8 + usize::from(rect.x);
            self.pixels[first..first + row.len()].copy_from_slice(row);
        }
        Ok(())
    }

    fn draw_strip(&mut self, rect: Rect, pixels: &mut [u16]) -> Result<(), Error> {
        let result = self.draw(rect, pixels);
        pixels.fill(0xffff);
        result
    }
}

const FULL: Area = Area {
    x: 0,
    y: 0,
    w: 8,
    h: 6,
};

#[test]
fn bounded_strips_refill_mutated_scratch_and_clip_to_requested_columns() {
    let mut surface = Surface::new(Panel::new(), 2).unwrap();
    surface
        .draw(
            FULL,
            3,
            |band| Some(Area { x: 2, w: 3, ..band }),
            |canvas| {
                canvas.fill(
                    Area {
                        x: 3,
                        y: 0,
                        w: 1,
                        h: 6,
                    },
                    7,
                );
            },
        )
        .unwrap();
    assert_eq!(surface.display().writes.len(), 3);
    assert!(surface
        .display()
        .writes
        .iter()
        .all(|r| r.size.width == 3 && r.size.height == 2));
    for row in surface.display().pixels.chunks_exact(8) {
        assert_eq!(row, &[99, 99, 3, 7, 3, 99, 99, 99]);
    }
}

#[test]
fn display_error_stops_the_transfer_and_a_full_retry_restores_all_pixels() {
    let mut panel = Panel::new();
    panel.fail_on = Some(2);
    let mut surface = Surface::new(panel, 2).unwrap();
    assert_eq!(surface.draw(FULL, 4, Some, |_| {}), Err(Error::Io));
    assert_eq!(surface.display().writes.len(), 2);
    assert!(surface.display().pixels[..16].iter().all(|p| *p == 4));
    assert!(surface.display().pixels[16..].iter().all(|p| *p == 99));
    surface.draw(FULL, 5, Some, |_| {}).unwrap();
    assert!(surface.display().pixels.iter().all(|p| *p == 5));
}

#[test]
fn invalid_surface_geometry_or_clipping_is_rejected_before_transferring() {
    assert!(matches!(
        Surface::new(Panel::new(), 0),
        Err(Error::InvalidRegion)
    ));
    assert!(matches!(
        Surface::new(Panel::new(), 7),
        Err(Error::InvalidRegion)
    ));
    let mut surface = Surface::new(Panel::new(), 2).unwrap();
    for area in [
        Area { x: -1, ..FULL },
        Area { w: 9, ..FULL },
        Area { w: 0, ..FULL },
        Area {
            x: i32::MAX,
            w: i32::MAX,
            ..FULL
        },
    ] {
        assert_eq!(
            surface.draw(area, 0, Some, |_| panic!("invalid area reached paint")),
            Err(Error::InvalidRegion)
        );
    }
    assert_eq!(
        surface.draw(
            FULL,
            0,
            |band| Some(Area { h: 3, ..band }),
            |_| panic!("invalid clip reached paint")
        ),
        Err(Error::InvalidRegion)
    );
    assert!(surface.display().writes.is_empty());
}

#[test]
fn lost_contacts_cannot_tap_and_a_vertical_swipe_is_emitted_once() {
    let mut gestures = Recognizer::new(14, 32);
    let origin = Point { x: 5, y: 5 };
    assert_eq!(gestures.observe(Event::Pressed(origin), false), None);
    assert_eq!(gestures.observe(Event::Released, true), None);
    gestures.observe(Event::Pressed(origin), false);
    assert_eq!(
        gestures.observe(Event::Moved(Point { x: 7, y: 40 }), false),
        Some(Gesture::Down)
    );
    assert_eq!(
        gestures.observe(Event::Moved(Point { x: 7, y: 50 }), false),
        None
    );
    assert_eq!(gestures.observe(Event::Released, false), None);
    gestures.observe(Event::Pressed(origin), false);
    assert_eq!(
        gestures.observe(Event::Released, false),
        Some(Gesture::Tap(origin))
    );
}

#[test]
fn history_evicts_before_reusing_its_capacity_and_keeps_explicit_gaps() {
    let now = Instant::now();
    let mut history = History::<Option<u32>, 3>::new();
    for index in 0..5 {
        history.record(
            now + Duration::from_secs(index),
            (index != 3).then_some(index as u32),
        );
    }
    let points: Vec<_> = history
        .window(now + Duration::from_secs(4), Duration::from_secs(4))
        .map(|(x, value)| (x, *value))
        .collect();
    assert_eq!(points, [(0.5, Some(2)), (0.75, None), (1.0, Some(4))]);
    assert_eq!(history.window(now, Duration::ZERO).count(), 0);
}

#[test]
fn projected_trace_gaps_do_not_connect_and_capacity_never_grows() {
    let plot = Area {
        x: 4,
        y: 10,
        w: 20,
        h: 20,
    };
    let mut points = Vec::with_capacity(3);
    let capacity = points.capacity();
    chart::project(
        plot,
        2,
        1.0,
        [(0.0, Some([1.0])), (0.5, None), (1.0, Some([-1.0]))],
        &mut points,
    )
    .unwrap();
    assert_eq!(
        points,
        [
            Some(chart::Point { x: 4, y: [12] }),
            None,
            Some(chart::Point { x: 23, y: [28] })
        ]
    );
    let mut bands = [chart::Columns::default(); 5];
    chart::damage(plot, 4, &points, &mut bands).unwrap();
    assert_eq!(bands[0], chart::Columns { left: 4, right: 6 });
    assert!(bands[1..4].iter().all(|b| *b == chart::Columns::default()));
    assert_eq!(
        bands[4],
        chart::Columns {
            left: 23,
            right: 24
        }
    );
    let samples = std::iter::repeat_n((0.5, Some([0.0])), capacity + 1);
    assert_eq!(
        chart::project(plot, 2, 1.0, samples, &mut points),
        Err(Error::InvalidPixelCount)
    );
    assert_eq!(points.capacity(), capacity);
    assert_eq!(
        chart::damage(plot, 4, &points, &mut []),
        Err(Error::InvalidRegion)
    );
}

#[test]
fn infinite_auto_scale_preserves_the_existing_projection_instead_of_failing_the_screen() {
    let plot = Area {
        x: 4,
        y: 10,
        w: 20,
        h: 20,
    };
    let mut points = Vec::with_capacity(2);
    chart::project(
        plot,
        4,
        f32::INFINITY,
        [
            (0.0, Some([1.0, f32::INFINITY, f32::NEG_INFINITY])),
            (1.0, Some([f32::NAN, 0.0, 9.8])),
        ],
        &mut points,
    )
    .unwrap();
    assert_eq!(
        points,
        [
            Some(chart::Point { x: 4, y: [20; 3] }),
            Some(chart::Point { x: 23, y: [20; 3] }),
        ]
    );
}

#[test]
fn text_glyphs_are_identical_when_drawn_as_independent_clipped_rows() {
    let rect = Rect {
        x: 0,
        y: 0,
        size: Size {
            width: 18,
            height: 9,
        },
    };
    let mut full = [0; 162];
    Raster::new(rect, &mut full)
        .unwrap()
        .text(-2, 1, b"a9", 1, 7);
    let mut clipped = [0; 162];
    for (row, pixels) in clipped.chunks_exact_mut(18).enumerate() {
        let strip = Rect {
            y: row as u16,
            size: Size {
                height: 1,
                ..rect.size
            },
            ..rect
        };
        Raster::new(strip, pixels).unwrap().text(-2, 1, b"a9", 1, 7);
    }
    assert_eq!(full, clipped);
    let mut raster = Raster::new(rect, &mut clipped).unwrap();
    raster.text(i32::MAX, i32::MAX, b"A", i32::MAX, 9);
    assert_eq!(full, clipped);
}
