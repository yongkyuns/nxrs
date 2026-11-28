use lock_order::{Mutex, Next, Unlocked};
use std::rc::Rc;
fn send<T: Send>() {}
fn main() {
    send::<Mutex<Rc<u8>, Next<Unlocked>>>();
}
