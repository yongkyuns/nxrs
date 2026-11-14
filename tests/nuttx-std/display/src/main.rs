use display::{Display, Error, Rect, Size};

fn main() {
    let mut screen = display::open().expect("open display");
    assert!(matches!(display::open(), Err(Error::Busy)));
    let size = screen.size();
    assert_eq!(
        size,
        Size {
            width: 320,
            height: 240
        }
    );
    let scanline = Size {
        width: size.width,
        height: 1,
    };
    assert_eq!(
        screen.draw(
            Rect {
                x: size.width,
                y: 0,
                size: scanline
            },
            &[]
        ),
        Err(Error::InvalidRegion)
    );

    let mut row = [0; 320];
    for (stripe, color) in row
        .chunks_exact_mut(80)
        .zip([0xf800, 0x07e0, 0x001f, 0xffff])
    {
        stripe.fill(color);
    }
    for y in 0..size.height {
        screen
            .draw(
                Rect {
                    x: 0,
                    y,
                    size: scanline,
                },
                &row,
            )
            .unwrap();
    }
    screen
        .draw(
            Rect {
                x: 0,
                y: 0,
                size: Size {
                    width: 8,
                    height: 8,
                },
            },
            &[0; 64],
        )
        .unwrap();
    println!("DISPLAY PASS {}x{} RGB565", size.width, size.height);
}
