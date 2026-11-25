/// A snapshot of the battery and its external power input.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Status {
    pub external_power: bool,
    /// `None` when the device reports no battery.
    pub battery: Option<Battery>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Battery {
    /// The gauge's state-of-charge estimate in whole percent, from 0 to 100.
    /// `None` means the gauge could not supply an estimate.
    pub percent: Option<u8>,
    pub charge_state: ChargeState,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChargeState {
    Charging,
    Discharging,
    Standby,
    Unknown,
}

pub trait Gauge {
    type Error;

    fn status(&mut self) -> Result<Status, Self::Error>;
}
