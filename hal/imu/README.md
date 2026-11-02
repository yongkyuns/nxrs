# IMU capability

`nxrs-imu` owns communication, device configuration and conversion into hardware-
independent acceleration (m/s²) and angular velocity (rad/s). Services receive
only `ImuSample`. The explicit mock remains available; unset builds acquire none.

The NuttX provider owns an I²C descriptor and uses the pinned no-std `bmi2`
protocol driver for BMI270. The profile supplies its bus, address and frequency.
Reads require both data-ready flags and a new sensor counter; no new sample is
`Busy`. The timestamp is the owner’s monotonic reception time. There are no
provider worker threads. Drop disables sensing and verifies it before closing;
failed initialization or shutdown quarantines acquisition.

The adapter uses a fixed 32-byte initialization burst, requires the exact
successful initialization status, and avoids the dependency’s reserved-value
panic decoders. Host tests cover those guards and SI conversion, not physical
sensor timing, axes/mounting calibration or I²C electrical behavior.
