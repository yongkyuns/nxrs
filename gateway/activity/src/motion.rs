//! Small grayscale comparison with lighting compensation, warmup and temporal confirmation.
//! This reports movement, not a person, pet, identity or security incident.

pub const WIDTH: usize = 96;
pub const HEIGHT: usize = 72;
pub const PIXELS: usize = WIDTH * HEIGHT;

pub struct Detector {
    previous: Option<Box<[u8; PIXELS]>>,
    warmup: u8,
    consecutive: u8,
    threshold: u16,
}
impl Detector {
    /// Required percentage of changed pixels, 1..=50. Two samples confirm movement.
    pub fn new(percent: u16) -> Result<Self, &'static str> {
        if !(1..=50).contains(&percent) {
            return Err("invalid motion sensitivity");
        }
        Ok(Self {
            previous: None,
            warmup: 4,
            consecutive: 0,
            threshold: percent * 10,
        })
    }
    pub fn reset(&mut self) {
        self.previous = None;
        self.warmup = 4;
        self.consecutive = 0;
    }
    pub fn observe(&mut self, frame: &[u8; PIXELS]) -> bool {
        let Some(previous) = self.previous.replace(Box::new(*frame)) else {
            return false;
        };
        // Median signed difference removes a uniform exposure/lighting adjustment.
        let mut differences = [0u32; 511];
        for (&a, &b) in frame.iter().zip(previous.iter()) {
            differences[(i16::from(a) - i16::from(b) + 255) as usize] += 1;
        }
        let mut count = 0;
        let mut offset = 0;
        for (index, &n) in differences.iter().enumerate() {
            count += n;
            if count >= (PIXELS / 2) as u32 {
                offset = index as i16 - 255;
                break;
            }
        }
        let changed = frame
            .iter()
            .zip(previous.iter())
            .filter(|(a, b)| (i16::from(**a) - i16::from(**b) - offset).unsigned_abs() >= 20)
            .count();
        if self.warmup > 0 {
            self.warmup -= 1;
            return false;
        }
        if changed * 1000 >= PIXELS * usize::from(self.threshold) {
            self.consecutive = self.consecutive.saturating_add(1);
        } else {
            self.consecutive = 0;
        }
        self.consecutive >= 2
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn ready() -> Detector {
        let mut d = Detector::new(3).unwrap();
        for _ in 0..6 {
            assert!(!d.observe(&[40; PIXELS]));
        }
        d
    }
    #[test]
    fn static_noise_and_exposure_do_not_trigger() {
        let mut d = ready();
        for value in [42, 43, 80, 110, 60] {
            assert!(!d.observe(&[value; PIXELS]));
        }
        let mut noise = [60; PIXELS];
        for pixel in noise.iter_mut().step_by(10) {
            *pixel += 5;
        }
        assert!(!d.observe(&noise));
    }
    #[test]
    fn movement_requires_confirmation_and_recovers_after_reset() {
        let mut d = ready();
        let mut a = [40; PIXELS];
        a[..1000].fill(140);
        assert!(!d.observe(&a));
        assert!(d.observe(&[40; PIXELS]));
        assert!(!d.observe(&[40; PIXELS]));
        d.reset();
        assert!(!d.observe(&a));
    }
}
