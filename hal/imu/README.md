# IMU capability

The single `nxrs-imu` crate contains the backend-free API and deterministic
synthetic provider used by the event-driven architecture demo. `NXRS_PLATFORM`
selects the provider; unset builds select `none`.

There is no physical/native/NuttX IMU implementation yet. The demo mock is an
explicit, non-default provider selection and is not evidence of hardware support.
