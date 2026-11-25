#![forbid(unsafe_code)]

fn main() -> std::process::ExitCode {
    nxrs_motion_tracker::run(std::env::args().skip(1))
}
