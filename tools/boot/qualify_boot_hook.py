"""Actual host MCUboot callback integration; no target/device qualification."""
import json
from pathlib import Path
import shutil
import tomllib

from cryptography.hazmat.primitives import serialization

import qualify as native
import qualify_admission as admission
import sign

ROOT = native.ROOT
TESTS = ("signed_wrong_manifest_is_accepted_only_without_hook",
         "all_signed_cases_keep_stock_signature_and_floor_enforcement",
         "compiled_root_is_required_and_restored",
         "signed_secondary_is_checked_before_trial_and_revert",
         "wrong_policy_secondary_retains_valid_primary")
INPUTS = ("tools/boot/qualify_boot_hook.py", "tools/boot/qualify_admission.py",
          "tests/boot/policy_hook.rs", "driver/mcuboot-policy/src/ffi/regular_hooks.c",
          "driver/mcuboot-policy/Cargo.toml", "driver/mcuboot-policy/src/lib.rs",
          "driver/mcuboot-policy/src/ffi/sdk.c", "driver/firmware-image/Cargo.toml",
          "driver/firmware-image/src/lib.rs", "driver/firmware-image/src/policy.rs",
          "tools/boot/qualify_signing.py", "tools/boot/sign.py", "tests/boot/Cargo.lock",
          "tools/boot/qualify.py",
          "Cargo.toml", "Cargo.lock", "tools/boot/test_boot_hook.py",
          ".github/workflows/firmware-signing.yml", "docs/mcuboot-policy-hook.md")


def checked_graph(before, after):
    """Allow two local Rust components and their exact edges, no registry drift."""
    original = tomllib.loads(before)["package"]
    updated = tomllib.loads(after)["package"]
    additions = {"nxrs-mcuboot-policy", "nxrs-firmware-image"}
    sign.require(len(updated) == len(original) + 2)
    sign.require(all(sum(p["name"] == name for p in updated) == 1 for name in additions))
    retained = []
    for package in updated:
        if package["name"] in additions:
            expected = {"name": package["name"], "version": "0.1.0"}
            if package["name"] == "nxrs-mcuboot-policy":
                expected["dependencies"] = ["nxrs-firmware-image"]
            sign.require(package == expected)
        else:
            package = dict(package)
            if package["name"] == "bootsim":
                dependencies = package["dependencies"]
                sign.require(dependencies.count("nxrs-mcuboot-policy") == 1)
                package["dependencies"] = [d for d in dependencies if d != "nxrs-mcuboot-policy"]
            retained.append(package)
    sign.require(retained == original)


def verify_execution(output, labels, enabled):
    required = TESTS if enabled else (TESTS[0],)
    native.require_tests(output, required, "image::nxrs_policy_hook::")
    sign.require(not any(word in output for word in ("FAILED", "panicked at", "Assertion failed")))
    actual = [line for line in output.splitlines() if line.startswith("BOOT_POLICY_CASE ")]
    expected = ["BOOT_POLICY_CASE " + label + " passed" for label in labels] if enabled else []
    sign.require(actual == expected)
    return list(required)


