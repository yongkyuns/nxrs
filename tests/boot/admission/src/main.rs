use nxrs_image_admission_qualification::fixture::{self, AdmissionMode};

fn run() -> u8 {
    let mut arguments = std::env::args().skip(1);
    let mode = arguments.next();
    if arguments.next().is_some() {
        return 2;
    }
    match mode.as_deref() {
        None => fixture::run(AdmissionMode::Authenticate),
        Some("floor-refusal") => fixture::run(AdmissionMode::FloorRefusal),
        _ => 2,
    }
}

fn main() -> std::process::ExitCode {
    fixture::execute(run)
}
