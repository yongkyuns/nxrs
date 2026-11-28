# Independent telemetry consumer

This small application declares its own renamed dependencies and uses no
Motion Tracker code. It creates exactly one IMU publication endpoint and one
reader, adapts that observation to the fixed navigation wire format, and drives
collector reports through a caller-owned `Write` implementation. The unused wire
slot is represented as waiting; no second observation source is allocated.

The telemetry dependency disables default features. The host test copies only
declared package dependencies into a temporary standalone workspace, generates
that application's own lockfile, builds it and executes its assertions. It checks
the compiled package set for absence of BLE, cellular, TLS and execution owners.
The small map protocol dependency is permitted because the public navigation
wire format includes map keys.

Run the focused check from the repository root:

```sh
PYTHONPATH=tests/host python3 -m unittest \
  test_external_consumers.ExternalConsumerTests.test_telemetry_consumer_without_transport_runtime
```

The fixture selects no hardware profile and starts no device or worker. It
establishes independent composition and public stream behavior; target ABI,
firmware footprint and active network transport are separate qualification.
