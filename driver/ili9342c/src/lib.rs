//! Write-only ILI9342C commands and RGB565 encoding for 320 × 240 panels.

use std::{thread, time::Duration};

pub const WIDTH: u16 = 320;
pub const HEIGHT: u16 = 240;

/// Send one command and its parameters under one chip-select assertion.
pub trait Interface {
    type Error;
    fn write(&mut self, command: u8, data: &[u8]) -> Result<(), Self::Error>;
}

#[derive(Debug, PartialEq, Eq)]
pub enum Error<E> {
    InvalidRegion,
    InvalidPixelCount,
    Transport(E),
}

pub struct Panel<I>(I);

impl<I: Interface> Panel<I> {
    /// Initialize the controller after its supply and external reset are ready.
    pub fn initialize(mut bus: I) -> Result<Self, I::Error> {
        bus.write(0x01, &[])?; // Software reset
        thread::sleep(Duration::from_millis(150));
        // CoreS3 ILI9342C register values from M5GFX. Coordinate and color
        // settings match Zephyr's CoreS3 profile.
        for &(command, data) in &[
            (0xc8, &[0xff, 0x93, 0x42][..]), // Extended commands
            (0xc0, &[0x12, 0x12]),           // Power control 1
            (0xc1, &[0x03]),                 // Power control 2
            (0xc5, &[0xf2]),                 // VCOM
            (0xb0, &[0xe0]),                 // Interface mode
            (0xf6, &[0x01, 0x00, 0x00]),     // Interface control, MSB first
            (
                0xe0,
                &[
                    0x00, 0x0c, 0x11, 0x04, 0x11, 0x08, 0x37, 0x89, 0x4c, 0x06, 0x0c, 0x0a, 0x2e,
                    0x34, 0x0f,
                ],
            ),
            (
                0xe1,
                &[
                    0x00, 0x0b, 0x11, 0x05, 0x13, 0x09, 0x33, 0x67, 0x48, 0x07, 0x0e, 0x0b, 0x2e,
                    0x33, 0x0f,
                ],
            ),
            (0xb6, &[0x08, 0x82, 0x1d, 0x04]), // Display function control
            (0x36, &[0x08]),                   // Native 320 × 240 orientation, BGR panel wiring
            (0x3a, &[0x55]),                   // RGB565
            (0x21, &[]),                       // Display inversion on
            (0x38, &[]),                       // Idle mode off
            (0x11, &[]),                       // Sleep out
        ] {
            bus.write(command, data)?;
        }
        thread::sleep(Duration::from_millis(120));
        bus.write(0x29, &[])?; // Display on
        Ok(Self(bus))
    }

    /// Write a packed rectangle. The panel's origin is (0, 0).
    pub fn draw(
        &mut self,
        x: u16,
        y: u16,
        width: u16,
        height: u16,
        pixels: &[u16],
    ) -> Result<(), Error<I::Error>> {
        let right = x
            .checked_add(width)
            .filter(|&edge| edge <= WIDTH && width != 0);
        let bottom = y
            .checked_add(height)
            .filter(|&edge| edge <= HEIGHT && height != 0);
        let (Some(right), Some(bottom)) = (right, bottom) else {
            return Err(Error::InvalidRegion);
        };
        let row_pixels = usize::from(width);
        if pixels.len() != row_pixels * usize::from(height) {
            return Err(Error::InvalidPixelCount);
        }
        let mut bytes = [0; WIDTH as usize * 2];
        let row = &mut bytes[..row_pixels * 2];

        // Open a separate window per row. The complete RAMWR command and row
        // remain under one chip-select assertion even when the controller splits
        // the payload into FIFO transfers.
        let [x_hi, x_lo] = x.to_be_bytes();
        let [right_hi, right_lo] = (right - 1).to_be_bytes();
        self.0
            .write(0x2a, &[x_hi, x_lo, right_hi, right_lo])
            .map_err(Error::Transport)?;
        for (y, pixels) in (y..bottom).zip(pixels.chunks_exact(row_pixels)) {
            let [hi, lo] = y.to_be_bytes();
            self.0
                .write(0x2b, &[hi, lo, hi, lo])
                .map_err(Error::Transport)?;
            for (value, output) in pixels.iter().zip(row.chunks_exact_mut(2)) {
                output.copy_from_slice(&value.to_be_bytes());
            }
            self.0.write(0x2c, row).map_err(Error::Transport)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Trace {
        writes: Vec<(u8, Vec<u8>)>,
    }

    impl Interface for Trace {
        type Error = ();

        fn write(&mut self, command: u8, data: &[u8]) -> Result<(), Self::Error> {
            self.writes.push((command, data.to_vec()));
            Ok(())
        }
    }

    #[test]
    fn writes_inclusive_windows_and_msb_first_rows() {
        let mut panel = Panel(Trace::default());
        panel
            .draw(318, 238, 2, 2, &[0xf800, 0x07e0, 0x001f, 0xffff])
            .unwrap();
        assert_eq!(
            panel.0.writes,
            [
                (0x2a, vec![1, 62, 1, 63]),
                (0x2b, vec![0, 238, 0, 238]),
                (0x2c, vec![0xf8, 0, 0x07, 0xe0]),
                (0x2b, vec![0, 239, 0, 239]),
                (0x2c, vec![0, 0x1f, 0xff, 0xff]),
            ],
        );
    }

    #[test]
    fn rejects_invalid_input_before_spi() {
        let mut panel = Panel(Trace::default());
        assert_eq!(panel.draw(319, 0, 2, 1, &[0; 2]), Err(Error::InvalidRegion));
        assert_eq!(
            panel.draw(u16::MAX, 0, 2, 1, &[0; 2]),
            Err(Error::InvalidRegion)
        );
        assert_eq!(panel.draw(0, 0, 1, 0, &[]), Err(Error::InvalidRegion));
        assert_eq!(
            panel.draw(0, 0, 2, 2, &[0; 3]),
            Err(Error::InvalidPixelCount)
        );
        assert!(panel.0.writes.is_empty());
    }
}
