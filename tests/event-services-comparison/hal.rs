//! ESP32-S3 onboard LED control with GPIO pad readback.
//!
//! The application must initialize `SystemTimer` before constructing or using
//! this driver so Unit0 is running for the short settling interval.

use esp_hal::{
    gpio::{Flex, Level, OutputConfig, Pin},
    timer::systimer::{SystemTimer, Unit},
};

pub struct Led {
    pin: Flex<'static>,
}

impl Led {
    /// Configure an owned pin as the onboard LED output with input sampling.
    pub fn new(pin: impl Pin + 'static) -> Self {
        let mut pin = Flex::new(pin);
        pin.apply_output_config(&OutputConfig::default());
        pin.set_low();
        // esp-hal 1.0 keeps input-buffer enable separate from OutputConfig.
        pin.set_input_enable(true);
        pin.set_output_enable(true);
        Self { pin }
    }

    /// Set the pin level and verify it through the GPIO input buffer.
    pub fn apply(&mut self, level: bool) -> Result<(), ()> {
        self.pin
            .set_level(if level { Level::High } else { Level::Low });
        settle_one_us();
        if self.pin.is_high() == level {
            Ok(())
        } else {
            Err(())
        }
    }

    /// Turn the LED off and verify the pad has reached a low level.
    pub fn turn_off(&mut self) -> Result<(), ()> {
        self.apply(false)
    }
}

fn settle_one_us() {
    let start = SystemTimer::unit_value(Unit::Unit0);
    let ticks_per_us = SystemTimer::ticks_per_second() / 1_000_000;
    while SystemTimer::unit_value(Unit::Unit0).wrapping_sub(start) < ticks_per_us {}
}
