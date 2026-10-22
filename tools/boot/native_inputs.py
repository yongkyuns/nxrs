"""Inventory actual native boot build inputs. A link input is not reachable code."""
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile

import qualify as process
import target_plan as policy

CRYPTO_MODULES = {
    "MBEDTLS_ASN1_PARSE_C", "MBEDTLS_ASN1_WRITE_C", "MBEDTLS_BIGNUM_C", "MBEDTLS_MD_C",
    "MBEDTLS_MEMORY_BUFFER_ALLOC_C", "MBEDTLS_OID_C", "MBEDTLS_PLATFORM_C", "MBEDTLS_RSA_C",
    "MBEDTLS_SHA256_C",
}


def bounded(path, limit=16 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        policy.require(stat.S_ISREG(info.st_mode) and info.st_size <= limit)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        policy.require(len(raw) <= limit)
        return raw
    finally:
        os.close(fd)


def defines(text):
    result = {}
    for line in text.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) >= 2 and fields[0] == "#define":
            policy.require(fields[1] not in result)
            result[fields[1]] = fields[2] if len(fields) == 3 else ""
    return result


def crypto_configuration(text):
    values = defines(text)
    modules = {name for name in values if name.startswith("MBEDTLS_") and name.endswith("_C")}
    policy.require(modules == CRYPTO_MODULES)
    policy.require(values.get("MBEDTLS_VERSION_STRING") == '"3.6.7"'
                   and "MBEDTLS_PKCS1_V21" in values and "MBEDTLS_HAVE_ASM" in values)
    policy.require(not any(name in values for name in (
        "NDEBUG", "MBEDTLS_PKCS1_V15", "MBEDTLS_USE_PSA_CRYPTO", "MBEDTLS_TEST_CONSTANT_FLOW_MEMSAN")))
    return {"version": "3.6.7", "modules": sorted(modules), "rsa_padding": "PSS-SHA256",
            "pkcs1_v15": False, "tls": False, "psa": False, "device_rng": False}


def dependency_graph(text):
    result = {}
    name = None
    expected = 0
    for line in text.splitlines():
        if not line.strip():
            if name is not None:
                policy.require(len(result[name]) == expected)
            name = None
            continue
        if line.startswith("    "):
            policy.require(name is not None)
            result[name].append(line[4:])
        else:
            policy.require(name is None)
            match = re.fullmatch(r"(.+): #deps ([0-9]+), deps mtime [0-9]+ \(VALID\)", line)
            policy.require(match is not None)
            name, count = match.groups()
            policy.require(name not in result)
            expected = int(count)
            policy.require(0 < expected < 4096)
            result[name] = []
    if name is not None:
        policy.require(len(result[name]) == expected)
    policy.require(0 < len(result) <= 512)
    return result


def retained_functions(text):
    result = set()
    for line in text.splitlines():
        fields = line.split()
        policy.require(len(fields) in (3, 4))
        name, kind = fields[:2]
        if kind in ("T", "t", "W", "w"):
            policy.require(re.fullmatch(r"[A-Za-z0-9_.$]+", name) is not None)
            policy.require(name not in result)
            result.add(name)
    policy.require("boot_go" in result and "mbedtls_rsa_public" in result)
    # No dynamic symbol expansion into secret-key or unrelated protocol APIs.
    forbidden = ("private", "decrypt", "encrypt", "gen_key", "rsaes", "rsassa", "ssl", "x509", "psa", "ecp", "dhm", "cipher", "lms")
    names = {name for name in result if name.startswith("mbedtls_")}
    policy.require(names and not any(word in name for name in names for word in forbidden))
    return {"function_count": len(result), "mbedtls_functions": sorted(names),
            "forbidden_mbedtls_function_parts": list(forbidden)}


