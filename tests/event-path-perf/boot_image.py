"""P2's opt-in binding of the unchanged P0 SIMPLE_BOOT image/loader profile.

Packing is reproduced offline from retained bytes. Neither the prefix digest
nor a matching loader receipt establishes a physical boot or timing result.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path, PurePosixPath
import sys


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "tests/std-channel-perf"


def _load_helper():
    # P0 uses ordinary sibling imports. Refuse an unrelated cached module and
    # restore the caller's search path even if the import fails.
    for name in ("inspect_esp_image", "simpleboot_digest"):
        if name in sys.modules:
            origin = getattr(sys.modules[name], "__file__", None)
            if not isinstance(origin, str) or Path(origin).resolve() != BASE / (name + ".py"):
                raise ImportError("P0 boot image requires its own " + name + " module")
    saved = list(sys.path)
    sys.path.insert(0, str(BASE))
    try:
        return importlib.import_module("simpleboot_digest")
    finally:
        sys.path[:] = saved


helper = _load_helper()
DEFAULT = "platform-default"
PROFILE = helper.PROFILE
require = helper.require

_IMAGE = "firmware/nuttx/nuttx.rom-digest.bin"
_CONFIG = "firmware/resolved.config"
_FIELDS = {
    "simpleboot_digest_report": "firmware/simpleboot-digest.json",
    "unmodified_source_image": "firmware/nuttx/nuttx.merged.bin",
    "simpleboot_patch_receipt": "firmware/nuttx-patches.json",
    "simpleboot_loader_source": "firmware/nuttx/" + helper.LOADER_PATH,
    "simpleboot_loader_patch": "firmware/" + helper.PATCH_NAME,
}
_PATCH_SOURCE = "upstream/nuttx/patches/" + helper.PATCH_NAME
_SOURCE_FILES = (
    "tests/std-channel-perf/simpleboot_digest.py",
    "tests/std-channel-perf/inspect_esp_image.py",
    "tools/apply-nuttx-patches.py",
    _PATCH_SOURCE,
)
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_BYTES = 1024 * 1024


def validate_profile(profile, platform):
    require(isinstance(profile, str) and profile in (DEFAULT, PROFILE),
            "unknown boot image profile")
    require(profile == DEFAULT or platform == "esp32s3-service-footprint",
            "digest candidate requires the ESP32-S3 service-footprint profile")


def _inside(directory, name):
    require(isinstance(name, str) and name and "\\" not in name and "\x00" not in name,
            "invalid boot evidence path")
    relative = PurePosixPath(name)
    require(not relative.is_absolute() and ".." not in relative.parts
            and relative.as_posix() == name and name != ".", "boot evidence path must be relative")
    root = Path(directory).resolve()
    path = (root / name).resolve()
    require(path.is_relative_to(root) and path.is_file(),
            "boot evidence is missing or escapes the build directory")
    return path


def _read(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "boot evidence exceeds its size bound")
    return data


def _json(data):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, "duplicate boot evidence JSON key")
            result[name] = value
        return result

    value = json.loads(data, object_pairs_hook=unique)
    require(isinstance(value, dict), "boot evidence must be a JSON object")
    return value


def _artifact(directory, record, name, limit):
    inventory = record.get("artifacts_sha256")
    require(isinstance(inventory, dict) and name in inventory,
            "boot evidence absent from artifact inventory: " + name)
    data = _read(_inside(directory, name), limit)
    require(helper.sha(data) == inventory[name], "boot artifact hash mismatch: " + name)
    return data


def _configuration(data):
    lines = data.decode("utf-8").splitlines()
    for key in ("CONFIG_ARCH_CHIP_ESP32S3", "CONFIG_ESPRESSIF_SIMPLE_BOOT"):
        declarations = [line for line in lines if line.startswith(key + "=")
                        or line == "# " + key + " is not set"]
        require(declarations == [key + "=y"], "candidate requires exactly one enabled " + key)


def prepare(firmware: Path, input_image: Path, out: Path):
    """Reuse P0's exclusive packer and retain its small loader/patch inputs."""
    out = Path(out).resolve()
    firmware = Path(firmware).resolve()
    require(firmware == out / "firmware", "unexpected P2 firmware directory")
    original = _inside(out, _FIELDS["unmodified_source_image"])
    require(Path(input_image).resolve() == original, "unexpected original ESP image")
    _read(original, helper.MAX_IMAGE_BYTES)
    _configuration(_read(_inside(out, _CONFIG), _MAX_SOURCE_BYTES))
    _json(_read(_inside(out, _FIELDS["simpleboot_patch_receipt"]), _MAX_RECEIPT_BYTES))
    _read(_inside(out, _FIELDS["simpleboot_loader_source"]), _MAX_SOURCE_BYTES)
    patch = _read(helper.PATCH, _MAX_SOURCE_BYTES)
    retained_patch = out / _FIELDS["simpleboot_loader_patch"]
    require(not retained_patch.exists() and not retained_patch.is_symlink(),
            "retained loader patch already exists")
    candidate, report = helper.prepare(firmware, original)
    require(candidate.resolve() == out / _IMAGE
            and report.resolve() == out / _FIELDS["simpleboot_digest_report"],
            "unexpected P0 candidate outputs")
    with retained_patch.open("xb") as stream:
        stream.write(patch)
    require(_read(retained_patch, _MAX_SOURCE_BYTES) == patch
            == _read(helper.PATCH, _MAX_SOURCE_BYTES), "loader patch changed during retention")
    return candidate, dict(_FIELDS), [out / name for name in _FIELDS.values()]


