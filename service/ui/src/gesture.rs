use touch::{Event, Point};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Gesture {
    Tap(Point),
    Down,
    Up,
}

#[derive(Clone, Copy)]
struct Contact {
    start: Point,
    moved: bool,
    swiped: bool,
}

/// Single-contact tap/vertical-swipe recognition, driven by the caller's inbox.
pub struct Recognizer {
    contact: Option<Contact>,
    tap_slop: u16,
    swipe_distance: u16,
}

impl Recognizer {
    pub const fn new(tap_slop: u16, swipe_distance: u16) -> Self {
        Self {
            contact: None,
            tap_slop,
            swipe_distance,
        }
    }

    pub fn reset(&mut self) {
        self.contact = None;
    }

    /// `missed` breaks the old contact before observing this event. A release
    /// after a lost press/move therefore cannot activate a control.
    pub fn observe(&mut self, event: Event, missed: bool) -> Option<Gesture> {
        if missed {
            self.reset();
        }
        match event {
            Event::Pressed(start) => {
                self.contact = Some(Contact {
                    start,
                    moved: false,
                    swiped: false,
                });
                None
            }
            Event::Moved(point) => {
                let contact = self.contact.as_mut()?;
                let dx = point.x.abs_diff(contact.start.x);
                let dy = i32::from(point.y) - i32::from(contact.start.y);
                contact.moved |= dx > self.tap_slop || dy.unsigned_abs() > u32::from(self.tap_slop);
                if contact.swiped
                    || dy.unsigned_abs() < u32::from(self.swipe_distance)
                    || dy.unsigned_abs() <= u32::from(dx)
                {
                    return None;
                }
                contact.swiped = true;
                Some(if dy > 0 { Gesture::Down } else { Gesture::Up })
            }
            Event::Released => self
                .contact
                .take()
                .filter(|contact| !contact.moved && !contact.swiped)
                .map(|contact| Gesture::Tap(contact.start)),
        }
    }
}
