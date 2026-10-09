# Upstream dependency changes

Nxrs-maintained changes to external dependencies live here, separate from
board configuration and application integration in `platform/`. Source
checkouts remain in `external/`; these packages contain patches and metadata,
not vendored source trees.

| Package | Contents |
| --- | --- |
| `nuttx/patches` | Ordered NuttX kernel and driver patchset |
| `nuttx-apps/patches` | Ordered NuttX-apps patchset |
| [rust-llvm](rust-llvm/README.md) | Pinned compiler patchset, opt-in proposals and local/CI build instructions |
| [rust-std](rust-std/README.md) | Separate opt-in standard-library proposal and pinned inputs |

Applicators and builders stay in `tools/`; qualification tests and measured
evidence stay in `tests/`. Builds apply dependency changes only to private
archived copies, recording revisions and patch hashes. An optional proposal
does not become part of the default build merely by living here.

The [patchset and provenance guide](../docs/upstream-patchsets.md) records the
series, their origins, and the version-specific Rust std/libc patch generators.
