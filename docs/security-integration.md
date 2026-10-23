# Security review-branch integration

The integration branch combines the verified-boot stack through draft #67,
authenticated gateway ingest and credential-safe publication through #46, and
encrypted recording/authenticated backups through #50. These remain review
branches. Integration does not authorize release merges, live services, board
operations, manufacturing changes or certification spending.

The gateway workspace contains controller, ingest, private-file and activity
crates. Its lock preserves the exact union of previously reviewed registry
versions/checksums. Both private-file and recorder CI jobs remain required;
the installed FFmpeg SDK requirements for the separate publisher are retained.
The root firmware workspace remains separate from this host dependency graph.

## Composed host qualification

The disposable Ubuntu `Encrypted activity volume` workflow runs the existing
real LUKS2, recording, backup, corruption and interrupted-restore controls. It now
also runs a single composed path on that same owned encrypted volume:

1. The actual Rust camera contract and stream server serve synthetic JPEGs.
   A test-only mutual-TLS front end uses the real offline issuer's camera/client
   roles. This front end does not qualify NuttX TLS or device entropy.
2. The production Rust ingest requires camera TLS and sends JPEGs to the real
   encoder and separate native publisher. Two WebRTC readers must decode video
   while one camera source owner remains active.
3. The production recorder requires encrypted storage. A new motion clip must
   appear through the camera-scoped API, fully decode and reject publisher
   credentials for playback. Actual process metadata must exclude credentials.
4. After all recording/ingest resources retire, the actual backup CLI snapshots
   the store using the audited rebuilt engine. The same composed clip must
   survive authenticated restoration, match its byte count and SHA-256, fully
   decode through the authenticated API, and retain its exact bytes after an
   interrupted restore and explicit resume.
5. Existing key rotation, live nested-plaintext refusal, wrong key/UUID, locked
   volume, backup corruption and recovery tests remain mandatory.

Only `security-integration-qualification.json` is uploaded. It binds exact source
and executable hashes, the clean CI revision, and a public receipt for the
synthetic clip. Private keys, configuration, process logs, footage, raw disks
and backup repositories remain in disposable private fixture directories and
are removed after proven resource cleanup. A failed gate produces no success
report. The composed path is pending until its hosted run passes; syntax and
host unit results are not substituted for that execution.

Local checks use locked Rust tests/Clippy and inspected rendering or mocked
refusal tests. Formatting a volume, creating mappings/mounts, or changing live
services is restricted to the explicitly guarded disposable hosted fixture.

## Remaining acceptance gates

This composition does not complete MCU TLS/entropy/provisioning, protected
rollback or activation/confirmation, full NuttX kernel/application startup,
physical ROM/watchdog/power-loss behavior, component key isolation, complete
native/OS/codec advisory closure, trusted release attestation or operator key
custody. The original acceptance scope remains in issues #33–#39 under #32.
Earlier component reports retain their own exact source/revision and evidence
scope; host integration does not turn them into physical or formal validation.
