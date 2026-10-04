//! Qualification-only NuttX mqueue backend for the existing `cq-scale` workload.
//!
//! This intentionally uses the same `mq_send`, `mq_receive`, and `poll` kernel
//! path as the C control. It is not a general Rust channel: queue closure and
//! cancellation are not represented, and only `Copy` wire messages are valid.

use std::ffi::{c_char, c_int, c_long};
use std::marker::PhantomData;
use std::mem::{size_of, MaybeUninit};
use std::ptr;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;

const O_RDWR: c_int = 3;
const O_CREAT: c_int = 4;
const O_EXCL: c_int = 8;
static NEXT_NAME: AtomicU32 = AtomicU32::new(0);
const POLLSET_WORDS: usize = 96;

#[repr(C)]
struct MqAttr {
    maxmsg: c_long,
    msgsize: c_long,
    flags: c_long,
    curmsgs: c_long,
}

unsafe extern "C" {
    fn mq_open(name: *const c_char, flags: c_int, ...) -> c_int;
    fn mq_close(fd: c_int) -> c_int;
    fn mq_unlink(name: *const c_char) -> c_int;
    fn mq_send(fd: c_int, message: *const c_char, len: usize, priority: u32) -> c_int;
    fn mq_receive(fd: c_int, message: *mut c_char, len: usize, priority: *mut u32) -> isize;
    fn nxrs_cq_pollset_init(storage: *mut u32, bytes: usize) -> c_int;
    fn nxrs_cq_pollset_add(storage: *mut u32, fd: c_int) -> c_int;
    fn nxrs_cq_pollset_remove(storage: *mut u32, index: u32);
    fn nxrs_cq_pollset_select(storage: *mut u32) -> c_int;
    fn nxrs_cq_pollset_fd(storage: *const u32, index: u32) -> c_int;
    fn nxrs_cq_cycles() -> u32;
}

struct Queue {
    fd: c_int,
    name: [u8; 13],
}

impl Drop for Queue {
    fn drop(&mut self) {
        // SAFETY: this is the last Arc owner; the descriptor and NUL-terminated
        // name were returned by a successful mq_open in bounded().
        unsafe {
            mq_close(self.fd);
            mq_unlink(self.name.as_ptr().cast());
        }
    }
}

pub struct Sender<T> {
    queue: Arc<Queue>,
    message: PhantomData<T>,
}

impl<T> Clone for Sender<T> {
    fn clone(&self) -> Self {
        Self {
            queue: Arc::clone(&self.queue),
            message: PhantomData,
        }
    }
}

pub struct Receiver<T> {
    queue: Arc<Queue>,
    message: PhantomData<T>,
}

fn queue_name(id: u32) -> [u8; 13] {
    let mut name = *b"/cqr00000000\0";
    for index in 0..8 {
        let shift = (7 - index) * 4;
        let digit = ((id >> shift) & 15) as u8;
        name[4 + index] = if digit < 10 {
            b'0' + digit
        } else {
            b'a' + digit - 10
        };
    }
    name[12] = 0;
    name
}

// Size ablation: keep a typed facade but share byte-level queue operations.
// Default builds inline the same logic; the opt-in build outlines the core.
#[cfg_attr(feature = "shared-mq-code", inline(never))]
#[cfg_attr(not(feature = "shared-mq-code"), inline(always))]
fn open_queue(capacity: usize, message_size: usize) -> Arc<Queue> {
    // The compact-topology control shares one queue across 11 logical lanes.
    assert!((1..=11).contains(&capacity));
    let attr = MqAttr {
        maxmsg: capacity as c_long,
        msgsize: message_size as c_long,
        flags: 0,
        curmsgs: 0,
    };
    let mut opened = None;
    for _ in 0..16 {
        let id = NEXT_NAME.fetch_add(1, Ordering::Relaxed);
        // SAFETY: the target clock helper reads a single-core CCOUNT register.
        let name = queue_name(unsafe { nxrs_cq_cycles() } ^ id);
        // SAFETY: NuttX mq_open expects a NUL-terminated name, mode and attr.
        let fd = unsafe {
            mq_open(
                name.as_ptr().cast(),
                O_RDWR | O_CREAT | O_EXCL,
                0o600,
                &attr,
            )
        };
        if fd >= 0 {
            opened = Some(Queue { fd, name });
            break;
        }
    }
    Arc::new(opened.expect("mq_open failed"))
}

pub fn bounded<T: Copy + Send + 'static>(capacity: usize) -> (Sender<T>, Receiver<T>) {
    let queue = open_queue(capacity, size_of::<T>());
    (
        Sender {
            queue: Arc::clone(&queue),
            message: PhantomData,
        },
        Receiver {
            queue,
            message: PhantomData,
        },
    )
}

#[cfg_attr(feature = "shared-mq-code", inline(never))]
#[cfg_attr(not(feature = "shared-mq-code"), inline(always))]
unsafe fn send_wire(fd: c_int, message: *const c_char, bytes: usize) -> Result<(), ()> {
    // SAFETY: callers keep a readable wire value alive for all bytes until
    // mq_send has copied it. The queue's message size agrees with this length.
    if unsafe { mq_send(fd, message, bytes, 0) } == 0 {
        Ok(())
    } else {
        Err(())
    }
}

