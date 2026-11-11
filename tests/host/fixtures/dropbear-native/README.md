# Native Dropbear regression witnesses

Run from the repository root:

```sh
python3 -m unittest tests.host.test_dropbear_native -v
```

The runner applies the registered outer NuttX Apps patch and the same nested
Dropbear patches used by the firmware build. It compiles the resulting C source
with AddressSanitizer, UndefinedBehaviorSanitizer and warnings as errors. A C
compiler, GNU `patch` and `git` are required; no network or submodule checkout is
needed.

`upstream.json` identifies the pinned NuttX Apps and Dropbear revisions and the
SHA-256 of each upstream input. The readable files under `upstream/src` retain
their upstream license headers and exact original bytes, including trailing
spaces and blank lines; the local Git attribute preserves those hashed snapshots.
Existing NuttX Apps files are reconstructed from
the complete preimages in the outer patch and checked against that manifest
before applying it. This makes a patch or dependency revision change explicit.

Most witnesses include a complete patched translation unit. The channel witness
extracts the complete `newchannel` and `recv_msg_channel_close` definitions
verbatim from the prepared source; it does not duplicate their implementation.
The headers in `include` and dependency functions in each witness provide the
small ABI surface necessary to observe ownership and inject failures.

The suite checks the real foreground command and socket lifecycle, singleton
generation races, bounded stop waits, preallocated channel admission, child and
waiter retirement, password rejection, descriptor cleanup after crypto errors,
allocation epochs, qualified entropy failures and random-state cleanup. SHA
operations in the random-state witness are observation stubs; they do not claim
to validate a cryptographic implementation. LeakSanitizer is disabled because
the test environment runs under ptrace; explicit ownership and release counts
cover the injected failure paths.

These are host regressions for the patched source. Target compilation, actual
OpenSSH protocol interoperability, PTY signal delivery, hardware entropy
readiness and resource recovery on the device remain separate qualification
gates in `docs/dropbear-ssh.md`.
