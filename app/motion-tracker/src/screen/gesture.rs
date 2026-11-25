use touch::{Event, Point};

const TAP_SLOP: u16 = 14;
const SWIPE_DISTANCE: i32 = 32;

pub(super) enum Gesture {
    Tap(Point),
    Down,
    Up,
}

pub(super) struct Contact {
    start: Point,
    moved: bool,
    swiped: bool,
}

pub(super) fn recognize(contact: &mut Option<Contact>, event: Event) -> Option<Gesture> {
    match event {
        Event::Pressed(start) => {
            *contact = Some(Contact {
                start,
                moved: false,
                swiped: false,
            });
            None
        }
        Event::Moved(point) => {
            let contact = contact.as_mut()?;
            let dx = point.x.abs_diff(contact.start.x);
            let dy = i32::from(point.y) - i32::from(contact.start.y);
            contact.moved |= dx > TAP_SLOP || dy.unsigned_abs() > u32::from(TAP_SLOP);
            if contact.swiped || dy.abs() < SWIPE_DISTANCE || dy.abs() <= i32::from(dx) {
                return None;
            }
            contact.swiped = true;
            Some(if dy > 0 { Gesture::Down } else { Gesture::Up })
        }
        Event::Released => contact
            .take()
            .filter(|contact| !contact.moved)
            .map(|contact| Gesture::Tap(contact.start)),
    }
}
