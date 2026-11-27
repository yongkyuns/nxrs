"""Run the production tracker with mock sensors and no display."""

import json
import os
from pathlib import Path
import subprocess


def main():
    result = subprocess.run(
        [
            "cargo", "run", "--locked", "-p", "nxrs-motion-tracker",
            "--", "--interval-ms", "100",
            "--frames", "8", "--stream-usb",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "NXRS_PLATFORM": "mock"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"{result.stderr}\n{result.stdout}"
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(frames) == 8, frames
    observed = []
    for frame in frames:
        assert frame["v"] == 1, frame
        assert frame["gnss"]["state"] in ("waiting", "live", "received"), frame
        imu, secondary = frame["imu"]
        assert secondary["state"] in ("waiting", "live", "received"), frame
        assert imu["state"] in ("waiting", "live", "received"), frame
        if imu["state"] in ("live", "received"):
            assert imu["interval_us"] > 0, imu
            assert len(imu["accel_mps2"]) == len(imu["gyro_rps"]) == 3, imu
            observed.append(imu["seq"])
    assert len(set(observed)) >= 2 and observed == sorted(observed), observed
    for source in [*frames[-1]["imu"], frames[-1]["gnss"]]:
        assert source["state"] in ("live", "received"), source
        if source["state"] == "received":
            assert source["time_source"] == "reception" and source["utc_ms"] is None, source
    print(f"Motion Tracker exited cleanly after {len(frames)} frames. IMU sequences {observed}.")

    # Close an optional diagnostic sink while acquisition is active. The actual
    # runtime must finish its requested frame budget and join its owners.
    root = Path(__file__).resolve().parents[2]
    target = Path(os.environ.get("CARGO_TARGET_DIR", root / "target"))
    binary = target / "debug" / "motion-tracker"
    process = subprocess.Popen(
        [str(binary), "--interval-ms", "100", "--frames", "12", "--stream-usb"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(4):
            frame = json.loads(process.stdout.readline())
        assert frame["imu"][0]["state"] in ("live", "received"), frame
        process.stdout.close()
        status = process.wait(timeout=10)
        stderr = process.stderr.read()
        assert status == 0, stderr
        assert "motion-tracker: 12 frames" in stderr, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stderr.close()
    print("Closing the optional USB/stdout sink preserved acquisition and clean shutdown.")


if __name__ == "__main__":
    main()
