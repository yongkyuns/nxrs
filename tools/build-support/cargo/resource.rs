fn main() {
    nxrs_build_support::configure_resource().unwrap_or_else(|error| panic!("{error}"));
}
