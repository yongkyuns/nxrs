pub use super::native::*;
pub const NAME: &str = "zephyr";
pub fn configure() -> u32 {
    restore([0, 20]);
    let p = priority();
    crate::check(p == 20, "FIFO-priority");
    p
}
unsafe extern "C" {
    fn mp_read() -> i32;
    fn mp_spawn(entry: extern "C" fn(usize, usize), context: usize, role: usize) -> i32;
    fn mp_join(role: usize) -> i32;
}
pub fn read() -> Option<u8> {
    let v = unsafe { mp_read() };
    if v < 0 { None } else { Some(v as u8) }
}
pub struct Threads {
    count: usize,
}
impl Threads {
    pub fn new() -> Self {
        Self { count: 0 }
    }
    pub fn spawn(&mut self, entry: extern "C" fn(usize, usize), context: usize, role: usize) {
        crate::check(
            unsafe { mp_spawn(entry, context, role) } == 0,
            "thread-create",
        );
        self.count += 1;
    }
    pub fn join(self) {
        for role in 1..=self.count {
            crate::check(unsafe { mp_join(role) } == 0, "thread-join");
        }
    }
}

pub fn execute(words: &[&str]) {
    crate::execute_native(words);
}

pub fn main() {
    crate::mpsc_entry();
}
