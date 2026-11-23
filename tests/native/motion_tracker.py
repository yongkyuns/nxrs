"""Run Motion Tracker with mock sensors on a host with no display binding."""

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
    live = []
    for frame in frames:
        assert frame["v"] == 1 and "gnss" in frame, frame
        assert len(frame["imu"]) == 2, frame
        for imu in frame["imu"]:
            assert imu["state"] in ("waiting", "live"), frame
            if imu["state"] == "live":
                assert imu["interval_us"] > 0, imu
                assert len(imu["accel_mps2"]) == len(imu["gyro_rps"]) == 3, imu
        if frame["imu"][0]["state"] == "live":
            live.append(frame["imu"][0]["seq"])
    assert len(set(live)) >= 2 and live == sorted(live), live
    assert all(imu["state"] == "live" for imu in frames[-1]["imu"]), frames[-1]
    assert frames[-1]["gnss"]["state"] == "live", frames[-1]
    print(f"Motion Tracker exited cleanly after {len(frames)} frames. Live IMU sequences {live}.")


if __name__ == "__main__":
    main()
