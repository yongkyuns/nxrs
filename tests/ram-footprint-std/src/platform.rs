//! Only observation is native. All lifecycle, threads and delivery use Rust.

#[derive(Clone, Copy, Debug, Default)]
#[repr(C)]
pub struct NativeHeap {
    pub used: usize,
    pub free: usize,
    pub arena: usize,
}

#[derive(Clone, Copy, Debug, Default)]
#[repr(C)]
pub struct Stack {
    /// NuttX adj_stack_size, not a guessed malloc allocation size.
    pub usable: usize,
    /// Observed coloration high water, including this diagnostic call.
    pub observed: usize,
}

#[cfg(target_os = "nuttx")]
mod selected {
    use super::{NativeHeap, Stack};
    unsafe extern "C" {
        fn ram_probe_heap(out: *mut NativeHeap);
        fn ram_probe_stack(out: *mut Stack);
    }
    pub const DOMAIN: &str = "nuttx-target";
    pub const NATIVE: bool = true;
    pub fn heap() -> NativeHeap {
        let mut out = NativeHeap::default();
        // SAFETY: the helper writes exactly the matching target-sized C struct.
        unsafe { ram_probe_heap(&mut out) };
        out
    }
    pub fn stack() -> Stack {
        let mut out = Stack::default();
        // SAFETY: this observes only the calling live thread; no TCB is retained.
        unsafe { ram_probe_stack(&mut out) };
        out
    }
}

#[cfg(not(target_os = "nuttx"))]
mod selected {
    use super::{NativeHeap, Stack};
    pub const DOMAIN: &str = "host-functional";
    pub const NATIVE: bool = false;
    pub fn heap() -> NativeHeap {
        NativeHeap::default()
    }
    pub fn stack() -> Stack {
        Stack::default()
    }
}

pub use selected::{heap, stack, DOMAIN, NATIVE};
