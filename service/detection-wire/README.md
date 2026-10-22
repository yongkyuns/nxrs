# Detection wire protocol

This crate is the intentionally shared camera/gateway wire protocol. It is not
a HAL API or a generic event bus. See [delivery and ownership](../../docs/camera-detection-delivery.md)
and the [JSON Schema](../../docs/detection-protocol.schema.json).

Pinned serde/serde_json use safe Rust and the same qualified std I/O as the
application. Source lists and serialization storage are bounded; the MCU path
serializes into an existing slice without per-frame allocation. Host parsing is
limited before deserialization. Target qualification uses the complete camera
image and preserves its inference, callback and shutdown requirements.
