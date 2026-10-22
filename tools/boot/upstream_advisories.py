"""Complete, current official boot-library advisory feeds; no empty-feed verdicts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import socket
import ssl
import time
import urllib.request
import urllib.error

import target_plan as policy

GITHUB = {"mcuboot": "mcu-tools/mcuboot", "esp-idf": "espressif/esp-idf"}
MBED = "https://mbed-tls.readthedocs.io/en/latest/security-advisories/"


class FeedFailure(ValueError):
    def __init__(self, source, kind, status=0, exhausted=False):
        super().__init__("official advisory transport refused")
        self.source, self.kind = source, kind
        self.status, self.exhausted = status, exhausted


def feed_source(url):
    for component, repository in GITHUB.items():
        if url == "https://api.github.com/repos/" + repository + "/security-advisories?per_page=100":
            return "github-" + component
    if url == MBED:
        return "mbedtls-index"
    if re.fullmatch(re.escape(MBED) + r"(?:mbedtls|polarssl)-security-advisory-[a-z0-9-]+/", url):
        return "mbedtls-article"
    return "unknown"


def public_failure(error):
    # Fixed fields only: never response bodies, URLs, exception messages or paths.
    sources = {"github-mcuboot", "github-esp-idf", "mbedtls-index", "mbedtls-article", "unknown"}
    kinds = {"http", "tls-certificate", "tls", "timeout", "dns", "network"}
    if (isinstance(error, FeedFailure) and error.source in sources and error.kind in kinds
            and type(error.status) is int and 0 <= error.status <= 599
            and type(error.exhausted) is bool):
        return ("Official native advisory refusal: source=" + error.source + " kind=" + error.kind
                + " status=" + str(error.status) + " exhausted=" + str(int(error.exhausted)))
    return "Official native advisory refusal: source=unknown kind=policy status=0 exhausted=0"


def public_diagnostic(text):
    pattern = (r"^Official native advisory refusal: source=(?:github-mcuboot|github-esp-idf|mbedtls-index|mbedtls-article|unknown) "
               r"kind=(?:http|tls-certificate|tls|timeout|dns|network|policy) status=(?:0|[1-5][0-9]{2}) exhausted=[01]$")
    matches = re.findall(pattern, text, re.M)
    return matches[-1] if matches else None


class Article(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.parts = []
        self.count = 0

    def handle_starttag(self, tag, attributes):
        if not self.stack and tag == "div" and dict(attributes).get("itemprop") == "articleBody":
            self.count += 1
            self.stack.append(tag)
        elif self.stack and tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack:
            policy.require(self.stack[-1] == tag)
            self.stack.pop()

    def handle_data(self, data):
        if self.stack:
            self.parts.append(data)

    def text(self):
        policy.require(self.count == 1 and not self.stack)
        text = " ".join(" ".join(self.parts).split())
        policy.require(100 < len(text) < 256 * 1024)
        return text


class AdvisoryLinks(Article):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attributes):
        super().handle_starttag(tag, attributes)
        if self.stack and tag == "a":
            href = dict(attributes).get("href", "")
            if href.startswith("#"):
                return
            # All main-index links must be recognized. Silently ignoring a new
            # slug style or external link could otherwise hide a new advisory.
            policy.require(re.fullmatch(r"(?:mbedtls|polarssl)-security-advisory-[a-z0-9-]+/", href) is not None)
            self.links.add(href[:-1])


def github_projection(record, repository):
    policy.require(isinstance(record, dict) and record.get("state") == "published")
    identity = record["ghsa_id"]
    policy.require(re.fullmatch(r"GHSA-[23456789cfghjmpqrvwx]{4}(?:-[23456789cfghjmpqrvwx]{4}){2}", identity) is not None)
    policy.require(record["html_url"] == "https://github.com/" + repository + "/security/advisories/" + identity)
    fields = ("ghsa_id", "cve_id", "html_url", "summary", "description", "severity", "published_at",
              "updated_at", "withdrawn_at", "vulnerabilities", "cwes")
    projection = {key: record[key] for key in fields}
    policy.require(isinstance(projection["vulnerabilities"], list) and projection["vulnerabilities"])
    return {"id": identity, "url": projection["html_url"], "sha256": policy.digest(policy.canonical(projection))}


def mbed_projection(slug, raw):
    parser = Article()
    parser.feed(raw.decode("utf-8"))
    return {"id": slug, "url": MBED + slug + "/", "sha256": policy.digest(parser.text().encode("utf-8"))}


def fetch(url, deadline, maximum):
    policy.require(time.monotonic() < deadline)
    request = urllib.request.Request(url, headers={"User-Agent": "nxrs-native-advisory-assessment", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            policy.require(response.status == 200 and response.geturl() == url)
            raw = response.read(maximum + 1)
            policy.require(len(raw) <= maximum and time.monotonic() < deadline)
            # Never mistake a first page for complete coverage.
            policy.require('rel="next"' not in response.headers.get("Link", ""))
            return raw
    except urllib.error.HTTPError as error:
        exhausted = error.code == 429 or (error.headers or {}).get("X-RateLimit-Remaining") == "0"
        status = error.code
        error.close()
        raise FeedFailure(feed_source(url), "http", status, exhausted) from None
    except (urllib.error.URLError, OSError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        kind = ("tls-certificate" if isinstance(reason, ssl.SSLCertVerificationError) else
                "tls" if isinstance(reason, ssl.SSLError) else
                "timeout" if isinstance(reason, TimeoutError) else
                "dns" if isinstance(reason, socket.gaierror) else "network")
        raise FeedFailure(feed_source(url), kind) from None


def capture():
    deadline = time.monotonic() + 180
    result = {}
    for component, repository in GITHUB.items():
        url = "https://api.github.com/repos/" + repository + "/security-advisories?per_page=100"
        records = json.loads(fetch(url, deadline, 4 * 1024 * 1024))
        policy.require(isinstance(records, list) and 0 < len(records) < 100)
        projected = [github_projection(record, repository) for record in records]
        policy.require(len({record["id"] for record in projected}) == len(projected))
        result[component] = sorted(projected, key=lambda item: item["id"])
    index = fetch(MBED, deadline, 1024 * 1024)
    links = AdvisoryLinks()
    links.feed(index.decode("utf-8"))
    links.text()
    policy.require(0 < len(links.links) <= 256)
    slugs = sorted(links.links)

    def read(slug):
        return mbed_projection(slug, fetch(MBED + slug + "/", deadline, 256 * 1024))

    with ThreadPoolExecutor(max_workers=4) as executor:
        result["mbedtls"] = list(executor.map(read, slugs))
    return {"schema": 1, "sources": result, "checked_at": datetime.now(timezone.utc).isoformat(),
            "complete_product_coverage": False, "deployment_permitted": False}


def assess(captured, inventory, catalogue):
    policy.require(captured["schema"] == catalogue["schema"] == 1)
    now = datetime.now(timezone.utc)
    checked = datetime.fromisoformat(captured["checked_at"])
    expires = datetime.fromisoformat(catalogue["valid_until"])
    policy.require(checked.tzinfo is not None and expires.tzinfo is not None
                   and 0 <= (now - checked).total_seconds() <= 3600 and now < expires)
    policy.require(inventory["sources"] == catalogue["sources"])
    policy.require(inventory["crypto"]["modules"] == catalogue["crypto_modules"]
                   and inventory["retained_functions"]["mbedtls_functions"] == catalogue["mbedtls_functions"])
    policy.require(inventory["compiler_options"] == {"optimization": "Os", "all_verified_gcc": True, "advanced_llvm": False}
                   and inventory["esp_idf_version"] == "5.1.6")
    policy.require(inventory["compiler_archive"] in catalogue["compiler_archives"]
                   and inventory["compiler"] == catalogue["compiler_identity"])
    # This exact boot-only proof cannot be reused for NuttX/application TLS or radio.
    sdk_sources = sorted(unit["source"] for unit in inventory["compile_units"])
    policy.require(sdk_sources == catalogue["compile_sources"])
    policy.require(inventory["crypto"]["version"] == "3.6.7" and not inventory["crypto"]["pkcs1_v15"])
    findings = []
    policy.require(set(captured["sources"]) == set(catalogue["advisories"]) == {"mcuboot", "mbedtls", "esp-idf"})
    for component, records in captured["sources"].items():
        current = {record["id"]: record for record in records}
        reviewed = {record["id"]: record for record in catalogue["advisories"][component]}
        policy.require(current and len(current) == len(records) and len(reviewed) == len(catalogue["advisories"][component])
                       and set(current) == set(reviewed))
        for identity, record in current.items():
            entry = reviewed[identity]
            policy.require(record == {key: entry[key] for key in ("id", "url", "sha256")}
                           and entry["assessment"] in ("fixed-in-selected-source", "excluded-by-build", "compiler-options-and-public-operations")
                           and 20 <= len(entry["reason"]) <= 512)
            findings.append({"component": component, "id": identity, "assessment": entry["assessment"], "url": record["url"]})
    return {"schema": 1, "scope": "exact-minimal-boot-source-configuration-and-symbols", "checked_at": captured["checked_at"],
            "reviewed_official_advisories": findings, "unreviewed_advisories": 0,
            "catalogue_sha256": policy.digest(policy.canonical(catalogue)),
            "sources_sha256": policy.digest(policy.canonical(captured["sources"])),
            "native_advisory_closure": False, "complete_product_coverage": False,
            "uncovered": ["physical-ROM", "compiler-security-lifecycle", "vendor-EOL-support", "NuttX-and-application", "other-product-native-components"],
            "deployment_permitted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = capture()
        with args.out.open("xb") as stream:
            stream.write(policy.canonical(data) + b"\n")
        print("Complete official boot advisory feeds captured; no clean-audit claim is implied")
    except Exception as error:
        print(public_failure(error), file=sys.stderr)
        print("Official native advisory capture failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
