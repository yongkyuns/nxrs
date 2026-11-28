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
    live = []
    for frame in frames:
        assert frame["v"] == 1, frame
        assert frame["gnss"]["state"] in ("waiting", "live"), frame
        imu, secondary = frame["imu"]
        assert secondary["state"] in ("waiting", "live"), frame
        assert imu["state"] in ("waiting", "live"), frame
        if imu["state"] == "live":
            assert imu["interval_us"] > 0, imu
            assert len(imu["accel_mps2"]) == len(imu["gyro_rps"]) == 3, imu
            live.append(imu["seq"])
    assert len(set(live)) >= 2 and live == sorted(live), live
    assert frames[-1]["imu"][0]["state"] == "live", frames[-1]
    assert frames[-1]["gnss"]["state"] == "live", frames[-1]
    assert frames[-1]["imu"][1]["state"] == "live", frames[-1]
    print(f"Motion Tracker exited cleanly after {len(frames)} frames. Live IMU sequences {live}.")

    # A closed diagnostic reader must not stop healthy acquisition.
    root = Path(__file__).resolve().parents[2]
    target = Path(os.environ.get("CARGO_TARGET_DIR", root / "target"))
    process = subprocess.Popen(
        [str(target / "debug" / "motion-tracker"), "--interval-ms", "100",
         "--frames", "12", "--stream-usb"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(4):
            frame = json.loads(process.stdout.readline())
        assert frame["imu"][0]["state"] == "live", frame
        process.stdout.close()
        status = process.wait(timeout=10)
        stderr = process.stderr.read()
        assert status == 0 and "motion-tracker: 12 frames" in stderr, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    print("Closing the diagnostic reader preserved acquisition and clean shutdown.")


if __name__ == "__main__":
    main()
