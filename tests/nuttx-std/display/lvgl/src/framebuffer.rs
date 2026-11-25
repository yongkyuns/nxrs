use crate::lvgl;
use display::{Display, Error, Rect, Size};

const SIZE: Size = Size {
    width: 320,
    height: 240,
};
const LABEL: &std::ffi::CStr = c"CoreS3 display ready";

#[derive(Default)]
struct PixelCheck {
    rows: u16,
    chunks: usize,
    ink: usize,
    fail_at: Option<usize>,
}

impl Display for PixelCheck {
    fn size(&self) -> Size {
        SIZE
    }

    fn draw(&mut self, area: Rect, pixels: &[u16]) -> Result<(), Error> {
        self.chunks += 1;
        if self.fail_at == Some(self.chunks) {
            return Err(Error::Io);
        }
        let width = usize::from(SIZE.width);
        if area.x != 0
            || area.y != self.rows
            || area.size.width != SIZE.width
            || area.size.height != 24
            || pixels.len() != width * 24
        {
            return Err(Error::InvalidRegion);
        }
        for (index, pixel) in pixels.iter().copied().enumerate() {
            if pixel != u16::MAX {
                let x = index % width;
                let y = usize::from(area.y) + index / width;
                if pixel != 0 || !(80..240).contains(&x) || !(116..126).contains(&y) {
                    return Err(Error::Io);
                }
                self.ink += 1;
            }
        }
        self.rows += area.size.height;
        Ok(())
    }
}

pub fn qualify() {
    let mut failure = PixelCheck {
        fail_at: Some(4),
        ..PixelCheck::default()
    };
    assert!(matches!(
        lvgl::render_label(&mut failure, LABEL),
        Err(lvgl::Error::Display(Error::Io))
    ));
    assert_eq!(failure.chunks, 4);

    for _ in 0..2 {
        let mut frame = PixelCheck::default();
        assert_eq!(lvgl::render_label(&mut frame, LABEL).unwrap(), 10);
        assert_eq!(frame.rows, SIZE.height);
        assert_eq!(frame.ink, 369);
    }
    println!("LVGL FRAMEBUFFER PASS 320x240 RGB565 (10 flushes, 369 ink pixels, recovery)");
}
