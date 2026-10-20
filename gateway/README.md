# Monitoring gateway boundary

This first slice contains the control API contract and its shared-fixture tests;
**it does not implement or deploy the Rust server, MediaMTX, or WireGuard**.

```sh
python3 -m venv gateway/.venv
gateway/.venv/bin/pip install -r gateway/tests/requirements.txt
gateway/.venv/bin/python -m unittest discover -s gateway/tests -v
```

[OpenAPI 3.1 contract](../docs/monitoring-api.json): authenticated camera list and
per-camera playback grants, with no VPN-specific fields. The example origin and
tokens are test values. Runtime checks (scope, origin, expiry, uniqueness,
revocation) are additional obligations beyond JSON-schema validation.

The next gateway slice implements a small Rust control service in an independent
host Cargo workspace under `gateway/`, without changing MCU workspace members or
weakening portable dependency checks. It owns identity, catalog authorization,
short-lived media grants, and MediaMTX authorization integration. Media never
flows through the control service. MediaMTX/encoder images must be pinned and
qualified before a runnable deployment is added; no untested `latest` stack is
presented here as ready to run.

[Coordination and release gates](../docs/remote-monitoring.md) specify the camera
handoff, WireGuard deployment, and future hosted boundary. Keychain, account
provisioning and active-session revocation are not completed by the client code.
