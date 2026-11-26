//! Fixed-reader publication of complete, copied latest values.
//!
//! Use this when readers may skip intermediate versions. Each reader has its
//! own single-producer/single-consumer triple buffer, initialized during
//! [`channel`]. [`Publisher::publish`] copies to those buffers in array order.
//! The fanout is not one transaction: readers may observe different complete
//! generations while publication is in progress. A paused reader does not stop
//! publication or other readers. An idle publisher leaves its last value
//! readable; before its first publication, that value is the supplied initial
//! value. Reads do not wait for a new publication.
//!
//! This is a latest-value contract, not ordered delivery or a freshness check.
//! There is no notification, acknowledgment, close event, fault state, retry or
//! lifecycle policy. Include source state, timestamps or sequence numbers in
//! the payload when the owning component needs them. Dropping the publisher
//! leaves each reader's last published value available. Dropping a reader does
//! not change the fixed fanout or notify the publisher.
//!
//! # Ownership
//!
//! Endpoints are not cloneable. Their operations require exclusive mutable
//! access. Both endpoints can move to another thread when the payload permits
//! it, but deliberately do not implement `Sync`: this API assigns one current
//! owner to each endpoint. This restriction is a wrapper contract, not a claim
//! that sharing the upstream endpoint is unsound. An ordinary spawned thread
//! additionally requires the payload's usual `'static` lifetime; scoped threads
//! may carry shorter borrows. Copies of a payload do not deep-copy its referents.
//!
//! ```
//! use messaging::latest::channel;
//!
//! let (mut publisher, [mut display, mut communications]) = channel::<_, 2>(0_u32);
//! assert_eq!(display.read(), 0);
//! publisher.publish(1);
//! publisher.publish(2); // Readers may skip 1.
//! assert_eq!(display.read(), 2);
//! assert_eq!(communications.read(), 2);
//! ```
//!
//! # Resource and progress boundaries
//!
//! Setup allocates one upstream shared state per reader. Each state contains
//! three payload slots plus atomic, reference-count and cache-padding overhead;
//! budget the actual target layout, not just `3 * READERS * size_of::<T>()`.
//! Endpoint arrays also occupy caller-owned storage. `publish` copies one `T`
//! per reader; `read` copies one `T`. Neither operation allocates, releases a
//! shared allocation, calls user `Clone`/`Drop`, or adds a retry loop. `T: Copy`
//! excludes payload destructors. The final endpoint drop for each pair releases
//! that pair's allocation, so endpoint teardown belongs outside steady-state
//! publication. Setup follows the allocator's normal failure behavior.
//!
//! The pinned `triple_buffer` backend owns the synchronization algorithm. This
//! wrapper adds no unsafe code, custom atomic protocol, mutex, thread or wait
//! notification. Target atomic support, compiler lowering, cache contention,
//! allocation/teardown costs and linked footprint require qualification. These
//! API contracts are not a global wait-free, ISR-safe or deadline guarantee.

use core::{cell::Cell, marker::PhantomData};
use triple_buffer::{Input, Output};

// The backend's constructor needs Clone. Copy the exact value even if a payload
// has a hand-written Clone implementation with side effects or different data.
#[derive(Copy)]
struct CopyValue<T: Copy>(T);

impl<T: Copy> Clone for CopyValue<T> {
    fn clone(&self) -> Self {
        *self
    }
}

/// Allocate a fixed set of independent readers with the same initial value.
///
/// Create every endpoint before starting publication. Readers cannot be added
/// later or cloned. Unread publications coalesce independently for each reader.
///
/// # Panics
///
/// Panics if `READERS` is zero: product wiring must name at least one consumer.
///
/// Payloads must be copied rather than moved or dropped during replacement:
///
/// ```compile_fail
/// messaging::latest::channel::<String, 1>(String::new());
/// ```
///
/// Payloads must be safe to move between execution owners:
///
/// ```compile_fail
/// messaging::latest::channel::<*const u8, 1>(core::ptr::null());
/// ```
pub fn channel<T: Copy + Send, const READERS: usize>(
    initial: T,
) -> (Publisher<T, READERS>, [Reader<T>; READERS]) {
    assert!(READERS > 0, "latest channel requires at least one reader");
    let mut inputs = core::array::from_fn(|_| None);
    let readers = core::array::from_fn(|index| {
        let (input, output) = triple_buffer::triple_buffer(&CopyValue(initial));
        inputs[index] = Some(input);
        Reader {
            output,
            _owner: PhantomData,
        }
    });
    let inputs = inputs.map(|input| input.expect("reader slot initialized"));
    (
        Publisher {
            inputs,
            _owner: PhantomData,
        },
        readers,
    )
}

/// The one publisher for a fixed reader set.
///
/// This endpoint is intentionally neither `Clone` nor `Sync`.
///
/// ```compile_fail
/// let (publisher, _) = messaging::latest::channel::<_, 1>(0_u32);
/// let another_publisher = publisher.clone();
/// ```
///
/// ```compile_fail
/// fn require_sync<T: Sync>() {}
/// require_sync::<messaging::latest::Publisher<u32, 1>>();
/// ```
pub struct Publisher<T: Copy + Send, const READERS: usize> {
    inputs: [Input<CopyValue<T>>; READERS],
    _owner: PhantomData<Cell<()>>,
}

impl<T: Copy + Send, const READERS: usize> Publisher<T, READERS> {
    /// Publish one complete value to each reader, without waiting for readers.
    ///
    /// Concurrent reads receive complete values, with different readers allowed
    /// to see different generations. This is not an atomic fanout.
    /// No delivery acknowledgment or reader-disconnection result is returned.
    pub fn publish(&mut self, value: T) {
        for input in &mut self.inputs {
            input.write(CopyValue(value));
        }
    }
}

/// One independent reader, moved to its execution owner during construction.
///
/// This endpoint is intentionally neither `Clone` nor `Sync`.
///
/// ```compile_fail
/// let (_, [reader]) = messaging::latest::channel::<_, 1>(0_u32);
/// let another_reader = reader.clone();
/// ```
///
/// ```compile_fail
/// fn require_sync<T: Sync>() {}
/// require_sync::<messaging::latest::Reader<u32>>();
/// ```
pub struct Reader<T: Copy + Send> {
    output: Output<CopyValue<T>>,
    _owner: PhantomData<Cell<()>>,
}

impl<T: Copy + Send> Reader<T> {
    /// Copy a complete published value, or the initial value before publication.
    ///
    /// A concurrent publication may become visible on this read or a later one.
    /// Multiple unread values may coalesce. The returned copy does not retain
    /// a backend buffer borrow and remains usable across later publications.
    ///
    /// Reading requires the reader's mutable ownership:
    ///
    /// ```compile_fail
    /// let (_, [reader]) = messaging::latest::channel::<_, 1>(0_u32);
    /// reader.read();
    /// ```
    pub fn read(&mut self) -> T {
        self.output.read().0
    }
}
