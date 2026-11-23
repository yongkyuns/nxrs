# Detection wire protocol

This crate owns the versioned camera/gateway protocol and the presence and
event kinds shared by the detection policy. Policy callers can compile those
types with `default-features = false`. The default `json` feature supplies
the codec and protocol records. [Delivery and ownership](../../docs/camera-detection-delivery.md) · [JSON Schema](../../docs/detection-protocol.schema.json)

Pinned serde/serde_json use safe Rust and the same qualified std I/O as the
application. Source lists and serialization storage are bounded; the MCU path
serializes into an existing slice without per-frame allocation. Host parsing is
limited before deserialization. Target qualification uses the complete camera
image and preserves its inference, callback and shutdown requirements.
