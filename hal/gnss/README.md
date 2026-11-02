# GNSS capability

The `nxrs-gnss` crate owns receiver communication, bounded protocol decoding and
the hardware-independent fix API. Fixes expose WGS84 latitude/longitude, speed
in m/s and optional movement course in radians. `timestamp_ms` is monotonic
reception time supplied by the owner, not a claimed satellite measurement time.
Navigation owns projection into local north/east coordinates.

The NuttX provider owns a nonblocking UART and private checksum-checked NMEA RMC
decoder. Its profile supplies `[gnss]` `device` and `baud`; services receive only
`GnssFix`. Each poll drains bounded input and emits a newly received fix once;
absence of a new valid fix is reported as `Busy`. Drop restores the serial mode before closing and
releasing acquisition; failed restoration quarantines further acquisition.
The deterministic mock remains explicit. Host tests do not qualify receiver
hardware, UART timing or RF performance.
