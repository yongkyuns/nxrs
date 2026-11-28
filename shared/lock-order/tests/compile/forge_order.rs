use lock_order::{Before, Next, Unlocked};
type State = Next<Unlocked>;
impl Before<State> for State {}
fn main() {}
