//! Product entry; profile admission is selected once by the build wiring.
#![forbid(unsafe_code)]
mod entry {
    include!(concat!(env!("OUT_DIR"), "/cli_entry.rs"));
}
fn main() -> std::process::ExitCode {
    entry::run()
}
