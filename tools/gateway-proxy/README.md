# Fixed native camera proxy qualification

The default Ubuntu 24.04 nginx package still has active source-package advisory
matches after refresh. This guarded candidate builds nginx 1.30.6 against static
PCRE2 10.49, the publishers' fixed stable releases. It does not replace the host
nginx installation, start a system service or change trust, routes or firewall
policy. Its exclusively created public prefix is
`/usr/lib/nxrs-gateway-proxy-qualification`. Compilation, keys, archives, native
output and synthetic TLS fixture material remain in a private disposable work
directory; only a bounded JSON receipt is uploaded.

Both exact archives, detached signatures and public keys have reviewed SHA-256
pins. GPG verifies each actual signature against its publisher's pinned primary
fingerprint before extraction or compilation. Both actual altered archives must
fail signature verification. The public-key initial review is grounded in the
[nginx key publication](https://nginx.org/en/pgp_keys.html) and
[PCRE2 signing policy](https://pcre2project.github.io/pcre2/project/security/).
The verifier uses an isolated GPG home and explicit read-only keyrings; it never
imports keys into system or personal trust stores. Bootstrap, key revocation and
rotation, complete release provenance and production permission remain open.

A fresh complete [nginx advisory feed](https://nginx.org/en/security_advisories.html)
and the complete current PCRE2 repository advisory page must match the reviewed,
expiring metadata in `security/gateway-proxy-dependencies.json`. Added, removed,
changed, incomplete or expired reviews refuse. Stable-branch fixes are evaluated
alongside affected version ranges. PCRE2 requires each publisher-declared fixed
version; unknown or absent fixes refuse. Metadata-only nginx 1.30.5 and PCRE2
10.48 controls must fail. No affected historical proxy is installed or executed.

The universal nginx CVE-2009-4487 entry (vendor severity `none`) is retained as
unfixed by version. Qualification requires the existing generated request/error
log-discard policy, two changed-log-policy refusals and live owned-process file
descriptor observations around the full shared HTTPS boundary suite. This is an
executed configuration assessment, not a silent exception or a general nginx
logging claim. Unknown further applicable advisories refuse qualification.

The static library linked by nginx also builds an independent ordinary regex
probe: exact PCRE2 version, disabled JIT, accepted route and rejected route forms.
The proxy preserves the generated route/method, body, header and TLS restrictions.
Unused optional HTTP modules, HTTP/2, HTTP/3, stream/mail, compression and file
cache are excluded. FastCGI/uWSGI/SCGI remain compiled because the shared deployed
configuration supplies their private startup temporary directories; no route
admits those protocols. The executed module and dynamic-library inventories are
retained. Dynamic unreviewed PCRE is refused.

The minimized native collector uses this actual qualified proxy, validates its
source/receipt/library joins, and classifies its image as pinned source rather
than an Ubuntu package. It still inventories and queries every remaining actual
Ubuntu package. In particular, cryptsetup's SELinux dependency also loads the
Ubuntu PCRE2 library: qualifying the proxy does not remove that separate package
or its findings. The broader stock-profile audit remains independent. The
separate Ubuntu payload receipt authenticates only its distribution subset.

A scoped proxy pass does not close FFmpeg/x264, retained distribution findings,
all native advisories, kernel/firmware, physical hardware, FIPS or deployment.
Generated services now require the separate [operator-approved artifact launcher](../../gateway/proxy/README.md). Qualification alone neither approves its review bundle nor installs or starts any production service.

Offline controls:

```sh
python3 -m unittest discover -s tests/host -p test_gateway_proxy_advisories.py -v
python3 -m unittest discover -s tests/host -p test_gateway_proxy.py -v
```

The actual builder refuses personal hosts before files, subprocesses, package
operations or network. The named hosted workflow runs it with a fresh `--work`
directory, then the native candidate repeats qualification after its own package
refresh. No hardware or FIPS inference is made from these hosted checks.
