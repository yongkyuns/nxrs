pub fn main() {
    if std::env::var_os("CARGO_FEATURE_PERSON_MODEL").is_none() {
        return;
    }
    let model = std::path::PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap())
        .join("models/person_detect.tflite");
    println!("cargo:rerun-if-changed={}", model.display());
    println!("cargo:rerun-if-changed=build.rs");
    let output = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("network.rs");
    std::fs::write(
        output,
        format!("#[microflow::model({model:?})]\nstruct Network;\n"),
    )
    .unwrap();
}