class Inputs:
    def __init__(self, work, source, hal, compiler):
        self.work = Path(work).resolve(strict=True)
        self.build = self.work / "build"
        self.compiler = Path(compiler).resolve(strict=True)
        self.roots = {"mbedtls": Path(source).resolve(strict=True) / "ext/mbedtls",
                      "mcuboot": Path(source).resolve(strict=True), "esp-idf": Path(hal).resolve(strict=True),
                      "toolchain": self.compiler, "generated": self.build,
                      "nxrs": policy.ROOT.resolve(strict=True)}
        self.files = {}
        self.aliases = []
        for name, root in self.roots.items():
            for alias in {str(root), str(root).replace("/private/tmp/", "/tmp/")}:
                self.aliases.append((alias, "@" + name))
        for alias in {str(self.work), str(self.work).replace("/private/tmp/", "/tmp/")}:
            self.aliases.append((alias, "@work"))
        self.aliases.sort(key=lambda item: len(item[0]), reverse=True)

    def identify(self, path):
        path = Path(path).resolve(strict=True)
        roots = sorted(self.roots.items(), key=lambda item: len(str(item[1])), reverse=True)
        for name, root in roots:
            if path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
                policy.require(re.fullmatch(r"[A-Za-z0-9_./+-]+", relative) is not None)
                return name + ":" + relative, path
        policy.require(False)

    def add(self, path):
        identity, path = self.identify(path)
        raw = bounded(path, 64 * 1024 * 1024)
        info = {"id": identity, "sha256": policy.digest(raw), "bytes": len(raw)}
        policy.require(identity not in self.files or self.files[identity] == info)
        self.files[identity] = info
        return identity

    def normalized(self, token):
        for absolute, label in self.aliases:
            token = token.replace(absolute, label)
        policy.require(re.search(r"/(Users|home|tmp|private|runner|Applications)/", token) is None)
        return token

    def command(self, entry):
        arguments = shlex.split(entry["command"])
        policy.require(arguments and Path(arguments[0]).name == "xtensa-esp32s3-elf-gcc"
                       and os.path.samefile(arguments[0], self.compiler / "bin/xtensa-esp32s3-elf-gcc"))
        policy.require("-Os" in arguments
                       and all(not x.startswith(("@", "-mllvm", "-flto")) for x in arguments)
                       and not any(x.startswith("-O") and x != "-Os" for x in arguments))
        return [self.normalized(x) for x in arguments]


