//! Own the two measured buffer-passing protocols at one boundary.
use crate::channel::{Receiver, SelectedOperation, Sender};
use std::mem::MaybeUninit;
pub struct Owned;
pub struct Borrowed;
impl Owned {
    pub fn send<T: Copy>(sender: &Sender<T>, value: &T) -> Result<(), ()> {
        sender.send(*value)
    }
    /// # Safety
    /// The selected queue contains only valid initialized values of `T`.
    pub unsafe fn receive<'a, T: Copy>(
        selected: &SelectedOperation,
        receiver: &Receiver<T>,
        storage: &'a mut MaybeUninit<T>,
    ) -> Result<&'a T, ()> {
        Ok(storage.write(selected.recv(receiver)?))
    }
}
impl Borrowed {
    pub fn send<T: Copy>(sender: &Sender<T>, value: &T) -> Result<(), ()> {
        sender.send_ref(value)
    }
    /// # Safety
    /// The selected queue contains only valid initialized values of `T`.
    pub unsafe fn receive<'a, T: Copy>(
        selected: &SelectedOperation,
        receiver: &Receiver<T>,
        storage: &'a mut MaybeUninit<T>,
    ) -> Result<&'a T, ()> {
        // SAFETY: forwarded from the selected queue's wire contract.
        unsafe { selected.recv_into(receiver, storage) }
    }
}