def qualify(work, baseline, configuration, environment, auditor, database):
    inputs = {name: native.digest(ROOT / name) for name in INPUTS}
    folder = work / "boot-hook"
    folder.mkdir(mode=0o700)
    generated, labels, _ = admission.fixtures(folder, configuration["target"], settings=configuration)
    # A separate SDK source/build preserves the earlier mandatory baseline and
    # sole-key reports, including their actual source/graph identities.
    source = folder / "sdk"
    shutil.copytree(baseline, source)
    public_data = (folder / "authority/policy.json").read_bytes()
    policy_pin = sign.sha(public_data)
    _, public = sign.public_policy(public_data, policy_pin)
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
    sign.require(len(der) == 398)
    key_source = source / "sim/mcuboot-sys/csupport/keys.c"
    key_source.write_text("#include <bootutil/sign_key.h>\nconst unsigned char nxrs_root[] = {" +
        ",".join(map(str, der)) + "};\nconst unsigned int nxrs_root_len=398;\n" +
        "const struct bootutil_key bootutil_keys[]={{.key=nxrs_root,.len=&nxrs_root_len}};\n" +
        "const int bootutil_key_cnt=1;\n")
    build = source / "sim/mcuboot-sys/build.rs"
    text = build.read_text()
    anchor = "    let mut conf = CachedBuild::new();"
    sign.require(text.count(anchor) == 1)
    extra = "\n".join('    conf.file(' + json.dumps(str(ROOT / name)) + ');' for name in
                        ("driver/mcuboot-policy/src/ffi/sdk.c", "driver/mcuboot-policy/src/ffi/regular_hooks.c"))
    text = text.replace(anchor, anchor + "\n" + extra)
    build.write_text(text)
    module = source / "sim/src/image.rs"
    module.write_text(module.read_text() + '\n#[cfg(test)]\n#[path="../tests/nxrs/policy_hook.rs"]\nmod nxrs_policy_hook;\n')
    shutil.copyfile(ROOT / "tests/boot/policy_hook.rs", source / "sim/tests/nxrs/policy_hook.rs")
    manifest = source / "sim/Cargo.toml"
    manifest.write_text(manifest.read_text() + '\n[dependencies.nxrs-mcuboot-policy]\npath = ' +
                        json.dumps(str(ROOT / "driver/mcuboot-policy")) + "\n")
    original_lock = (source / "Cargo.lock").read_text()
    env = dict(environment, CARGO_TARGET_DIR=str(folder / "target-disabled"), NXRS_BOOT_HOOK_CASES=str(generated))
    logs = work / "logs"
    status, _ = native.run(["cargo", "+1.90.0", "generate-lockfile", "--offline", "--manifest-path", str(manifest)],
                           work, env, logs, "hook-graph", 60)
    sign.require(status == 0)
    checked_graph(original_lock, (source / "Cargo.lock").read_text())
    audit = [str(Path(shutil.which(auditor) or auditor).absolute()), "audit", "--json", "--deny", "unsound",
             "--file", str(source / "Cargo.lock")]
    if database:
        audit += ["--db", str(database.resolve())]
    status, output = native.run(audit, work, env, logs, "hook-rustsec", 180)
    advisory = native.audit_result(status, output)
    controls = []
    fixture_hashes = {label: native.digest(folder / (label + ".bin")) for label in labels}
    for enabled in (False, True):
        if enabled:
            sign.require(build.read_text().count(anchor) == 1)
            build.write_text(build.read_text().replace(anchor, anchor +
                '\n    conf.conf.define("MCUBOOT_IMAGE_ACCESS_HOOKS", None);'))
            env["CARGO_TARGET_DIR"] = str(folder / "target-enabled")
        cases = generated.read_text()
        generated.write_text(("const HOOK_ENABLED: bool = " + str(enabled).lower() + ";\n") + cases)
        status, output = native.run(["cargo", "+1.90.0", "test", "--locked", "--manifest-path", str(manifest),
            "--features", native.FEATURES, "--lib",
            "nxrs_policy_hook" if enabled else "nxrs_policy_hook::" + TESTS[0],
            "--", "--test-threads=1", "--show-output"],
            work, env, logs, "hook-" + ("enabled" if enabled else "disabled"), 600)
        generated.write_text(cases)
        sign.require(status == 0)
        required = verify_execution(output, labels, enabled)
        controls.append({"hook_enabled": enabled, "required_tests": required})
    for name, expected in inputs.items():
        native.verify(ROOT / name, expected)
    sign.require(fixture_hashes == {label: native.digest(folder / (label + ".bin")) for label in labels})
    status, revision = native.run(["git", "rev-parse", "HEAD"], ROOT, env, logs, "hook-repository-head", 30)
    sign.require(status == 0 and len(revision.strip()) == 40)
    status, changes = native.run(["git", "status", "--porcelain=v1", "--untracked-files=normal"],
                                 ROOT, env, logs, "hook-repository-state", 30)
    sign.require(status == 0)
    report = {"schema": 1, "scope": "actual-host-mcuboot-loader-and-production-rust-callback",
        "qualified": True, "inputs": inputs, "repository_head": revision.strip(),
        "repository_has_changes": bool(changes.strip()), "fixtures": fixture_hashes,
        "mcuboot": json.loads((ROOT / "security/boot-dependencies.json").read_text())["mcuboot"],
        "public_policy_sha256": policy_pin, "actual_compiled_root_pkcs1_sha256": sign.sha(der),
        "native_key_source_sha256": native.digest(key_source), "native_build_script_sha256": native.digest(build),
        "host_lock_sha256": native.digest(source / "Cargo.lock"), "rustsec": advisory,
        "controls": controls, "required_cases": labels,
        "signature_validation": "stock-SDK-required-after-policy-continuation",
        "boot_policy_hook_integrated": "host-simulator-only", "simulator_geometry": "K64f",
        "protected_device_counter": False, "target_linked": False, "boot_stack_qualified": False,
        "watchdog_qualified": False, "physical_qualified": False, "deployment_permitted": False}
    destination = work / "boot-hook-qualification.json"
    destination.write_bytes(sign.canonical(report) + b"\n")
    print("Actual MCUboot loader callback, signed policy, signature/floor and fallback controls passed")
    return native.digest(destination)
