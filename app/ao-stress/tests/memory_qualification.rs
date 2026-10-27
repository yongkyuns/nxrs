//! A dedicated process keeps libtest's own allocations out of measured windows.
#![forbid(unsafe_code)]
// #[test] functions in the reused app modules have no libtest registration here.
#[allow(dead_code, unused_imports)]
#[path = "../src/main.rs"]
mod app;

fn main() {
    app::qualify_memory();
}
