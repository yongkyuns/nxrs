use lock_order::{scope, Mutex, Next, Unlocked};
type State = Next<Unlocked>;
fn main() {
    let state = Mutex::<_, State>::new(0);
    scope(|root| state.with_lock(root, |held, _| state.with_lock(held, |_, _| ())));
}
