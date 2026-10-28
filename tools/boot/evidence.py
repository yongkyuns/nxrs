"""Artifact-bound native boot input SBOM and unsigned build records."""
import json
from pathlib import Path
import uuid

import native_inputs
import qualify as process
import target_plan as policy
import upstream_advisories

NAMES = ("native-inputs.json", "native-advisories.json", "boot.sbom.cdx.json", "boot.provenance.json")


def property_(name, value):
    return {"name": "nxrs:" + name, "value": str(value).lower() if isinstance(value, bool) else str(value)}


def hashes(digest):
    return [{"alg": "SHA-256", "content": digest}]


def sbom(report, inventory, advisory):
    components, dependencies = [], []
    source_files = {name: [] for name in ("mcuboot", "mbedtls", "esp-idf")}
    for item in inventory["files"]:
        components.append({"type": "file", "name": item["id"], "bom-ref": item["id"], "hashes": hashes(item["sha256"]),
            "properties": [property_("file-bytes", item["bytes"]), property_("scope", "actual-build-input")]})
        prefix = item["id"].split(":", 1)[0]
        if prefix in source_files:
            source_files[prefix].append(item["id"])
    for name, entry in inventory["sources"].items():
        name = "esp-idf" if name == "hal" else name
        reference = "component:" + name
        components.append({"type": "library", "name": name, "version": inventory["esp_idf_version"] if name == "esp-idf" else entry["version"], "bom-ref": reference,
            "hashes": hashes(entry["sha256"]),
            "externalReferences": [{"type": "distribution", "url": entry["url"]}],
            "properties": [property_("source-commit", entry["commit"]), property_("scope", "pinned-source-archive-with-recorded-build-patches")]})
        dependencies.append({"ref": reference, "dependsOn": sorted(source_files[name])})
    hal_objects = []
    for unit in inventory["compile_units"]:
        reference = "object:" + unit["source"]
        components.append({"type": "file", "name": reference, "bom-ref": reference, "hashes": hashes(unit["object_sha256"]),
            "properties": [property_("scope", "compiled-object-input"), property_("provided-as", unit["provided_as"]),
                           property_("compiler-argv-sha256", unit["argv_sha256"])]})
        dependencies.append({"ref": reference, "dependsOn": sorted(unit["dependencies"])})
        if unit["provided_as"] == "hal-archive":
            hal_objects.append(reference)
    dependencies.append({"ref": "generated:hal/libhal.a", "dependsOn": sorted(hal_objects)})
    # Address maps identify the SDK ABI, never the actual silicon ROM bytes.
    components.append({"type": "firmware", "name": "ESP32-S3 mask ROM", "bom-ref": "opaque:physical-rom",
        "properties": [property_("physical-identity-known", False), property_("bytes-in-inventory", False)]})
    rust = inventory.get("rust_build")
    if rust is not None:
        refs = []
        for item in rust["components"]:
            ref = "rust-component:" + item["name"]
            refs.append(ref)
            components.append({"type": "library", "name": item["name"], "version": item["version"], "bom-ref": ref,
                "properties": [property_("scope", "actual-compiled-rust-source-inputs"),
                               property_("intermediate-artifact-sha256", item["artifact_sha256"]),
                               property_("stdlib-distribution", "1.90.0.0"), property_("advisory-closure", False)]})
            dependencies.append({"ref": ref, "dependsOn": item["sources"]})
        dependencies.append({"ref": rust["archive"], "dependsOn": sorted(refs)})
    root_ref = "artifact:mcuboot_esp32s3.bin"
    root = {"type": "firmware", "name": "nxrs-esp32s3-bootloader", "version": report["sources"]["mcuboot"]["version"],
        "bom-ref": root_ref, "hashes": hashes(report["binary_sha256"]),
        "properties": [property_("scope", "boot-cross-build-inputs-only"), property_("complete-product-sbom", False),
                       property_("physical-qualified", False), property_("deployment-permitted", False)]}
    root_dependencies = {"component:mcuboot", "component:mbedtls", "component:esp-idf", "opaque:physical-rom"}
    for item in inventory["link_inputs"]:
        root_dependencies.add(item["archive"] if "archive" in item else "object:" + item["source"])
    root_dependencies.update(inventory["linker_scripts"])
    dependencies.append({"ref": root_ref, "dependsOn": sorted(root_dependencies)})
    tools = [{"type": "application", "name": "xtensa-esp-elf-gcc", "version": report["compiler"],
              "hashes": hashes(report["compiler_archive"]["sha256"]),
              "externalReferences": [{"type": "distribution", "url": report["compiler_archive"]["url"]}]}]
    if rust is not None:
        tools.append({"type": "application", "name": "Espressif rustc", "version": "1.90.0.0",
            "hashes": hashes(rust["compiler_archive"]["sha256"]),
            "externalReferences": [{"type": "distribution", "url": rust["compiler_archive"]["url"]}]})
        tools.append({"type": "application", "name": "Cargo", "version": "1.98.1", "hashes": hashes(rust["cargo_sha256"])})
    runtime = inventory["operator_runtime"]
    tools.append({"type": "application", "name": "CPython", "version": runtime["version"],
                  "hashes": hashes(runtime["executable_sha256"]),
                  "properties": [property_("stdlib-ssl", runtime["stdlib_ssl"]),
                                 property_("stdlib-ssl-module", policy.canonical(runtime["stdlib_ssl_module"]).decode()),
                                 property_("cryptography-ssl", runtime["cryptography_ssl"]),
                                 property_("cryptography-native-modules", policy.canonical(runtime["cryptography_native_modules"]).decode()),
                                 property_("complete-os-library-inventory", False)]})
    for name, version in sorted(report["operator_versions"].items()):
        tools.append({"type": "application", "name": name, "version": version, "purl": "pkg:pypi/" + name + "@" + version})
    bom = {"$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json", "bomFormat": "CycloneDX", "specVersion": "1.6",
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:sha256:" + report["binary_sha256"])),
        "version": 1, "metadata": {"timestamp": advisory["checked_at"], "lifecycles": [{"phase": "build"}],
                                  "component": root, "tools": {"components": tools}},
        "components": sorted(components, key=lambda item: item["bom-ref"]),
        "dependencies": sorted(dependencies, key=lambda item: item["ref"]),
        "properties": [property_("compiled-units-are-reachability-proof", False),
                       property_("native-advisory-closure", False), property_("trusted-release-attestation", False)]}
    references = {root_ref, *(item["bom-ref"] for item in components)}
    policy.require(len(references) == len(components) + 1)
    policy.require(all(item["ref"] in references and set(item["dependsOn"]) <= references for item in dependencies))
    return bom


