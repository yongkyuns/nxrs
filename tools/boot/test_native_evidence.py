"""Adversarial native inventory/feed gates. No network or device operations."""
import copy
from datetime import datetime, timedelta, timezone
import json
import ssl
import socket
import time
import urllib.error
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import evidence
import native_inputs
import target_plan as policy
import upstream_advisories as upstream


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = json.loads((policy.ROOT / "security/boot-advisory-review.json").read_text())
        self.captured = {"schema": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "sources": {
            component: [{key: record[key] for key in ("id", "url", "sha256")} for record in records]
            for component, records in self.catalogue["advisories"].items()}}
        self.inventory = {"sources": self.catalogue["sources"], "crypto": {"modules": self.catalogue["crypto_modules"],
            "version": "3.6.7", "pkcs1_v15": False}, "retained_functions": {"mbedtls_functions": self.catalogue["mbedtls_functions"]},
            "compile_units": [{"source": source} for source in self.catalogue["compile_sources"]],
            "compiler": self.catalogue["compiler_identity"], "compiler_archive": self.catalogue["compiler_archives"][0],
            "esp_idf_version": "5.1.6", "compiler_options": {"optimization": "Os", "all_verified_gcc": True, "advanced_llvm": False}}

    def assess(self):
        return upstream.assess(self.captured, self.inventory, self.catalogue)

    def test_current_review_is_boot_scoped_and_never_closes_native_coverage(self):
        result = self.assess()
        self.assertEqual(len(result["reviewed_official_advisories"]), 107)
        self.assertIs(result["native_advisory_closure"], False)
        self.assertIs(result["complete_product_coverage"], False)
        self.assertIs(result["deployment_permitted"], False)

    def test_new_tee_advisory_is_scoped_to_the_exact_xtensa_boot_inventory(self):
        identity = "GHSA-49x8-xqjg-2pjg"
        entry = next(row for row in self.catalogue["advisories"]["esp-idf"] if row["id"] == identity)
        self.assertEqual(entry["sha256"], "e9381b7999870265ca3b18b39415a752c3d9a0d3bb1566f17bfb78d65f6430a6")
        self.assertEqual(entry["url"], "https://github.com/espressif/esp-idf/security/advisories/" + identity)
        self.assertEqual(entry["assessment"], "excluded-by-build")
        self.assertIn("ESP32-S3 Xtensa boot", entry["reason"])
        self.assertIn("does not qualify NuttX", entry["reason"])
        self.assertEqual(len(self.inventory["compile_units"]), 83)
        self.assertFalse(any("esp_tee" in row["source"].lower() for row in self.inventory["compile_units"]))
        result = self.assess()
        self.assertEqual(sum(row["id"] == identity for row in result["reviewed_official_advisories"]), 1)
        self.assertIs(result["complete_product_coverage"], False)

    def test_tee_review_cannot_hide_a_changed_advisory_or_authorize_a_tee_build(self):
        identity = "GHSA-49x8-xqjg-2pjg"
        captured, inventory = copy.deepcopy(self.captured), copy.deepcopy(self.inventory)
        changes = [
            lambda: self.captured["sources"]["esp-idf"].remove(next(row for row in self.captured["sources"]["esp-idf"] if row["id"] == identity)),
            lambda: next(row for row in self.captured["sources"]["esp-idf"] if row["id"] == identity).update(sha256="0" * 64),
            lambda: self.inventory["compile_units"].append({"source": "esp-idf:components/esp_tee/esp_tee_vectors_plic.S"}),
            lambda: self.inventory.update(compiler="riscv32-esp-elf-gcc 14.2.0"),
            lambda: self.inventory.update(esp_idf_version="6.1"),
        ]
        for change in changes:
            self.captured, self.inventory = copy.deepcopy(captured), copy.deepcopy(inventory)
            change()
            with self.assertRaises(ValueError):
                self.assess()

    def test_new_removed_changed_and_duplicate_advisories_are_refused(self):
        original = copy.deepcopy(self.captured)
        for mutate in (lambda data: data["sources"]["mcuboot"].append({"id": "GHSA-2222-3333-4444", "url": "https://example.invalid", "sha256": "0" * 64}),
                       lambda data: data["sources"]["mbedtls"].pop(),
                       lambda data: data["sources"]["esp-idf"][0].update(sha256="0" * 64),
                       lambda data: data["sources"]["mcuboot"].append(data["sources"]["mcuboot"][0])):
            self.captured = copy.deepcopy(original)
            mutate(self.captured)
            with self.assertRaises(ValueError):
                self.assess()

    def test_stale_future_naive_capture_and_expired_review_are_refused(self):
        for date in (datetime.now(timezone.utc) - timedelta(hours=2),
                     datetime.now(timezone.utc) + timedelta(minutes=1), datetime.now()):
            self.captured["checked_at"] = date.isoformat()
            with self.assertRaises(ValueError):
                self.assess()
        self.captured["checked_at"] = datetime.now(timezone.utc).isoformat()
        self.catalogue["valid_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            self.assess()

    def test_radio_tls_extra_function_and_removed_unit_cannot_reuse_boot_review(self):
        original = copy.deepcopy(self.inventory)
        for mutate in (lambda data: data["crypto"]["modules"].append("MBEDTLS_SSL_TLS_C"),
                       lambda data: data["retained_functions"]["mbedtls_functions"].append("mbedtls_rsa_private"),
                       lambda data: data["compile_units"].append({"source": "esp-idf:components/bt/unreviewed.c"}),
                       lambda data: data["compile_units"].pop(),
                       lambda data: data["sources"]["mbedtls"].update(version="3.6.6")):
            self.inventory = copy.deepcopy(original)
            mutate(self.inventory)
            with self.assertRaises(ValueError):
                self.assess()

    def test_compiler_archive_identity_or_advanced_optimization_change_is_refused(self):
        original = copy.deepcopy(self.inventory)
        for mutate in (lambda data: data.update(compiler="clang 18"),
                       lambda data: data["compiler_archive"].update(sha256="0" * 64),
                       lambda data: data["compiler_options"].update(advanced_llvm=True),
                       lambda data: data.update(esp_idf_version="5.1.3")):
            self.inventory = copy.deepcopy(original)
            mutate(self.inventory)
            with self.assertRaises(ValueError):
                self.assess()

    def test_public_http_failure_reports_exhaustion_without_response_or_url_data(self):
        url = "https://api.github.com/repos/mcu-tools/mcuboot/security-advisories?per_page=100"
        error = urllib.error.HTTPError(url, 403, "private response data", {"X-RateLimit-Remaining": "0"}, None)
        with patch.object(upstream.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(upstream.FeedFailure) as caught:
                upstream.fetch(url, time.monotonic() + 10, 100)
        self.assertEqual(upstream.public_failure(caught.exception),
                         "Official native advisory refusal: source=github-mcuboot kind=http status=403 exhausted=1")

    def test_tls_timeout_dns_and_network_failure_diagnostics_are_fixed_fields(self):
        reasons = ((ssl.SSLCertVerificationError("private path"), "tls-certificate"),
                   (ssl.SSLError("private data"), "tls"), (TimeoutError("private host"), "timeout"),
                   (socket.gaierror("private host"), "dns"), (OSError("private path"), "network"))
        for reason, kind in reasons:
            with patch.object(upstream.urllib.request, "urlopen", side_effect=urllib.error.URLError(reason)):
                with self.assertRaises(upstream.FeedFailure) as caught:
                    upstream.fetch(upstream.MBED, time.monotonic() + 10, 100)
            self.assertEqual(upstream.public_failure(caught.exception),
                             "Official native advisory refusal: source=mbedtls-index kind=" + kind + " status=0 exhausted=0")

    def test_arbitrary_exception_or_malformed_failure_cannot_inject_diagnostics(self):
        generic = "Official native advisory refusal: source=unknown kind=policy status=0 exhausted=0"
        for error in (ValueError("private secret"), upstream.FeedFailure("private-data", "http", 403),
                      upstream.FeedFailure("github-mcuboot", "private-data", 403),
                      upstream.FeedFailure("github-mcuboot", "http", "private-data")):
            self.assertEqual(upstream.public_failure(error), generic)

    def test_only_exact_bounded_advisory_diagnostic_lines_can_be_relayed(self):
        expected = "Official native advisory refusal: source=mbedtls-index kind=http status=429 exhausted=1"
        self.assertEqual(upstream.public_diagnostic("private data\n" + expected + "\nprivate path"), expected)
        for text in ("private data", expected + " private data", expected.replace("429", "999"),
                     expected.replace("mbedtls-index", "/private/data"), expected.replace("http", "private-data")):
            self.assertIsNone(upstream.public_diagnostic(text))

    def test_an_empty_github_response_cannot_claim_success(self):
        with patch.object(upstream, "fetch", return_value=b"[]"):
            with self.assertRaises(ValueError):
                upstream.capture()

    def test_new_mbed_link_style_is_refused_instead_of_hidden(self):
        parser = upstream.AdvisoryLinks()
        parser.feed('<nav><a href="https://untrusted.invalid/">theme</a></nav>')
        self.assertEqual(parser.links, set())
        parser.feed('<div itemprop="articleBody"><a href="mbedtls-security-advisory-2026-07-valid/">official</a></div>')
        self.assertEqual(parser.links, {"mbedtls-security-advisory-2026-07-valid"})
        for href in ("https://untrusted.invalid/advisory/", "../private/", "new-unsupported-advisory/"):
            with self.assertRaises(ValueError):
                upstream.AdvisoryLinks().feed('<div itemprop="articleBody"><a href="' + href + '">new</a></div>')

    def test_article_requires_one_complete_main_body_and_ignores_page_chrome(self):
        content = 'A public vulnerability explanation with sufficient detail. ' * 4
        first = '<div itemprop="articleBody"><h1>Advisory</h1><p>' + content + '</p></div>'
        one = upstream.mbed_projection("public-test", first.encode())
        two = upstream.mbed_projection("public-test", ('<nav>changed theme</nav>' + first + '<footer>changed footer</footer>').encode())
        self.assertEqual(one, two)
        for html in ('<html>access denied</html>', first + first, first[:-6]):
            with self.assertRaises(ValueError):
                upstream.mbed_projection("public-test", html.encode())

    def test_pagination_and_redirect_are_refused_instead_of_truncated(self):
        class Response:
            status = 200
            headers = {"Link": '<https://api.github.com/page2>; rel="next"'}
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def geturl(self): return "https://api.github.com/example"
            def read(self, _): return b"[]"
        import time
        response = Response()
        with patch.object(upstream.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(ValueError):
                upstream.fetch("https://api.github.com/example", time.monotonic() + 10, 100)
            response.headers = {}
            with self.assertRaises(ValueError):
                upstream.fetch("https://api.github.com/other", time.monotonic() + 10, 100)


class InventoryTests(unittest.TestCase):
    def test_provenance_is_stable_across_canonical_report_roundtrip(self):
        source = {"mcuboot": {"url": "https://example.invalid/a", "sha256": "a" * 64},
                  "mbedtls": {"url": "https://example.invalid/b", "sha256": "b" * 64},
                  "hal": {"url": "https://example.invalid/c", "sha256": "c" * 64}}
        report = {"binary_sha256": "1" * 64, "elf_sha256": "2" * 64, "platform": "fixture",
                  "public_policy_sha256": "3" * 64, "configuration_sha256": "4" * 64,
                  "repository_head": "5" * 40, "repository_has_changes": False, "inputs": {},
                  "sources": source, "compiler_archive": {"url": "https://example.invalid/compiler", "sha256": "6" * 64}}
        first = evidence.provenance(report, "7" * 64, "8" * 64, "9" * 64)
        second = evidence.provenance(json.loads(policy.canonical(report)), "7" * 64, "8" * 64, "9" * 64)
        self.assertEqual(first, second)

    def test_dependency_counts_stale_records_and_truncation_are_refused(self):
        good = "object.c.obj: #deps 1, deps mtime 12345 (VALID)\n    /source.c\n\n"
        self.assertEqual(native_inputs.dependency_graph(good), {"object.c.obj": ["/source.c"]})
        for data in (good.replace("VALID", "STALE"), good.replace("#deps 1", "#deps 2"),
                     good + good, "    /orphan.c\n", good.replace("    /source.c\n", "")):
            with self.assertRaises(ValueError):
                native_inputs.dependency_graph(data)

    def test_function_inventory_accepts_rom_constants_but_refuses_secret_protocol_api(self):
        good = "//vendor_absolute A 12345\nboot_go T 1010 20\nmbedtls_rsa_public T 2020 40\n"
        result = native_inputs.retained_functions(good)
        self.assertEqual(result["function_count"], 2)
        for name in ("mbedtls_rsa_private", "mbedtls_ssl_read", "mbedtls_rsa_pkcs1_decrypt", "mbedtls_psa_crypto_init"):
            with self.assertRaises(ValueError):
                native_inputs.retained_functions(good + name + " T 3000 20\n")
        with self.assertRaises(ValueError):
            native_inputs.retained_functions(good + "boot_go T 9999 20\n")

    def test_ssl_identity_distinguishes_builtin_extension_and_unidentified_module(self):
        import _ssl
        from types import SimpleNamespace
        import sys
        # Actual installed runtime can be either a linked builtin or an extension.
        actual = native_inputs.ssl_module_identity("a" * 64)
        self.assertIn(actual["kind"], ("builtin", "extension"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public-native-module"
            path.write_bytes(b"public-module-fixture")
            with patch.object(_ssl, "__file__", str(path), create=True):
                extension = native_inputs.ssl_module_identity("b" * 64)
                self.assertEqual(extension, {"kind": "extension", "sha256": policy.digest(path.read_bytes())})
        with patch.object(_ssl, "__file__", None, create=True), patch.object(_ssl, "__spec__", SimpleNamespace(origin="built-in")), patch.object(sys, "builtin_module_names", ("_ssl",)):
            self.assertEqual(native_inputs.ssl_module_identity("c" * 64), {"kind": "builtin", "container_executable_sha256": "c" * 64})
        with patch.object(_ssl, "__file__", None, create=True), patch.object(_ssl, "__spec__", SimpleNamespace(origin="unknown")):
            with self.assertRaises(ValueError):
                native_inputs.ssl_module_identity("d" * 64)

    def test_unbounded_special_and_symlinked_inputs_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "input"
            p.write_bytes(b"public")
            self.assertEqual(native_inputs.bounded(p), b"public")
            with self.assertRaises(ValueError): native_inputs.bounded(p, 3)
            link = Path(directory) / "link"
            link.symlink_to(p)
            with self.assertRaises(OSError): native_inputs.bounded(link)
            with self.assertRaises(ValueError): native_inputs.bounded(Path(directory))


if __name__ == "__main__":
    unittest.main()
