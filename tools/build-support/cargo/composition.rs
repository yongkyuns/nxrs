fn main() {
    nxrs_build_support::configure_composition().unwrap_or_else(|error| panic!("{error}"));
}