def provenance(report, inventory_digest, advisory_digest, bom_digest):
    return {"_type": "https://in-toto.io/Statement/v1", "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [{"name": "mcuboot_esp32s3.bin", "digest": {"sha256": report["binary_sha256"]}},
                    {"name": "mcuboot_esp32s3.elf", "digest": {"sha256": report["elf_sha256"]}}],
        "predicate": {"buildDefinition": {"buildType": "https://github.com/yongkyuns/nxrs/boot-cross-build/v1",
            "externalParameters": {"platform": report["platform"], "public_policy_sha256": report["public_policy_sha256"],
                "configuration_sha256": report["configuration_sha256"], "scope": "cross-build-only"},
            "internalParameters": {"repository_head": report["repository_head"], "repository_has_changes": report["repository_has_changes"],
                "recipe_inputs": report["inputs"], "physical_qualified": False, "deployment_permitted": False},
            "resolvedDependencies": [{"uri": entry["url"], "digest": {"sha256": entry["sha256"]}} for _, entry in sorted(report["sources"].items())]
                + ([{"uri": report["rust_build"][name]["url"], "digest": {"sha256": report["rust_build"][name]["sha256"]}}
                    for name in ("compiler_archive", "library_archive")] if "rust_build" in report else [])
                + [{"uri": report["compiler_archive"]["url"], "digest": {"sha256": report["compiler_archive"]["sha256"]}},
                   {"uri": "native-inputs.json", "digest": {"sha256": inventory_digest}},
                   {"uri": "native-advisories.json", "digest": {"sha256": advisory_digest}},
                   {"uri": "boot.sbom.cdx.json", "digest": {"sha256": bom_digest}}]},
            "runDetails": {"builder": {"id": "urn:nxrs:builder:boot-qualification:v1"},
                           "metadata": {"invocationId": "urn:sha256:" + inventory_digest,
                                        "trustedAttestation": False, "slsaLevelClaimed": False}}}}


