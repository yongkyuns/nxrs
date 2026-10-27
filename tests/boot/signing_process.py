"""Test-only process barriers for real interrupted offline signing."""
import argparse
import importlib.util
import os
from pathlib import Path
import signal

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("firmware_sign", ROOT / "tools/boot/sign.py")
SIGN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SIGN)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--password-fd", type=int, required=True)
    parser.add_argument("--checkpoint-fd", type=int, required=True)
    parser.add_argument("--phase", choices=["pre-replace", "post-replace", "pre-link", "post-link"], required=True)
    args = parser.parse_args()
    passphrase = SIGN.read_password(args.password_fd)
    replace, link = SIGN.os.replace, SIGN.os.link

    def checkpoint():
        os.write(args.checkpoint_fd, b"ready")
        while True:
            signal.pause()

    def replacing(*values, **options):
        if args.phase == "pre-replace":
            checkpoint()
        result = replace(*values, **options)
        if args.phase == "post-replace":
            checkpoint()
        return result

    def linking(*values, **options):
        if args.phase == "pre-link":
            checkpoint()
        result = link(*values, **options)
        if args.phase == "post-link":
            checkpoint()
        return result

    SIGN.os.replace, SIGN.os.link = replacing, linking
    body = SIGN.untrusted_read(args.input, SIGN.MAX_IMAGE)
    SIGN.sign(args.authority, passphrase, args.policy_sha256, body, "1.2.3+4", 2,
              args.output, SIGN.sha(body))
    raise RuntimeError("test process did not reach its required barrier")


if __name__ == "__main__":
    main()
