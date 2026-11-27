//! Product travel thresholds used to prioritize visible map downloads.
use maps::{Direction, Position};

#[derive(Default)]
pub(super) struct DirectionHint {
    last: Option<Position>,
    direction: Option<Direction>,
}

impl DirectionHint {
    pub(super) fn observe(&mut self, fix: Option<(Position, f32)>) -> Option<Direction> {
        let Some((position, speed)) = fix else {
            self.last = None;
            self.direction = None;
            return None;
        };
        if let Some(delta) = self
            .last
            .replace(position)
            .and_then(|last| last.offset_to(position))
        {
            let axis = usize::from(delta[1].abs() > delta[0].abs());
            let travel = delta[axis];
            if !speed.is_finite() || speed < 0.8 || travel.abs() >= 1.0 {
                self.direction = None;
            } else if travel.abs() >= 0.0005 {
                self.direction = Some(match (axis, travel > 0.0) {
                    (0, true) => Direction::East,
                    (0, false) => Direction::West,
                    (_, true) => Direction::South,
                    (_, false) => Direction::North,
                });
            }
        } else {
            self.direction = None;
        }
        self.direction
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use maps::{Key, Zoom};

    fn point(x: u32, y: u32, east: f64, south: f64) -> Position {
        Position {
            key: Key::new(Zoom::new(15).unwrap(), x, y).unwrap(),
            east,
            south,
        }
    }

    #[test]
    fn travel_hint_preserves_thresholds_stationary_hysteresis_and_loss_reset() {
        let mut hint = DirectionHint::default();
        let start = point(10, 20, 0.75, 0.75);
        assert_eq!(hint.observe(Some((start, 1.0))), None);
        let south = Position {
            south: 0.8,
            ..start
        };
        assert_eq!(hint.observe(Some((south, 1.0))), Some(Direction::South));
        assert_eq!(hint.observe(Some((south, 1.0))), Some(Direction::South));
        assert_eq!(hint.observe(Some((south, 0.79))), None);
        assert_eq!(hint.observe(Some((start, 1.0))), Some(Direction::North));
        assert_eq!(hint.observe(Some((south, f32::NAN))), None);
        assert_eq!(hint.observe(Some((start, 1.0))), Some(Direction::North));
        assert_eq!(hint.observe(None), None);
        assert_eq!(hint.observe(Some((south, 1.0))), None);
    }

    #[test]
    fn travel_hint_wraps_at_antimeridian_and_resets_on_jump_or_zoom_change() {
        let mut hint = DirectionHint::default();
        let west = point(32767, 20, 0.9, 0.75);
        let east = point(0, 20, 0.1, 0.75);
        assert_eq!(hint.observe(Some((west, 5.0))), None);
        assert_eq!(hint.observe(Some((east, 5.0))), Some(Direction::East));
        assert_eq!(hint.observe(Some((point(0, 22, 0.1, 0.75), 5.0))), None);
        let zoomed = Position {
            key: Key::new(Zoom::new(16).unwrap(), 0, 44).unwrap(),
            ..east
        };
        assert_eq!(hint.observe(Some((zoomed, 5.0))), None);
    }
}
