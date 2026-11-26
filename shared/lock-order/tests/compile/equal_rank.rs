use lock_order::{scope, Mutex, Next, Unlocked};
type State = Next<Unlocked>;
fn main() {
    let first = Mutex::<_, State>::new(0);
    let second = Mutex::<_, State>::new(0);
    scope(|root| first.with_lock(root, |held, _| second.with_lock(held, |_, _| ())));
}
