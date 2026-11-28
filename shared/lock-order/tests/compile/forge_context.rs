use lock_order::{LockContext, Unlocked};
fn main() {
    let _ = LockContext::<Unlocked>::new();
}
