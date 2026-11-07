#[path = "build/emit.rs"]
mod emit;

fn main() {
    nxrs_build_support::configure_hal_with(|profile, os, out| {
        if profile.binding("imu") == "simulation" {
            nxrs_build_support::write_simulation_config(profile, out)?;
        }
        if profile.binding("imu") == "nuttx" && os == "nuttx" {
            let graph = profile
                .composition()?
                .ok_or("NuttX IMU has no resolved peripheral composition")?;
            let binding = graph
                .imu()
                .ok_or("NuttX IMU composition has no selected binding")?;
            emit::write(&out.join("imu_config.rs"), binding)?;
        }
        Ok(())
    })
    .unwrap_or_else(|error| panic!("{error}"));
}
