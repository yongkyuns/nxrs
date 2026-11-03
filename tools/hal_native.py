"""Compatibility for native qualification runners; compilation is owned by Rust."""
from pathlib import Path
import subprocess
from platform_config import _frontend


def build(capability, target_os, target, out):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([str(_frontend()), '--workspace-root', str(root), 'hal-native', '--capability-dir', str(capability),
                             '--os', target_os, '--target', target, '--out', str(out)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.splitlines()
