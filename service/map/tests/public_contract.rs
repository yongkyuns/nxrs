use maps::{wire, Direction, Key, Position, Tile, View, Viewport, Zoom, SIDE};
use std::io::Cursor;

fn key(x: u32, y: u32) -> Key {
    Key::new(Zoom::new(15).unwrap(), x, y).unwrap()
}

fn decode(key: Key, color: u16) -> Tile {
    let mut frame = key.request().to_vec();
    frame.push(0);
    for _ in 0..16 {
        frame.extend(color.to_be_bytes());
    }
    // Each row has ten runs of sixteen pixels and a final run of ten.
    frame.extend([11; SIDE]);
    for _ in 0..SIDE {
        frame.extend([0xf0; 10]);
        frame.push(0x90);
    }
    let mut source = Cursor::new(frame);
    let mut receiver = wire::Receiver::default();
    while (source.position() as usize) < source.get_ref().len() {
        if let Some(response) = receiver.poll(&mut source, Some(key)).unwrap() {
            let wire::Response::Tile(tile) = response else {
                panic!("tile response was lost");
            };
            assert_eq!(source.position() as usize, source.get_ref().len());
            return tile;
        }
    }
    panic!("complete response did not produce a tile");
}

fn render(view: &View, viewport: Viewport, side: usize) -> Vec<u16> {
    let mut output = vec![0; side * side];
    let anchor = viewport.origin(viewport.keys()[0].unwrap());
    let pixels = anchor.map(|offset| (offset * side as f32).round() as i32);
    for key in viewport.keys().into_iter().flatten() {
        let tile = view.tile(key).unwrap();
        let origin = viewport.origin(key);
        let x = pixels[0] + (origin[0] - anchor[0]).round() as i32 * side as i32;
        let y = pixels[1] + (origin[1] - anchor[1]).round() as i32 * side as i32;
        let mut row = [0; SIDE];
        for screen_y in y.max(0)..(y + side as i32).min(side as i32) {
            tile.row((screen_y - y) as usize * SIDE / side, &mut row)
                .unwrap();
            for screen_x in x.max(0)..(x + side as i32).min(side as i32) {
                let column = (screen_x - x) as usize * SIDE / side;
                output[screen_y as usize * side + screen_x as usize] =
                    tile.palette[usize::from(row[column])];
            }
        }
    }
    output
}

#[test]
fn public_decoder_and_view_cover_scaled_displays_without_a_sensor_or_tracker() {
    for (east, south, right_edge) in [
        (0.5025, 0.5, [1, 2, 2]),
        (0.5025, 0.4975, [1, 2, 2]),
        (0.75, 0.75, [2, 2, 2]),
        (0.75, 0.25, [2, 2, 2]),
    ] {
        let position = Position {
            key: key(10, 20),
            east,
            south,
        };
        let mut view = View::default();
        let viewport = view.select(Some(position), None).unwrap();
        for _ in 0..4 {
            let Some(wanted) = view.wanted(viewport) else {
                break;
            };
            // Distinct nonzero colors expose gaps and wrong tile placement.
            let color = 1 + (wanted.x % 2 + 2 * (wanted.y % 2)) as u16;
            view.accept(viewport, decode(wanted, color));
        }
        assert_eq!(view.wanted(viewport), None);
        assert!(viewport.keys().contains(&Some(key(11, 20))));
        for (side, expected) in [170, 320, 640].into_iter().zip(right_edge) {
            let pixels = render(&view, viewport, side);
            assert!(pixels.iter().all(|&pixel| pixel != 0), "gap at {side}px");
            let middle_right = pixels[(side / 2) * side + side - 1];
            assert_eq!(middle_right, expected);
        }
    }
}

#[test]
fn caller_preference_preserves_the_visible_pending_request_and_geometry_wraps() {
    let position = Position {
        key: key(10, 20),
        east: 0.75,
        south: 0.75,
    };
    let mut view = View::default();
    let south_first = view.select(Some(position), Some(Direction::South)).unwrap();
    assert_eq!(south_first.keys()[1], Some(key(10, 21)));
    view.accept(south_first, decode(position.key, 1));
    assert_eq!(view.wanted(south_first), Some(key(10, 21)));

    let east_first = view.select(Some(position), Some(Direction::East)).unwrap();
    assert_eq!(east_first.keys()[1], Some(key(11, 20)));
    assert_eq!(view.wanted(east_first), Some(key(10, 21)));
    view.accept(east_first, decode(key(10, 21), 2));
    assert_eq!(view.wanted(east_first), Some(key(11, 20)));
    assert!(view.select(None, None).is_none());
    assert!(view.tile(position.key).is_some());

    let west = Position {
        key: key(32767, 20),
        east: 0.9,
        ..position
    };
    let east = Position {
        key: key(0, 20),
        east: 0.1,
        ..position
    };
    let [horizontal, vertical] = west.offset_to(east).unwrap();
    assert!((horizontal - 0.2).abs() < 1e-12);
    assert_eq!(vertical, 0.0);
    assert!(west
        .offset_to(Position {
            key: Key::new(Zoom::new(16).unwrap(), 0, 40).unwrap(),
            ..east
        })
        .is_none());
}