def verify(directory, record):
    """Check retained packing, exact pinned loader and declared profile offline."""
    require(isinstance(record, dict), "invalid boot image receipt")
    profile = record.get("boot_image_profile", DEFAULT)
    validate_profile(profile, record.get("platform"))
    if profile == DEFAULT:
        require(not any(name in record for name in _FIELDS),
                "candidate evidence requires an explicit boot image profile")
        inventory = record.get("artifacts_sha256", {})
        names = [record.get("image_file", "")]
        if isinstance(inventory, dict):
            names.extend(inventory)
        require(not any(isinstance(name, str) and PurePosixPath(name).name == "nuttx.rom-digest.bin"
                        for name in names), "candidate output requires an explicit boot image profile")
        # Legacy receipts and synthetic fixtures remain readable. For a
        # recognizable ESP32-S3 image, the header alone prevents a candidate
        # from becoming "default" by deleting its fields and renaming it.
        # This does not claim support for arbitrary ESP image layouts.
        if record.get("platform") == "esp32s3-service-footprint" and "image_file" in record:
            with _inside(directory, record["image_file"]).open("rb") as stream:
                header = stream.read(24)
            if len(header) == 24 and header[0] == 0xe9 and header[12:14] == b"\x09\x00":
                require(header[23] == 0, "digest-bearing ESP image requires the candidate profile")
        return

    require(record.get("target") == "xtensa-esp32s3-nuttx"
            and record.get("sdk") == "esp-1.90.0.0", "candidate target/SDK mismatch")
    require(record.get("kernel_revision") == helper.KERNEL_REVISION, "candidate kernel pin mismatch")
    for field, name in _FIELDS.items():
        require(record.get(field) == name, "candidate evidence path mismatch: " + field)
    require(record.get("image_file") == _IMAGE and record.get("config_file") == _CONFIG,
            "candidate image/configuration path mismatch")

    original = _artifact(directory, record, _FIELDS["unmodified_source_image"], helper.MAX_IMAGE_BYTES)
    image = _artifact(directory, record, _IMAGE, helper.MAX_IMAGE_BYTES)
    require(helper.sha(image) == record.get("image_sha256"), "candidate image identity mismatch")
    configuration = _artifact(directory, record, _CONFIG, _MAX_SOURCE_BYTES)
    require(helper.sha(configuration) == record.get("config_sha256"), "candidate configuration identity mismatch")
    _configuration(configuration)
    packing = _json(_artifact(directory, record, _FIELDS["simpleboot_digest_report"], _MAX_RECEIPT_BYTES))
    _json(_artifact(directory, record, _FIELDS["simpleboot_patch_receipt"], _MAX_RECEIPT_BYTES))
    _artifact(directory, record, _FIELDS["simpleboot_loader_source"], _MAX_SOURCE_BYTES)
    patch = _artifact(directory, record, _FIELDS["simpleboot_loader_patch"], _MAX_SOURCE_BYTES)

    sources = record.get("source_sha256")
    require(isinstance(sources, dict), "missing candidate source inventory")
    for name in _SOURCE_FILES:
        expected = helper.sha(_read(ROOT / name, _MAX_SOURCE_BYTES))
        require(sources.get(name) == expected, "candidate source inventory mismatch: " + name)
    require(helper.sha(patch) == sources[_PATCH_SOURCE], "retained loader patch differs from source inventory")
    # The unchanged P0 verifier consumes only this retained firmware's ledger
    # and actual loader bytes, together with the pinned patch in verifier source.
    loader = helper.verify_loader(Path(directory) / "firmware")
    require(loader["patch_sha256"] == helper.sha(patch)
            and loader["kernel_revision"] == record["kernel_revision"], "candidate loader proof mismatch")
    reproduced, expected = helper.repack(original)
    require(reproduced == image, "candidate differs from retained original's exact repacking")
    expected.update(loader=loader,
                    source_image=PurePosixPath(_FIELDS["unmodified_source_image"]).name,
                    output_image=PurePosixPath(_IMAGE).name, qualification="build-candidate-only")
    require(json.dumps(packing, sort_keys=True, separators=(",", ":"), allow_nan=False)
            == json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False),
            "candidate packing receipt differs from recomputed evidence")
