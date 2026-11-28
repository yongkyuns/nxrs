use lock_order::{scope, Mutex, Next, Unlocked};
fn main() {
    let state = Mutex::<_, Next<Unlocked>>::new(0);
    let escaped = scope(|root| state.with_lock(root, |held, _| held));
    let _ = escaped;
}
