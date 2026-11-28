use lock_order::{LockContext, Unlocked};
fn send<T: Send>() {}
fn main() {
    send::<LockContext<Unlocked>>();
}
