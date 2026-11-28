use lock_order::{Mutex, Next, Unlocked};
fn main() {
    let state = Mutex::<_, Next<Unlocked>>::new(0);
    let _guard = state.inner.lock().unwrap();
}
