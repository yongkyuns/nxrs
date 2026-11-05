# Native bounded-channel host controls

Run against a private SDK produced by the normal frontend:

```sh
python3 tests/nuttx-std/channels/run.py --prepared-sdk PREPARED --out NEW_OUTPUT
```

The runner verifies the exact queue patch, overrides its selector only in a
private copy, rebuilds std and checks Cargo's actual selected source. Nine
controls cover capacity/errors, producer FIFO, MPMC ownership, disconnect/drop,
timeouts, reentrant destruction, unrelated park permits, zero/unbounded channels
and notification after the completed owner has joined. The host supplies mutual
exclusion; target interrupt masking and native NuttX parking require separate
device qualification. No installed SDK is changed.
