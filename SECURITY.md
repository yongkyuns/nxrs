# Reporting security issues

Report suspected vulnerabilities privately through [GitHub private vulnerability
reporting](https://github.com/yongkyuns/nxrs/security/advisories/new). If that
repository feature is unavailable, contact the repository owner through their
[GitHub profile](https://github.com/yongkyuns) to establish a private channel;
do not post exploit details, credentials or private footage in a public issue.

Include the affected commit, target/profile, component, prerequisites, impact
and a minimal synthetic reproduction. Remove credentials, personal addresses,
footage and raw device logs. Do not test other people's devices or deployments.
There is no guaranteed response time or supported commercial release at present.

Security fixes target the current main branch. Development and qualification
images are not production security configurations. For deployment boundaries,
release gates and the FIPS decision, see [the threat model](docs/security.md).

The security workflow checks available Git history and both Cargo lockfiles on
pull requests, main pushes and weekly. Secret reports contain locations without values.
If a real credential is detected, revoke or rotate it through a private channel;
removing the current file does not remove it from Git history.

## Credentials and public evidence

Keep real Wi-Fi profiles, private TLS/signing/VPN keys, enrollment and recovery
material outside the checkout with owner-only permissions. Wi-Fi and stream
qualification tools enforce external paths for their private inputs and outputs.
The ignore rules for `.env` files and `/.local/` guard against accidental local
additions; they do not change those tools' requirements or remove tracked files.
Only synthetic examples belong in `.env.example` and `.env.template`.

Publish reviewed, structured measurements using the existing exporters where
available: [event-services reports](tests/event-services-comparison/report.py),
[service qualification](tests/service-qualification/publish.py), and
[detector timings](tests/detection/report_profile.py). Retain source revisions,
hashes and measurements while replacing unnecessary network details and hardware
identifiers with placeholders or stable board labels such as `esp32s3-board-a`.
Review remaining free-text fields and media separately; these exporters and the
secret scanner do not detect every kind of personal information.

Public verification keys and certificates may be legitimate source material;
review their identity metadata before committing them. Keep raw flash backups,
credential-bearing console transcripts and private footage out of public results.

## Dependency checks

Cargo audits fail on vulnerabilities, unsoundness findings and audit errors.
Maintenance warnings remain visible and need follow-up; they are not waived.
There are no advisory ignores. Any future exception needs a reviewed rationale,
owner, tracking issue, expiry and an enforced expiry check before introduction.
The gateway check still runs if the root audit fails. Native dependencies,
release SBOM/provenance and parser qualification remain tracked in #38.
