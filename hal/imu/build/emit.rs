use nxrs_build_support::{composition::ImuBinding, Result};
use std::{fs, path::Path};

/// Emit only fixed Rust identifiers; all profile strings are escaped literals.
pub fn write(path: &Path, binding: ImuBinding<'_>) -> Result<()> {
    let model = match binding.model {
        nxrs_build_support::composition::ImuModel::Bmi270 => "SensorModel::Bmi270",
        nxrs_build_support::composition::ImuModel::Lsm6dso => "SensorModel::Lsm6dso",
    };
    let irq_path = binding
        .irq_path
        .map(|value| format!("Some({value:?})"))
        .unwrap_or_else(|| "None".into());
    let source = format!(
        "#[allow(dead_code)]\nenum SensorModel {{ Bmi270, Lsm6dso }}\n\
         const MODEL: SensorModel = {model};\n\
         const BINDING: Binding<'static> = Binding {{ path: {path:?}, address: {address}, frequency: {frequency}, irq_path: {irq_path} }};\n\
         const STACK_BYTES: usize = {stack_bytes};\n",
        path = binding.path,
        address = binding.address,
        frequency = binding.frequency,
        stack_bytes = binding.stack_bytes,
    );
    fs::write(path, source).map_err(|error| format!("{}: {error}", path.display()))
}
