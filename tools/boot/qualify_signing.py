#!/usr/bin/env python3
"""Fresh encrypted authority -> actual imgtool -> sole-key native MCUboot qualification."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import sys

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

import qualify as native
import sign

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("fresh_authority_image_is_accepted", "other_authority_key_is_refused",
            "sdk_example_key_is_refused", "stored_security_floor_is_enforced",
            "authenticated_image_mutation_is_refused")
VERSIONS = {"imgtool": "2.4.0", "cryptography": "50.0.2", "cffi": "2.1.1",
            "pycparser": "3.11", "click": "8.5.0", "intelhex": "2.3.0",
            "cbor2": "6.1.5", "PyYAML": "6.0.3"}


def qualify(args):
    sign.require(sys.version_info >= (3, 12) and sys.byteorder == "little")
    for name, expected in VERSIONS.items():
        sign.require(version(name) == expected)
    native.qualify(args)
    work = args.work.resolve(strict=True)
    spec = json.loads((ROOT / "security/boot-dependencies.json").read_text())
    source = work / "mcuboot" / ("mcuboot-" + spec["mcuboot"]["commit"])
    sign.require((source / "sim/mcuboot-sys/build.rs").read_text().count(
        'conf.conf.define("MCUBOOT_MAX_IMG_SECTORS", Some("128"));') == 1)
    minimal = getattr(args, "minimal_crypto", False)
    crypto_input = ROOT / "tools/boot/mbedtls_config.h"
    if minimal:
        # Baseline fixtures require the upstream key. Execute their entire
        # recovery suite before replacing it with the fresh authority's sole key.
        target_spec = json.loads((ROOT / "security/esp32s3-boot-dependencies.json").read_text())
        native.verify(crypto_input, target_spec["files"]["tools/boot/mbedtls_config.h"])
        shutil.copyfile(crypto_input, source / "sim/mcuboot-sys/csupport/config-rsa.h")
        environment = {"PATH": os.environ["PATH"], "HOME": str(work / "home"),
            "CARGO_HOME": str(work / "cargo"), "CARGO_TARGET_DIR": str(work / "target-minimal"),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
            "RUST_LOG": "error", "CARGO_BUILD_JOBS": "2"}
        status, output = native.run(["cargo", "+1.90.0", "test", "--locked", "--manifest-path",
            str(source / "sim/Cargo.toml"), "--features", native.FEATURES],
            work, environment, work / "logs", "minimal-native-tests", 900)
        sign.require(status == 0)
        native.require_tests(output, native.POLICY_TESTS, "image::nxrs_policy::")
        native.require_tests(output, native.CORE_TESTS)
    configuration = {"schema": 1, "target": "host-k64f-qualification", "format": sign.FORMAT,
        "configuration_sha256": sign.sha(sign.canonical({
            "sdk": spec["mcuboot"]["commit"], "features": native.FEATURES,
            "fixture": "K64f", "boot_max_img_sectors": 128, "boot_max_align": 8})),
        "layout": {"flash_bytes": 512 * 1024, "primary_offset": 0x20000,
            "secondary_offset": 0x40000, "slot_bytes": 0x20000,
            "scratch_offset": 0x60000, "scratch_bytes": 4096, "erase_bytes": 4096,
            "header_bytes": 32, "write_align": 4, "max_align": 8, "max_sectors": 128}}
    for name in ("authority", "wrong-authority", "exports"):
        (work / name).mkdir(mode=0o700)
    # Fresh fixture custody secret exists only in memory; no private key is compiled.
    passphrase = secrets.token_hex(32).encode()
    pin = sign.initialize(work / "authority", configuration, passphrase)
    other_pin = sign.initialize(work / "wrong-authority", configuration, passphrase)
    body = bytes(range(256)) * 128
    for name, authority, policy_pin in (("application.bin", "authority", pin),
                                      ("wrong-key.bin", "wrong-authority", other_pin)):
        sign.sign(work / authority, passphrase, policy_pin, body, "1.2.3+4", 7,
                  work / "exports" / name, sign.sha(body))
    policy_data = (work / "authority/policy.json").read_bytes()
    _, public = sign.public_policy(policy_data, pin)
    image = (work / "exports/application.bin").read_bytes()
    sign.verify_image(image, policy_data, pin, configuration["target"],
                      configuration["configuration_sha256"], 7)
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
    sign.require(len(der) == 398)
    # SDK witness key ABI is data only, beside the upstream simulator's C fixture.
    key_data = "#include <bootutil/sign_key.h>\nconst unsigned char root_pub_der[] = {"
    key_data += ",".join(str(value) for value in der) + "};\n"
    key_data += "const unsigned int root_pub_der_len = 398;\n"
    key_data += "const struct bootutil_key bootutil_keys[] = {{.key=root_pub_der,.len=&root_pub_der_len}};\n"
    key_data += "const int bootutil_key_cnt=1;\n"
    key_source = source / "sim/mcuboot-sys/csupport/keys.c"
    key_source.write_text(key_data)
    folder = source / "sim/tests/nxrs"
    for name in ("application.bin", "wrong-key.bin"):
        shutil.copyfile(work / "exports" / name, folder / name)
    witness = ROOT / "tests/boot/signing_policy.rs"
    shutil.copyfile(witness, folder / "signing_policy.rs")
    module = source / "sim/src/image.rs"
    module.write_text(module.read_text() + '\n#[cfg(all(test, feature="sig-rsa3072", '
        'feature="validate-primary-slot", feature="hw-rollback-protection"))]\n'
        '#[path="../tests/nxrs/signing_policy.rs"]\nmod nxrs_signing_policy;\n')
    environment = {"PATH": os.environ["PATH"], "HOME": str(work / "home"),
        "CARGO_HOME": str(work / "cargo"), "CARGO_TARGET_DIR": str(work / "target-signing"),
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        "RUST_LOG": "error", "CARGO_BUILD_JOBS": "2"}
    # Separate native build directory makes stale sample-key object reuse impossible.
    status, output = native.run(["cargo", "+1.90.0", "test", "--locked", "--manifest-path",
        str(source / "sim/Cargo.toml"), "--features", native.FEATURES, "--lib", "nxrs_signing_policy"],
        work, environment, work / "logs", "signing-native-tests", 600)
    sign.require(status == 0)
    native.require_tests(output, REQUIRED, "image::nxrs_signing_policy::")
    native.verify(source / "Cargo.lock", spec["files"]["tests/boot/Cargo.lock"])
    report = {"schema": 1, "scope": "host-native-signing-interoperability-only", "qualified": True,
        "baseline_qualification_sha256": native.digest(work / "qualification.json"),
        "inputs": {name: native.digest(ROOT / name) for name in (
            "tools/boot/sign.py", "tools/boot/requirements.txt", "tools/boot/qualify_signing.py",
            "tests/boot/signing_policy.rs")},
        "operator_versions": {name: version(name) for name in VERSIONS},
        "python": sys.version.split()[0], "python_ssl": ssl.OPENSSL_VERSION,
        "cryptography_ssl": default_backend().openssl_version_text(),
        "public_policy_sha256": pin, "public_key_pkcs1_sha256": sign.sha(der),
        "native_key_fixture_sha256": native.digest(key_source),
        "target_minimal_crypto_executed": minimal,
        "target_crypto_configuration_sha256": native.digest(crypto_input) if minimal else None,
        "required_minimal_policy_tests": list(native.POLICY_TESTS) if minimal else [],
        "native_image_module_sha256": native.digest(module),
        "signed_image_sha256": sign.sha(image), "required_native_tests": list(REQUIRED),
        "physical_qualified": False, "production_firmware_built": False,
        "device_target_layout_enforcement_qualified": False, "native_advisory_closure": False,
        "fips_validated": False}
    with (work / "signing-qualification.json").open("x") as destination:
        json.dump(report, destination, indent=2)
        destination.write("\n")
    print("Fresh offline-authority/native-verifier interoperability and refusal controls passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--auditor", default="cargo-audit")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--minimal-crypto", action="store_true")
    args = parser.parse_args()
    try:
        qualify(args)
    except Exception:
        print("Signing native qualification failed; inspect private local logs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
