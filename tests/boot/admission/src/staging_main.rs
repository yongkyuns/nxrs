use nxrs_image_admission_qualification::{
    fixture::{self, trusted, AdmissionMode, Case, Expected, CASES, MODULUS, OTHER_MODULUS},
    native::Native,
};
mod staging;

fn run() -> u8 {
    let mut arguments = std::env::args().skip(1);
    let mode = arguments.next();
    if arguments.next().is_some() {
        return 2;
    }
    match mode.as_deref() {
        None => fixture::run(AdmissionMode::Authenticate),
        Some("floor-refusal") => fixture::run(AdmissionMode::FloorRefusal),
        Some("staging") => staging::qualify(),
        Some("root-refusal") => staging::root_refusal(),
        _ => 2,
    }
}

fn main() -> std::process::ExitCode {
    fixture::execute(run)
}
