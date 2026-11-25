# PPP dependency

The workspace uses the published ppproto 0.2.1 library under its original
MIT and Apache-2.0 licenses. The vendored Cargo manifest selects heapless
0.9.4, which fixes the soundness advisories affecting the 0.8 release series.
The local PPPoS patch exposes LCP Echo-Request and Echo-Reply through the
existing framing engine. The cellular HAL owns the probe interval and timeout.
The archive and file hashes are in [source.json](source.json).

[Upstream ppproto](https://github.com/embassy-rs/ppproto) ·
[RustSec advisory](https://rustsec.org/advisories/RUSTSEC-2026-0580.html)
