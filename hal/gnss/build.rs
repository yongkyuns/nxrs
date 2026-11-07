use nxrs_build_support::composition::GnssModel;

fn main() {
    nxrs_build_support::configure_hal_with(|profile, os, out| {
        match profile.binding("gnss") {
            "simulation" => nxrs_build_support::write_simulation_config(profile, out)?,
            "nuttx" if os == "nuttx" => {
                let graph = profile.composition()?.ok_or("NuttX GNSS requires composition")?;
                let binding = graph.gnss().ok_or("NuttX GNSS requires a receiver binding")?;
                match binding.model {
                    GnssModel::NeoM9n => {
                        let source = format!(
                            "const DEVICE_PATH: &str = {:?};\nconst BAUD: u32 = {};\nconst STACK_BYTES: usize = {};\n",
                            binding.path, binding.baud, binding.stack_bytes
                        );
                        std::fs::write(out.join("gnss_config.rs"), source)
                            .map_err(|error| error.to_string())?;
                    }
                }
            }
            _ => {}
        }
        Ok(())
    })
    .unwrap_or_else(|error| panic!("{error}"));
}
