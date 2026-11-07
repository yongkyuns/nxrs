fn main() {
    nxrs_build_support::configure_hal_with(|profile, _, out| {
        if profile.binding("gnss") == "simulation" {
            nxrs_build_support::write_simulation_config(profile, out)?;
        }
        Ok(())
    })
    .unwrap_or_else(|error| panic!("{error}"));
}
