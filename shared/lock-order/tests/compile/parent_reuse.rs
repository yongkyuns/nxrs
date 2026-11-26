use lock_order::{scope, Mutex, Next, Unlocked};
type Low = Next<Unlocked>;
type High = Next<Low>;
fn main() {
    let low = Mutex::<_, Low>::new(0);
    let high = Mutex::<_, High>::new(0);
    scope(|root| high.with_lock(root, |_, _| low.with_lock(root, |_, _| ())));
}
