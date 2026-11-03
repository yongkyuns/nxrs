# Pinned Crossbeam progress paths

This audit covers the exact registry sources selected by the nxrs lockfile:
`crossbeam-channel 0.5.17` and `crossbeam-utils 0.8.23`. It motivates the P1
interleavings; it is not a physical execution result or a progress qualification.
The existing actual-std witnesses remain under `../std-channel-perf`.

## Source identity

| Source | Authenticated SHA-256 |
| --- | --- |
| [crossbeam-channel archive](https://static.crates.io/crates/crossbeam-channel/crossbeam-channel-0.5.17.crate) | `98b0cc327b5bc766e7fda9c9260cc0fa81b43a8e240440422dff70788e3f9ef1` |
| [crossbeam-utils archive](https://static.crates.io/crates/crossbeam-utils/crossbeam-utils-0.8.23.crate) | `a31eee39dddec8330830986fcd7625edb5a24ec90ea038215273bbc3adb08ac6` |
| Channel `src/flavors/array.rs` | `c4cfcbecb137bb8d605801b36c5faee87abaa2579b6f6be7d35332a280e147c9` |
| Channel `src/select.rs` | `301c765751586204371bedb69162e23bcf7e094cbc37b72203698a18b889550f` |
| Utils `src/backoff.rs` | `8715f0303ec91d1847c8ac3fc24bcc002a22a7284ade610e5eff4181f85827c7` |

Both archives identify source revision
[`2920c984290229ab4e0ca0452ef09e48a82063f3`](https://github.com/crossbeam-rs/crossbeam/tree/2920c984290229ab4e0ca0452ef09e48a82063f3).
Preparation authenticates the complete archives before extraction. The inventory
hash is SHA-256 of a sorted, compact JSON map from every relative regular-file
path to its SHA-256. The original channel inventory contains 43 files and hashes
to `d27ab09eddc299e42f1d40d726029c745b06173f084e122039147027bbbffed0`;
the original utils inventory contains 32 files and hashes to
`6c63d97f344fdca6aca92f429681613c30beb9b68f6c5898ed303fd8251022b1`.

## Reservation and reclamation

The bounded array's `start_send` advances the tail with a successful compare and
exchange before `write` initializes the payload and publishes its slot stamp with
release ordering. A receiver encountering that reserved but unpublished head
can remain inside `start_recv`, retrying while the lower-priority sender cannot
run. The direct `recv` wrapper checks its timeout outside this inner operation;
passing a timeout therefore does not itself bound this particular retry path.
These boundaries are in the authenticated
[`array.rs`](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/flavors/array.rs).

The reverse direction also requires a witness. `start_recv` advances the head
before `read` consumes the payload and publishes the reusable slot stamp. With a
full ring, a higher-priority sender can encounter the reserved but unreclaimed
slot and retry inside `start_send`. A successful reclamation control must prove
the slot is reusable before waking that sender. A receive-only audit would miss
this priority relationship.

The inner loops use `Backoff::spin` or `Backoff::snooze`. `spin` repeatedly issues
spin-loop hints; `snooze` eventually calls `std::thread::yield_now` when its std
feature is enabled. Neither establishes that a lower-priority FIFO participant
will run. No backoff or scheduler implementation is changed by this fixture.
See the pinned
[`backoff.rs`](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-utils/src/backoff.rs).

## Three-arm selected wait

`run_select` first tries the selected handles and otherwise registers them in a
shared context, waits, unregisters, and accepts the selected operation. The array
receiver's `try_select` and `accept` ultimately use the same `start_recv` path.
The witness begins with stop, important and ordinary receive arms registered;
it requires both the recorded registration count of three and an independent
native `WAIT_SEM` observation before the producer is released. The registration
marker alone is not evidence that the owner blocked.

The important producer is interrupted at its reservation/publication hook. A
separate waker publishes to the ordinary arm, allowing the blocked owner to
complete that selected operation. The owner then re-enters the same biased
three-arm wait, where the important reservation is encountered. This explicitly
exercises a real selected wait and an independent arm wake, rather than merely
calling selection on a queue after the producer has reserved it. See pinned
[`select.rs`](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/select.rs).

## Private instrumentation contract

| Hook | Exact position | Witness purpose |
| --- | --- | --- |
| `1` | Successful tail reservation, before preparing the write token | Receive-side reserved slot |
| `2` | Release publication of the message stamp, before receiver notification | Published-message control |
| `3` | Successful head reservation, before preparing the read token | Sender-side unreclaimed slot |
| `4` | Release publication of the reclaimed stamp, before sender notification | Reclaimed-slot control |
| `0x100 + registered_count` | `run_select`, immediately before `Context::wait_until` | Actual selected registration count; `0x103` means three |

`prepare-crossbeam.py` changes only the selected array hook and the selection
registration marker in a new private source tree. Hook calls exist only for
`target_os = "nuttx"`; the clean mode is byte-identical across both entire source
trees. The hook is a fixture-owned Rust function with the C ABI only for linking
the separately compiled dependency. Workers, lifetime, recovery and reporting
remain Rust code in the test fixture. Installed Cargo sources and production
dependencies are never patched.

`crossbeam-hook.json` records the original and selected complete source maps,
archive identities, hook mode and patch configuration. Validation reconstructs
every expected selected byte from the retained authenticated archives and rejects
modified source, altered receipts, changed patch paths, links or extra source
files. The firmware build must separately prove Cargo compiled these paths and
bind the receipt to its compiler, configuration, image and runtime build identity.
Source preparation alone cannot establish that a firmware image used the sources.

An expected reservation stall is successful hazard reproduction and a failed
no-repair progress contract. Cleanup priority changes must remain visible in the
result; neither recovery nor the published/reclaimed control alone qualifies the
full intended transport contract. The observation interval is a diagnostic window,
not an upper bound on how long the channel could stall without intervention.
