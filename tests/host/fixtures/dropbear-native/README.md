# Native Dropbear regression witnesses

Run from the repository root:

```sh
python3 -m unittest tests.host.test_dropbear_native -v
```

The runner applies the registered outer NuttX Apps patch and the full ordered
0001–0008 nested Dropbear series used by the firmware build. It compiles the resulting C source
with AddressSanitizer, UndefinedBehaviorSanitizer and warnings as errors. A C
compiler, GNU `patch` and `git` are required; no network or submodule checkout is
needed.

`upstream.json` identifies the pinned NuttX Apps and Dropbear revisions and the
SHA-256 of each upstream input. The readable files under `upstream/src` and `apps`
retain
their upstream license headers and exact original bytes, including trailing
spaces and blank lines; the local Git attribute preserves those hashed snapshots.
Existing NuttX Apps files are reconstructed from
the complete preimages in the outer patch and checked against that manifest
before applying it. The checked-in
`upstream/nuttx-apps/dropbear-source-receipt.json` covers all 16 source/header
postimages and the exact ordered nested patch bytes. Both the host preparation
and the target build verifier check this receipt. This makes a patch or dependency
revision change explicit and rejects successful patch commands that produce the
wrong source.

The unchanged pinned `src/default_options.h` is an auxiliary input in
`upstream.json`'s `support_files` map. Its hash is checked before copying it.
The packet witnesses extract the actual payload limit from that header and
the channel/deferred-queue limits from the prepared `sysoptions.h`, including
its native conditional; the witnesses do not supply independent numeric limits.

The maintained 0005 patch contains contextual hunks based on the actual result
of 0001–0004. Patches 0005–0008 apply in order with `--fuzz=0`. Whitespace in context lines is normalized
only in the patch representation; GNU `patch -l` preserves the original source
bytes, which the full postimage hashes verify. The first four upstream patches
retain their original context/fuzz behavior, and every patch failure is fatal.

Most witnesses include a complete patched translation unit. The channel witness
extracts the complete `newchannel`, `recv_msg_channel_close`, and `remove_channel` definitions
verbatim from the prepared source; it does not duplicate their implementation.
The new packet witnesses also extract complete definitions from `packet.c`,
`common-session.c`, `ecdsa.c` and both channel-open handlers. The headers in
`include` and dependency functions in each witness provide the
small ABI surface necessary to observe ownership and inject failures.

The suite checks the real foreground command and socket lifecycle, singleton
generation races, bounded stop waits, preallocated channel admission, child and
waiter retirement, password rejection, descriptor cleanup after crypto errors,
allocation epochs, qualified entropy failures and random-state cleanup. SHA
operations in the random-state witness are observation stubs; they do not claim
to validate a cryptographic implementation. LeakSanitizer is disabled because
the test environment runs under ptrace; explicit ownership and release counts
cover the injected failure paths.

`misplaced-zero-context.patch` is a deliberately broken historical input used
only by a negative control. Applied after the full prerequisite series, it
returns success but displaces cancellation into a block comment. The test
compiles that actual resulting `remove_channel` function and requires the
cancellation-before-retirement assertion to fail. The corrected source passes
the same witness. This guards against repeating the former incomplete-series
test setup, which missed an offset introduced by patch 0004.

## Focused upstream backports

The nested base remains `54ef47adf8c99b422be6a8f694f2866e62f88b9e`.
These three ordered patches retain a direct upstream source reference:

| Nested patch | Upstream change | Coverage |
| --- | --- | --- |
| `0006-limit-kex-deferred-replies.patch` | [d35cf537](https://github.com/mkj/dropbear/commit/d35cf53782040ab11ea38369244cdbe92fc85b9a) | Deferred-reply admission, drain, session initialization and reuse. |
| `0007-validate-ecdsa-curve-identifier.patch` | [cfedf988](https://github.com/mkj/dropbear/commit/cfedf9889222e44b2a31ecc14a289b7d0d86c563) | Short/mismatched identifiers, valid P-256 identifier and exact-string rejection. |
| `0008-enforce-channel-packet-bounds.patch` | [59870ad4](https://github.com/mkj/dropbear/commit/59870ad43153fe8d4f1c96f5d5752116c94f31ff) | Zero, minimum, ordinary, maximum and oversized peer packet sizes in both channel-open paths. |

Patch 0006 sets `MAX_DEFER_REPLY_QUEUE` to one for `DROPBEAR_NUTTX` while
retaining upstream's `>` guard. The actual native queue therefore admits two
entries and refuses the third before allocating. `MAX_CHANNELS` remains 1000;
the existing native channel-admission guard independently allows one interactive
channel. Other platforms retain upstream's queue threshold of `MAX_CHANNELS`.

For each backport, an explicit negative-control preparation verifies all input
and patch hashes but omits that one patch and disables the expected postimage
check. The runner compiles the resulting historical function definitions with
the corrected profile's actual limits. Seven independent controls must fail:
allocation beyond the reply bound, missing drain reset, missing session reset,
the ECDSA helper's short-allocation over-read under ASan, and zero packet sizes
in each channel-open path plus an oversized open-confirmation packet size.
The corrected source passes the same behavioral witnesses. Historical incoming
channel-open already capped oversized packets; that behavior has positive
coverage but is not claimed as a newly fixed failure.

ECDSA coverage calls the helper directly. The normal host-key dispatcher first
validates the outer key type, so the helper test does not establish that a
malformed key file or remote peer can reach the historical over-read. This is
defensive parser hardening. ECDSA point decoding and packet encryption remain
observation stubs; these tests do not qualify cryptographic behavior.

These are host regressions for the patched source. Target compilation, actual
OpenSSH protocol interoperability, PTY signal delivery, hardware entropy
readiness and resource recovery on the device remain separate qualification
gates in `docs/dropbear-ssh.md`.
