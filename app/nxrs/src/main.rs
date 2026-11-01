//! Portable application entry. HAL factories own resource admission.
#![forbid(unsafe_code)]
mod cli;
fn main() -> std::process::ExitCode {
    cli::main()
}
