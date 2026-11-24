use connectivity::Endpoint;
use std::time::Duration;

#[cfg_attr(not(feature = "cellular-uplink"), allow(dead_code))]
pub struct CellularConfig {
    pub apn: String,
    pub collector: Option<Endpoint>,
    pub interval: Duration,
}
