fn main() {
    nxrs_build_support::configure_hal().unwrap_or_else(|error| panic!("{error}"));
}
