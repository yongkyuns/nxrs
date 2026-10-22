# CSI presence evidence evaluation

This standalone host fixture adapts Espressif's demo reports to the Nxrs Rust
presence policy. It is separate from firmware and all running camera services.
See [the design, commands and physical qualification gates](../../docs/csi-presence.md).

`fixtures/lifecycle.log` is hand-authored protocol test data. All output is
advisory and clearly marked as replay or observer data. The upstream demo does
not export RF frame freshness, so this fixture must not drive recording or
absence decisions. Raw captures should stay outside Git: they can contain local
network identities and observations about occupancy.