def encoded(value):
    return policy.canonical(value) + b"\n"


def schema_documents(work, spec):
    directory = work / "cyclonedx-schemas"
    directory.mkdir(mode=0o700)
    documents = {}
    for entry in spec["cyclonedx_schemas"]:
        path = process.fetch(dict(entry, filename=entry["name"]), directory)
        documents[entry["name"]] = json.loads(native_inputs.bounded(path, 1024 * 1024))
    return documents


def validate_schema(bom, documents):
    from jsonschema.validators import validator_for
    from jsonschema import FormatChecker
    from referencing import Registry, Resource
    registry = Registry().with_resources((value["$id"], Resource.from_contents(value)) for value in documents.values())
    schema = documents["bom-1.6.schema.json"]
    validator = validator_for(schema)
    validator.check_schema(schema)
    validator(schema, registry=registry, format_checker=FormatChecker()).validate(bom)


def bundle(report, inventory, advisory):
    bom = sbom(report, inventory, advisory)
    values = {"native-inputs.json": inventory, "native-advisories.json": advisory, "boot.sbom.cdx.json": bom}
    prov = provenance(report, *(policy.digest(encoded(values[name])) for name in NAMES[:3]))
    values["boot.provenance.json"] = prov
    return values


def write(work, report, inventory, captured, documents):
    catalogue = json.loads((policy.ROOT / "security/boot-advisory-review.json").read_text())
    advisory = upstream_advisories.assess(captured, inventory, catalogue)
    values = bundle(report, inventory, advisory)
    validate_schema(values["boot.sbom.cdx.json"], documents)
    for name, value in values.items():
        with (work / name).open("xb") as stream:
            stream.write(encoded(value))
    return {name: policy.digest(encoded(value)) for name, value in values.items()}


def verify(work, report, source, hal, compiler, environment, captured, documents):
    # Independent current artifacts/inputs, not self-consistent bundle hashes alone.
    policy.require(process.digest(work / "build/mcuboot_esp32s3.bin") == report["binary_sha256"]
                   and process.digest(work / "build/mcuboot_esp32s3.elf") == report["elf_sha256"])
    current = native_inputs.collect(work, source, hal, compiler, environment, report)
    policy.require(current["operator_runtime"] == report["operator_runtime"])
    catalogue = json.loads((policy.ROOT / "security/boot-advisory-review.json").read_text())
    advisory = upstream_advisories.assess(captured, current, catalogue)
    expected = bundle(report, current, advisory)
    for name, value in expected.items():
        policy.require(native_inputs.bounded(work / name) == encoded(value))
    validate_schema(expected["boot.sbom.cdx.json"], documents)
    return {name: policy.digest(encoded(value)) for name, value in expected.items()}


