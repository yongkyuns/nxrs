use lock_order::{LockContext, Unlocked};
fn sync<T: Sync>() {}
fn main() {
    sync::<LockContext<Unlocked>>();
}