#[cfg_attr(feature = "shared-mq-code", inline(never))]
#[cfg_attr(not(feature = "shared-mq-code"), inline(always))]
unsafe fn receive_wire(fd: c_int, storage: *mut c_char, bytes: usize) -> Result<(), ()> {
    // SAFETY: callers provide writable storage for bytes. They initialize a
    // Rust value only after this exact-length success, never a short message.
    if unsafe { mq_receive(fd, storage, bytes, ptr::null_mut()) } == bytes as isize {
        Ok(())
    } else {
        Err(())
    }
}

impl<T: Copy> Sender<T> {
    pub fn send(&self, message: T) -> Result<(), ()> {
        // SAFETY: T is a Copy wire struct in this fixture and mq_send copies
        // exactly size_of::<T>() bytes before returning.
        unsafe { send_wire(self.queue.fd, (&raw const message).cast(), size_of::<T>()) }
    }

    /// Diagnostic path: pass the caller's wire buffer directly to NuttX.
    #[cfg(feature = "borrowed-mq-io")]
    pub fn send_ref(&self, message: &T) -> Result<(), ()> {
        // SAFETY: mq_send copies the wire bytes before returning; the caller
        // retains the message for the duration of the call.
        unsafe { send_wire(self.queue.fd, (message as *const T).cast(), size_of::<T>()) }
    }
}

impl<T: Copy> Receiver<T> {
    pub fn recv(&self) -> Result<T, ()> {
        let mut message = MaybeUninit::<T>::uninit();
        // SAFETY: the NuttX queue was created for precisely size_of::<T>()
        // messages. A value is read only when receive returns the full length.
        unsafe { receive_wire(self.queue.fd, message.as_mut_ptr().cast(), size_of::<T>()) }?;
        // SAFETY: an exact-sized successful receive initialized the wire value.
        Ok(unsafe { message.assume_init() })
    }

    /// # Safety
    /// The queue must contain only byte patterns valid for `T`. Its message
    /// size must be exactly `size_of::<T>()`.
    #[cfg(feature = "borrowed-mq-io")]
    unsafe fn recv_into<'a>(&self, storage: &'a mut MaybeUninit<T>) -> Result<&'a T, ()> {
        // SAFETY: storage has room for T and mq_receive writes a full message
        // before the reference is constructed below.
        unsafe { receive_wire(self.queue.fd, storage.as_mut_ptr().cast(), size_of::<T>()) }?;
        // SAFETY: exact receive initialized all bytes; caller guarantees T validity.
        Ok(unsafe { storage.assume_init_ref() })
    }
}

#[repr(C, align(4))]
struct PollStorage([u32; POLLSET_WORDS]);

pub struct Select {
    storage: PollStorage,
}

pub struct SelectedOperation {
    index: usize,
    fd: c_int,
}

impl Select {
    pub fn new() -> Self {
        let mut selection = Self {
            storage: PollStorage([0; POLLSET_WORDS]),
        };
        // SAFETY: C validates the byte capacity and owns the opaque layout.
        let result = unsafe {
            nxrs_cq_pollset_init(selection.storage.0.as_mut_ptr(), size_of::<PollStorage>())
        };
        assert_eq!(result, 0, "poll set initialization failed");
        selection
    }

    pub fn recv<T>(&mut self, receiver: &Receiver<T>) {
        // SAFETY: storage was initialized by the C helper and the descriptor
        // remains owned by the receiver for this benchmark's worker lifetime.
        let index = unsafe { nxrs_cq_pollset_add(self.storage.0.as_mut_ptr(), receiver.queue.fd) };
        assert!(index >= 0, "poll set is full");
    }

    pub fn remove(&mut self, index: usize) {
        // SAFETY: the index came from this poll set's select result.
        unsafe { nxrs_cq_pollset_remove(self.storage.0.as_mut_ptr(), index as u32) };
    }

    pub fn select(&mut self) -> SelectedOperation {
        // SAFETY: storage is an initialized opaque C poll set.
        let ready = unsafe { nxrs_cq_pollset_select(self.storage.0.as_mut_ptr()) };
        assert!(ready >= 0, "poll failed");
        let index = ready as usize;
        // SAFETY: select returned a live index in this poll set.
        let fd = unsafe { nxrs_cq_pollset_fd(self.storage.0.as_ptr(), index as u32) };
        SelectedOperation { index, fd }
    }
}

impl SelectedOperation {
    pub fn index(&self) -> usize {
        self.index
    }

    pub fn recv<T: Copy>(&self, receiver: &Receiver<T>) -> Result<T, ()> {
        if self.fd != receiver.queue.fd {
            return Err(());
        }
        receiver.recv()
    }

    /// # Safety
    /// The selected queue must contain only byte patterns valid for `T`.
    #[cfg(feature = "borrowed-mq-io")]
    pub unsafe fn recv_into<'a, T: Copy>(
        &self,
        receiver: &Receiver<T>,
        storage: &'a mut MaybeUninit<T>,
    ) -> Result<&'a T, ()> {
        if self.fd != receiver.queue.fd {
            return Err(());
        }
        // SAFETY: the caller upholds the wire-value validity requirement.
        unsafe { receiver.recv_into(storage) }
    }
}