def exercise_refusals(work, report, source, hal, compiler, environment, captured, documents):
    """Real artifact/bundle mutations; private files are restored before success."""
    import copy
    originals = {name: native_inputs.bounded(work / name) for name in NAMES}
    original_values = {name: json.loads(raw) for name, raw in originals.items()}
    verify(work, report, source, hal, compiler, environment, captured, documents)
    labels = []

    def coherent_inventory(change):
        data = copy.deepcopy(original_values["native-inputs.json"])
        change(data)
        return bundle(report, data, original_values["native-advisories.json"])

    def omit_unit(data):
        removed = data["compile_units"].pop(0)
        data["link_inputs"] = [item for item in data["link_inputs"] if item.get("source") != removed["source"]]

    def forged_advisory():
        data = copy.deepcopy(original_values["native-advisories.json"])
        data["native_advisory_closure"] = True
        return bundle(report, original_values["native-inputs.json"], data)

    def forged_bom(change):
        values = copy.deepcopy(original_values)
        change(values["boot.sbom.cdx.json"])
        values["boot.provenance.json"] = provenance(report, *(policy.digest(encoded(values[name])) for name in NAMES[:3]))
        return values

    def claim_rom(bom):
        component = next(item for item in bom["components"] if item["bom-ref"] == "opaque:physical-rom")
        next(item for item in component["properties"] if item["name"] == "nxrs:physical-identity-known")["value"] = "true"

    cases = (
        ("coherent-sdk-version", lambda: coherent_inventory(lambda data: data["sources"]["mbedtls"].update(version="0.0.0"))),
        ("coherent-compile-unit-removal", lambda: coherent_inventory(omit_unit)),
        ("coherent-header-digest", lambda: coherent_inventory(lambda data: data["files"][0].update(sha256="0" * 64))),
        ("coherent-compiler-identity", lambda: coherent_inventory(lambda data: data["compiler_archive"].update(sha256="0" * 64))),
        ("coherent-coverage-claim", forged_advisory),
        ("coherent-opaque-rom-claim", lambda: forged_bom(claim_rom)),
        ("coherent-sbom-subject", lambda: forged_bom(lambda bom: bom["metadata"]["component"].update(hashes=hashes("0" * 64)))),
    )
    if report.get("rust_build") is not None:
        cases += (
            ("coherent-rust-component-removal", lambda: coherent_inventory(lambda data: data["rust_build"]["components"].pop(0))),
            ("coherent-rust-compiler-substitution", lambda: coherent_inventory(lambda data: data["rust_build"]["compiler_archive"].update(sha256="0" * 64))),
            ("coherent-rust-coverage-claim", lambda: coherent_inventory(lambda data: data["rust_build"].update(rust_advisory_closure=True))),
        )
    try:
        for label, create in cases:
            values = create()
            # These are structurally valid coherent bundles, not just malformed JSON.
            validate_schema(values["boot.sbom.cdx.json"], documents)
            for name, value in values.items():
                (work / name).write_bytes(encoded(value))
            try:
                verify(work, report, source, hal, compiler, environment, captured, documents)
            except ValueError:
                labels.append(label)
            else:
                raise ValueError("coherent native evidence mutation was admitted")
            finally:
                for name, raw in originals.items():
                    (work / name).write_bytes(raw)
        image = work / "build/mcuboot_esp32s3.bin"
        raw = native_inputs.bounded(image)
        try:
            image.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            try:
                verify(work, report, source, hal, compiler, environment, captured, documents)
            except ValueError:
                labels.append("changed-native-artifact")
            else:
                raise ValueError("changed native artifact was admitted")
        finally:
            image.write_bytes(raw)
    finally:
        for name, raw in originals.items():
            (work / name).write_bytes(raw)
    if report.get("rust_build") is not None:
        roots = native_inputs.Inputs(work, source, hal, compiler).roots
        core = next(entry for entry in report["rust_build"]["files"]
                    if entry["id"].startswith("rust-sdk:") and entry["id"].endswith("/core/src/lib.rs"))
        prefix, relative = core["id"].split(":", 1)
        paths = (("changed-rust-stdlib-source", roots[prefix] / relative),
                 ("changed-rust-linked-archive", work / "build/boot-policy/libnxrs_boot_policy.a"))
        for label, path in paths:
            original = native_inputs.bounded(path, 64 * 1024 * 1024)
            try:
                path.write_bytes(original + b"\n")
                try:
                    verify(work, report, source, hal, compiler, environment, captured, documents)
                except ValueError:
                    labels.append(label)
                else:
                    raise ValueError("changed Rust input was admitted")
            finally:
                path.write_bytes(original)
    digests = verify(work, report, source, hal, compiler, environment, captured, documents)
    policy.require(len(labels) == (13 if report.get("rust_build") is not None else 8))
    result = {"schema": 1, "scope": "private-boot-build-evidence-controls", "qualified": True,
        "bundle_sha256": digests, "required_refusal_controls": labels,
        "native_advisory_closure": False, "trusted_release_attestation": False,
        "physical_qualified": False, "deployment_permitted": False}
    with (work / "boot-evidence-qualification.json").open("xb") as stream:
        stream.write(encoded(result))
    return result
