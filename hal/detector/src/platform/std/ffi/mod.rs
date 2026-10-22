use std::ptr::NonNull;
pub const INPUT_BYTES: usize = 224 * 224 * 3;
pub const OUTPUT_BYTES: usize = (28 * 28 + 14 * 14 + 7 * 7) * 33;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Model,
}
unsafe extern "C" {
    fn nxrs_pico_create() -> *mut std::ffi::c_void;
    fn nxrs_pico_destroy(engine: *mut std::ffi::c_void);
    fn nxrs_pico_run(engine: *mut std::ffi::c_void, input: *const i8, output: *mut i8);
}
/// One synchronous SDK graph. It has no callbacks, background tasks, or shared
/// mutable state. An exclusive Rust owner supplies all calls and joins its
/// worker before dropping the graph.
pub struct Engine {
    pointer: NonNull<std::ffi::c_void>,
}
// SAFETY: the synchronous graph has exclusively owned tensors/ops; no SDK task,
// callback or thread-local lifetime is retained. All access requires &mut self.
unsafe impl Send for Engine {}
impl Engine {
    pub fn new() -> Result<Self, Error> {
        // SAFETY: the SDK constructor needs no borrowed state.
        NonNull::new(unsafe { nxrs_pico_create() })
            .map(|pointer| Self { pointer })
            .ok_or(Error::Model)
    }
    pub fn run(&mut self, input: &[i8; INPUT_BYTES], output: &mut [i8; OUTPUT_BYTES]) {
        // SAFETY: exact buffer sizes, no aliasing, and exclusive graph ownership.
        // The native call is finite and synchronous and retains neither buffer.
        unsafe { nxrs_pico_run(self.pointer.as_ptr(), input.as_ptr(), output.as_mut_ptr()) }
    }
}
impl Drop for Engine {
    fn drop(&mut self) {
        // SAFETY: exactly one matching destructor; &mut ownership rules exclude
        // concurrent run, and the SDK never creates an asynchronous callback.
        unsafe { nxrs_pico_destroy(self.pointer.as_ptr()) }
    }
}
