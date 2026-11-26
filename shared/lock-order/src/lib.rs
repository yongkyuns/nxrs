//! Optional, strictly ordered acquisition of ordinary [`std::sync::Mutex`] locks.
//!
//! Declare ranks using [`Next`], then pass the current [`LockContext`] through
//! every helper that acquires an ordered mutex. Acquiring an equal or lower rank
//! does not compile. A rank names a place in one total order: two mutexes with
//! the same rank cannot be held together, even when they are different values.
//!
//! ```
//! use lock_order::{scope, Mutex, Next, Unlocked};
//!
//! type State = Next<Unlocked>;
//! type Statistics = Next<State>;
//!
//! let state = Mutex::<_, State>::new(Vec::<u32>::new());
//! let statistics = Mutex::<_, Statistics>::new(0_u32);
//! scope(|root| {
//!     state.with_lock(root, |held, samples| {
//!         samples.push(42);
//!         statistics.with_lock(held, |_, count| *count += 1)
//!     })
//! })
//! .expect("one root scope per thread")
//! .expect("state is not poisoned")
//! .expect("statistics are not poisoned");
//! ```
//!
//! The native guard stays inside [`Mutex::with_lock`]. Only borrowed data and
//! a borrowed next-level context reach its closure; neither can escape. This
//! prevents an owned guard from being forgotten while its ordering permission
//! is reused. Contexts cannot be constructed, cloned, or sent to another thread.
//!
//! Rank and borrow checks happen at compile time. Root uniqueness is a separate
//! runtime check: [`scope`] returns [`ScopeError::AlreadyActive`] if a callback
//! or nested helper attempts to start another scope on the same thread. Start
//! a root at an ordinary thread entry, and pass its context through the call tree.
//!
//! This contract covers acquisitions through this API. It does not prove
//! freedom from channel/join cycles, waits inside a critical section, unmanaged
//! locks, native callbacks, starvation, or deadline misses. It does not select a
//! mutex priority protocol: the target's ordinary std backend and configuration
//! still determine that behavior. No worker, queue, executor, or third-party
//! dependency is added. Thread-local and mutex initialization costs remain
//! subject to target qualification.
#![forbid(unsafe_code)]

use std::{cell::Cell, fmt, marker::PhantomData, rc::Rc, sync};

mod sealed {
    pub trait Level {}
    pub trait Before<Other> {}
}

/// A level in the sealed lock order. Use [`Unlocked`] and [`Next`] to name levels.
/// Downstream crates cannot implement this trait for additional types.
pub trait Level: sealed::Level {}

/// The root level, before every mutex rank. It cannot be used as a mutex rank.
pub struct Unlocked {
    _private: (),
}

/// The next rank after `Previous`. Type aliases give ranks application names.
///
/// `Next<Unlocked>` precedes `Next<Next<Unlocked>>`, and so on. The ordering
/// implementations are sealed, so a caller cannot introduce a reverse edge,
/// equal-rank edge, or cycle.
pub struct Next<Previous: Level>(PhantomData<Previous>);

impl sealed::Level for Unlocked {}
impl Level for Unlocked {}
impl<Previous: Level> sealed::Level for Next<Previous> {}
impl<Previous: Level> Level for Next<Previous> {}

/// A positive lock rank. Only [`Next`] levels implement this sealed trait.
pub trait Rank: Level {}
impl<Previous: Level> Rank for Next<Previous> {}

impl<Other: Level> sealed::Before<Next<Other>> for Unlocked {}
impl<Previous: Level, Other: Level> sealed::Before<Next<Other>> for Next<Previous> where
    Previous: sealed::Before<Other>
{
}

/// A strict, transitive acquisition order, usable in a helper's bounds.
///
/// ```
/// use lock_order::{Before, LockContext, Mutex, Next, Poisoned, Unlocked};
/// type State = Next<Unlocked>;
/// type Statistics = Next<State>;
///
/// fn count<L: Before<Statistics>>(
///     current: &mut LockContext<L>,
///     statistics: &Mutex<u32, Statistics>,
/// ) -> Result<(), Poisoned> {
///     statistics.with_lock(current, |_, value| *value += 1)
/// }
/// ```
pub trait Before<Other: Level>: Level + sealed::Before<Other> {}
impl<Current: Level, Other: Level> Before<Other> for Current where Current: sealed::Before<Other> {}

