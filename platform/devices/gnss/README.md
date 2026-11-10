# GNSS device catalogue

Applications select `gnss_devices::primary_gnss()` or a configured device name;
services receive its `gnss::Device` reference. `aliases.gnss` selects the primary;
`gnss_<name>` adds another alias. Aliases share a canonical receiver and its Busy
and fault state. Separate UART receivers acquire independently.

The catalogue imports `driver/ubx/bindings.toml` and supplies concrete construction.
The GNSS HAL owns UART configuration/restoration, acquisition and delivery.
The `ubx` feature includes the driver; product builds derive it from the profile.
No optional driver is enabled by default. Native timing and receiver RF behavior
still require physical qualification.
