use lock_order::{scope, Before, LockContext, Mutex, Next, Poisoned, ScopeError, Unlocked};
use std::{
    cell::Cell,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{Arc, Barrier},
    thread,
};

type State = Next<Unlocked>;
type Buffer = Next<State>;
type Statistics = Next<Buffer>;

fn count<Current: Before<Statistics>>(
    current: &mut LockContext<Current>,
    statistics: &Mutex<u32, Statistics>,
) -> Result<(), Poisoned> {
    statistics.with_lock(current, |_, value| *value += 1)
}

#[test]
fn nested_and_chained_helpers_preserve_data_and_release_each_level() {
    let state = Mutex::<_, State>::new(0);
    let buffer = Mutex::<_, Buffer>::new(0);
    let statistics = Mutex::<_, Statistics>::new(0);
    scope(|root| {
        state
            .with_lock(root, |held, state_value| {
                buffer
                    .with_lock(held, |held, buffer_value| {
                        *state_value = 7;
                        *buffer_value = 11;
                        count(held, &statistics).unwrap();
                    })
                    .unwrap();
                // The buffer has been released; the state context is usable again.
                count(held, &statistics).unwrap();
            })
            .unwrap();
        // The root may enter the same rank again after its closure completes.
        state.with_lock(root, |_, value| *value += 1).unwrap();
        count(root, &statistics).unwrap();
    })
    .unwrap();
    assert_eq!(state.into_inner().unwrap(), 8);
    assert_eq!(buffer.into_inner().unwrap(), 11);
    assert_eq!(statistics.into_inner().unwrap(), 3);
}

#[test]
fn nested_roots_are_rejected_before_their_closure_runs() {
    let calls = Cell::new(0);
    scope(|_| {
        assert_eq!(
            scope(|_| calls.set(calls.get() + 1)),
            Err(ScopeError::AlreadyActive)
        );
        // Rejection must not clear the outer scope's active flag.
        assert_eq!(
            scope(|_| calls.set(calls.get() + 1)),
            Err(ScopeError::AlreadyActive)
        );
    })
    .unwrap();
    assert_eq!(calls.get(), 0);
    scope(|_| calls.set(1)).unwrap();
    assert_eq!(calls.get(), 1);
}

#[test]
fn a_callback_cannot_mint_a_root_to_reacquire_the_locked_mutex() {
    let state = Mutex::<_, State>::new(3);
    let callback = || scope(|root| state.with_lock(root, |_, value| *value));
    let rejected = scope(|root| state.with_lock(root, |_, _| callback()))
        .unwrap()
        .unwrap();
    assert_eq!(rejected, Err(ScopeError::AlreadyActive));
    assert_eq!(callback().unwrap().unwrap(), 3);
}

#[test]
fn unwinding_unlocks_the_mutex_and_clears_the_root() {
    let damaged = Mutex::<_, State>::new(0);
    let healthy = Mutex::<_, State>::new(4);
    let result = catch_unwind(AssertUnwindSafe(|| {
        scope(|root| {
            damaged.with_lock(root, |_, value| {
                *value = 9;
                panic!("operation failed");
            })
        })
    }));
    assert!(result.is_err());
    assert!(damaged.is_poisoned());
    assert_eq!(
        scope(|root| healthy.with_lock(root, |_, value| *value))
            .unwrap()
            .unwrap(),
        4
    );
    assert_eq!(damaged.into_inner().unwrap_err().into_inner(), 9);
}

#[test]
fn poison_does_not_run_the_callback_or_retain_an_error_guard() {
    let mut state = Mutex::<_, State>::new(0);
    assert!(catch_unwind(AssertUnwindSafe(|| {
        scope(|root| {
            state.with_lock(root, |_, value| {
                *value = 5;
                panic!("poison witness");
            })
        })
    }))
    .is_err());
    let calls = Cell::new(0);
    for _ in 0..2 {
        let result =
            scope(|root| state.with_lock(root, |_, _| calls.set(calls.get() + 1))).unwrap();
        assert_eq!(result, Err(Poisoned));
    }
    assert_eq!(calls.get(), 0);
    *state.get_mut().unwrap_err().into_inner() = 12;
    assert!(state.is_poisoned());
    assert_eq!(state.into_inner().unwrap_err().into_inner(), 12);
}

#[test]
fn ordinary_threads_have_independent_roots_and_share_std_backed_data() {
    let state = Arc::new(Mutex::<_, State>::new(0_u32));
    let statistics = Arc::new(Mutex::<_, Statistics>::new(0_u32));
    let ready = Arc::new(Barrier::new(3));
    let workers: Vec<_> = (0..3)
        .map(|_| {
            let (state, statistics, ready) = (state.clone(), statistics.clone(), ready.clone());
            thread::spawn(move || {
                scope(|root| {
                    // Synchronize roots before any ordered mutex is held.
                    ready.wait();
                    for _ in 0..100 {
                        state
                            .with_lock(root, |held, value| {
                                *value += 1;
                                count(held, &statistics).unwrap();
                            })
                            .unwrap();
                    }
                })
                .unwrap();
            })
        })
        .collect();
    for worker in workers {
        worker.join().unwrap();
    }
    scope(|root| {
        assert_eq!(state.with_lock(root, |_, value| *value).unwrap(), 300);
        assert_eq!(statistics.with_lock(root, |_, value| *value).unwrap(), 300);
    })
    .unwrap();
}

#[test]
fn exclusive_access_needs_no_ordering_scope() {
    let mut state = Mutex::<_, State>::new(4_u32);
    *state.get_mut().unwrap() = 8;
    assert!(!state.is_poisoned());
    assert_eq!(state.into_inner().unwrap(), 8);
}
