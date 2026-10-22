# Combined strict ingest/WireGuard reference

The `Monitoring combined strict VPN deployment` workflow closes a gap left by
separate ingest, service-supervision and VPN tests. Its existence is not evidence
that a revision passed: use the matching commit and Actions run in PR #21.

The hosted-only harness runs the actual generated strict control, MediaMTX,
nginx and ingest units together. Each joins the same new network namespace;
a second namespace represents an external phone network. A real `wg-quick`
service owns the server tunnel, with real kernel WireGuard, per-peer keys,
the generated nftables policy, and a narrowly routed peer. No network dependency
is replaced with a no-op service. Original sandbox/resource directives remain
active. Only unit names, file paths and test namespace placement are adapted.

The actual Rust `service/stream` implementation serves generated JPEG images to
one ingest process, its FFmpeg encoder and a separate SDK publisher. Two continuously drained WebRTC
receivers operate in the peer namespace through verified HTTPS/WHEP and the
VPN media path. These are Python receivers, not an iPhone. The firmware HTTP
server source is real; the image source and native owner loop are test fixtures.

Required assertions include actual advancing H.264 video for both viewers;
one admitted camera connection, one encoder and one publisher; non-root service processes
without effective capabilities; an authenticated WireGuard handshake; selective
application revocation while the other viewer continues; abrupt controller
termination stopping proxy/media/ingest and releasing the source while the VPN
remains active; preserved identity denial after explicit group recovery;
actual VPN-service shutdown removing its interface and stopping every video
service/encoder; recovery through the same peer; and WHEP DELETE followed by
404 for the retired session.

The harness refuses non-GitHub-hosted execution. It uses uniquely named runtime
units/accounts/directories and two disposable namespaces. It never applies
firewall rules to the host namespace, accesses a physical sensor, modifies a
home router, installs persistent units or changes global certificate trust.
Keys, credentials, generated image files and raw service logs remain in private
fixture storage and are removed. Output is limited to fixed assertions, state
categories and frame counters. Cleanup attempts all owned resources even after
an earlier cleanup error; cleanup failure fails the job.

`gateway/tests/test_combined_reference.py` exercises the refusal guard, unit
adaptation, bounded receiver commands and cleanup policy without privilege or
media dependencies. The runtime scripts require the workflow's Linux/systemd,
Python 3.12, kernel networking and media dependencies and are not an installation
or user-run diagnostic tool.

## What remains separate

A pass does not establish physical sensor-to-iPhone/cellular behavior, real
Internet NAT or carrier-grade NAT traversal, App Store distribution, intended-
host cold boot, device power loss, worst-case latency or long-duration stability.
Camera HTTP remains plaintext on a trusted isolated LAN; the phone VPN does not
secure that separate link. FFmpeg's publisher credential is still visible to
trusted local process observers. Account pairing, measured source-health,
secure ingest and release/security/licensing reviews remain outstanding.

The existing component tests remain independent: this workflow neither replaces
their negative protocol/security checks nor weakens their acceptance criteria.
No camera ownership, HAL, firmware, iOS or runtime code change is required by
this qualification harness.
