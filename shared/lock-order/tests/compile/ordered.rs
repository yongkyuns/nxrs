use lock_order::{scope, Before, LockContext, Mutex, Next, Poisoned, Unlocked};
use std::cell::Cell;

type State = Next<Unlocked>;
type Buffer = Next<State>;
type Statistics = Next<Buffer>;
static STATE: Mutex<u32, State> = Mutex::new(0);
static STATISTICS: Mutex<u32, Statistics> = Mutex::new(0);

fn count<L: Before<Statistics>>(held: &mut LockContext<L>) -> Result<(), Poisoned> {
    STATISTICS.with_lock(held, |_, value| *value += 1)
}

fn send_sync<T: Send + Sync>() {}

fn main() {
    // Mutex<T> needs T: Send, not T: Sync, to be shared safely.
    send_sync::<Mutex<Cell<u32>, State>>();
    scope(|root| {
        STATE
            .with_lock(root, |held, value| {
                *value += 1;
                count(held).unwrap();
            })
            .unwrap();
        count(root).unwrap();
    })
    .unwrap();
}