def collect(work, source, hal, compiler, environment, report):
    from build_esp32s3 import compiler_command
    inputs = Inputs(work, source, hal, compiler)
    work, build = inputs.work, inputs.build
    logs = Path(tempfile.mkdtemp(prefix="native-inputs-", dir=work / "logs"))
    entries = json.loads(bounded(build / "compile_commands.json"))
    policy.require(0 < len(entries) <= 512)
    ninja = str(Path(__import__("sys").executable).parent / "ninja")
    status, output = process.run([ninja, "-C", str(build), "-t", "deps"],
                                 work, environment, logs, "native-dependencies", 60)
    policy.require(status == 0)
    graph = dependency_graph(output)
    policy.require(set(graph) == {entry["output"] for entry in entries})
    units, objects = [], {}
    for entry in entries:
        obj = (Path(entry["directory"]) / entry["output"]).resolve(strict=True)
        policy.require(obj.is_relative_to(build) and obj not in objects)
        source_id = inputs.add(entry["file"])
        policy.require(source_id not in {unit["source"] for unit in units})
        dependencies = sorted(set(inputs.add(Path(entry["directory"]) / path) for path in graph[entry["output"]]))
        policy.require(source_id in dependencies)
        argv = inputs.command(entry)
        unit = {"source": source_id, "object_sha256": policy.digest(bounded(obj)),
                "object_bytes": obj.stat().st_size, "argv_sha256": policy.digest(policy.canonical(argv)),
                "dependencies": dependencies, "provided_as": "hal-archive" if entry["output"].startswith("hal/") else "direct-object"}
        units.append(unit)
        objects[obj] = source_id
    # Inspect effective crypto definitions using the exact RSA implementation command.
    rsa = [entry for entry in entries if entry["file"].endswith("/ext/mbedtls/library/rsa.c")]
    policy.require(len(rsa) == 1)
    rsa = rsa[0]
    status, output = process.run([*compiler_command(rsa), "-dM", "-E", rsa["file"]],
        Path(rsa["directory"]), environment, logs, "crypto-defines", 60)
    policy.require(status == 0)
    crypto = crypto_configuration(output)
    configuration_id = inputs.add(policy.ROOT / "tools/boot/mbedtls_config.h")
    policy.require(all(configuration_id in unit["dependencies"] for unit in units if unit["source"].startswith("mbedtls:")))
    nm = str(inputs.compiler / "bin/xtensa-esp32s3-elf-nm")
    status, output = process.run([nm, "--defined-only", "--print-size", "--format=posix", str(build / "mcuboot_esp32s3.elf")],
                                 work, environment, logs, "retained-functions", 30)
    policy.require(status == 0)
    functions = retained_functions(output)
    # A map LOAD record means supplied to the linker, including discarded sections.
    mapping = bounded(build / "mcuboot_esp32s3.map").decode()
    loaded = []
    for line in mapping.splitlines():
        if line.startswith("LOAD "):
            path = (build / line[5:]).resolve(strict=True)
            if path in objects:
                loaded.append({"source": objects[path], "kind": "direct-object"})
            else:
                identity = inputs.add(path)
                policy.require(path.suffix == ".a" and (identity.startswith("toolchain:") or identity == "generated:hal/libhal.a"))
                loaded.append({"archive": identity, "kind": "archive"})
    policy.require({item["source"] for item in loaded if "source" in item}
                   == {unit["source"] for unit in units if unit["provided_as"] == "direct-object"})
    archives = {item["archive"] for item in loaded if "archive" in item}
    policy.require(len(archives) == 4 and "generated:hal/libhal.a" in archives)
    policy.require({Path(name).name for name in archives} == {"libhal.a", "libm.a", "libgcc.a", "libgcov.a"})
    # Exact seven linker scripts, including ROM address maps; reject untracked INCLUDE.
    ninja_text = bounded(build / "build.ninja").decode()
    values = [line.split(" = ", 1)[1] for line in ninja_text.splitlines() if line.startswith("  LINK_LIBRARIES = ")]
    policy.require(len(values) == 1)
    tokens = shlex.split(values[0])
    scripts = [Path(token[2:]) for token in tokens if token.startswith("-T")]
    policy.require(len(scripts) == 7 and len(set(scripts)) == 7)
    script_ids = []
    for path in scripts:
        script_ids.append(inputs.add(path))
        policy.require(re.search(r"\bINCLUDE\b", bounded(path).decode()) is None)
    header = Path(hal) / "components/esp_common/include/esp_idf_version.h"
    text = bounded(header).decode()
    policy.require(all(re.search(r"^#define ESP_IDF_VERSION_" + name + r"\s+" + value + r"\s*$", text, re.M)
                       for name, value in (("MAJOR", "5"), ("MINOR", "1"), ("PATCH", "6"))))
    inputs.add(header)
    data = {"schema": 1, "scope": "actual-boot-native-build-inputs", "configuration_sha256": report["configuration_sha256"],
        "public_policy_sha256": report["public_policy_sha256"], "elf_sha256": report["elf_sha256"],
        "binary_sha256": report["binary_sha256"], "sources": report["sources"],
        "compiler_archive": report["compiler_archive"], "operator_versions": report["operator_versions"],
        "compiler": report["compiler"], "esp_idf_version": "5.1.6",
        "compiler_options": {"optimization": "Os", "all_verified_gcc": True, "advanced_llvm": False}, "compile_units": sorted(units, key=lambda unit: unit["source"]),
        "files": sorted(inputs.files.values(), key=lambda item: item["id"]), "link_inputs": loaded,
        "linker_scripts": sorted(script_ids), "linker_map_sha256": policy.digest(mapping.encode()),
        "crypto": crypto, "retained_functions": functions,
        "link_input_means_reachable_code": False, "rom_bytes_identified": False,
        "complete_product_sbom": False, "native_advisory_closure": False, "deployment_permitted": False}
    return data
