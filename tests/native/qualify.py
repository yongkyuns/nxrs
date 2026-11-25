"""Qualify real replay, recording and UDP with unset, mock and host profiles."""
from pathlib import Path
import os
import subprocess
import sys


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    host = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    for profile in (None, "mock", host):
        env = dict(os.environ)
        env.pop("NXRS_PLATFORM", None)
        if profile is not None:
            env["NXRS_PLATFORM"] = profile
        print(f"Native camera qualification: NXRS_PLATFORM={profile or '<unset>'}", flush=True)
        subprocess.run(
            ["cargo", "test", "--locked", "-p", "nxrs-native-integration"],
            cwd=root, env=env, check=True, timeout=300,
        )


if __name__ == "__main__":
    main()