/// Borrowed permission to acquire a strictly higher-ranked mutex.
///
/// Obtain the root through [`scope`] and higher levels through [`Mutex::with_lock`].
/// This type is neither `Send` nor `Sync`; an ordinary new thread starts its own
/// root scope. The `Rc` marker is zero-sized and does not allocate an `Rc`.
pub struct LockContext<Current: Level> {
    _level: PhantomData<Current>,
    _thread: PhantomData<Rc<()>>,
}

impl<Current: Level> LockContext<Current> {
    fn new() -> Self {
        Self {
            _level: PhantomData,
            _thread: PhantomData,
        }
    }
}

thread_local! {
    static ACTIVE: Cell<bool> = const { Cell::new(false) };
}

/// A root scope could not be entered. No user closure or mutex acquisition ran.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScopeError {
    /// A scope is already active on this thread; pass its current context instead.
    AlreadyActive,
    /// Thread-local storage is unavailable, for example during thread teardown.
    Unavailable,
}

impl fmt::Display for ScopeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::AlreadyActive => {
                "lock-order scope is already active on this thread; pass its current context"
            }
            Self::Unavailable => "lock-order thread-local state is unavailable",
        })
    }
}

impl std::error::Error for ScopeError {}

/// Enter the one ordered-lock root scope on this thread.
///
/// Nested calls fail immediately, including calls from synchronous callbacks.
/// The active flag resets on normal return and on unwinding. The higher-ranked
/// closure prevents the root context from escaping in the result.
pub fn scope<R>(
    operation: impl for<'root> FnOnce(&'root mut LockContext<Unlocked>) -> R,
) -> Result<R, ScopeError> {
    ACTIVE
        .try_with(|active| {
            if active.replace(true) {
                return Err(ScopeError::AlreadyActive);
            }
            struct Reset<'a>(&'a Cell<bool>);
            impl Drop for Reset<'_> {
                fn drop(&mut self) {
                    self.0.set(false);
                }
            }
            let _reset = Reset(active);
            Ok(operation(&mut LockContext::new()))
        })
        .map_err(|_| ScopeError::Unavailable)?
}

/// The std mutex was poisoned. Its data closure did not run.
///
/// Poisoning remains set; this API does not silently recover possibly damaged
/// state. An owner with exclusive access can inspect or recover the value through
/// [`Mutex::get_mut`] or [`Mutex::into_inner`], with std's normal poison result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Poisoned;

impl fmt::Display for Poisoned {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ordered mutex is poisoned")
    }
}

impl std::error::Error for Poisoned {}

/// A std mutex with a fixed acquisition rank and no unranked lock accessor.
///
/// `Send` and `Sync` follow the contained std mutex and its data. The sealed
/// zero-sized rank adds no ownership of another resource.
pub struct Mutex<T, Order: Rank> {
    inner: sync::Mutex<T>,
    _rank: PhantomData<Order>,
}

impl<T, Order: Rank> Mutex<T, Order> {
    /// Create a mutex. This is const so an ordered mutex may be a static.
    pub const fn new(value: T) -> Self {
        Self {
            inner: sync::Mutex::new(value),
            _rank: PhantomData,
        }
    }

    /// Lock at a strictly higher rank for one synchronous closure.
    ///
    /// The parent context remains exclusively borrowed for this entire call.
    /// The native guard stays private and drops before this call returns. Neither
    /// the borrowed data nor the next context may escape in `R`.
    ///
    /// This uses std's blocking `lock`; it makes no bounded-wait or fairness claim.
    /// On poisoning, the temporary native guard is released and the closure is
    /// not called. A panic in the closure has std's ordinary poisoning behavior.
    pub fn with_lock<Current: Before<Order>, R>(
        &self,
        _current: &mut LockContext<Current>,
        operation: impl for<'held> FnOnce(&'held mut LockContext<Order>, &'held mut T) -> R,
    ) -> Result<R, Poisoned> {
        let mut guard = self.inner.lock().map_err(|_| Poisoned)?;
        Ok(operation(&mut LockContext::new(), &mut *guard))
    }

    /// Inspect std's advisory poison state without acquiring the mutex.
    pub fn is_poisoned(&self) -> bool {
        self.inner.is_poisoned()
    }

    /// Borrow the data through exclusive ownership; no mutex is acquired.
    pub fn get_mut(&mut self) -> sync::LockResult<&mut T> {
        self.inner.get_mut()
    }

    /// Consume an exclusively owned mutex, preserving std's poison result.
    pub fn into_inner(self) -> sync::LockResult<T> {
        self.inner.into_inner()
    }
}
