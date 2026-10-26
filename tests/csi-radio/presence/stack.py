"""Target-code regression gate for recorder producer stack frames.
This bounds individual Xtensa frames, not the complete native call chain;
physical callback high-water measurements are independently required."""

import re

FRAME_LIMIT = 512
REQUIRED = (
    "presence_lab_rf",
    "csi_idf60_reference::lab::bytes",
    "csi_idf60_reference::lab::floats",
    "csi_lab_core::recorder::Lease::wander",
    "csi_lab_core::ownership::Ingress::observe",
)


def qualify(disassembly):
    frames = []
    for name, body in re.findall(
        r"^[0-9a-f]+ <([^\n]+)>:\n(.*?)(?=^[0-9a-f]+ <|\Z)", disassembly, re.M | re.S
    ):
        selected = (
            name in REQUIRED
            or "csi_lab_core::recorder::Recorder::append_admitted" in name
            or "csi_lab_core::recorder::Lease::wander" in name
            or "csi_lab_core::ownership::Ingress::publish" in name
            or "csi_lab_core::calibration::Runtime::write" in name
            or "csi_lab_core::calibration::Runtime::execute_with" in name
        )
        if not selected:
            continue
        entry = re.search(r"\bentry\s+a1,\s*(0x[0-9a-f]+|\d+)", body)
        if not entry:
            raise ValueError("producer frame entry missing")
        size = int(entry[1], 0)
        if size > FRAME_LIMIT:
            raise ValueError("recorder producer stack frame exceeds 512 bytes: " + name)
        frames.append(dict(symbol=name, stack_frame_bytes=size))
    if not all(any(r["symbol"] == name for r in frames) for name in REQUIRED):
        raise ValueError("required recorder producer stack witness missing")
    if not any("append_admitted" in r["symbol"] for r in frames):
        raise ValueError("in-place recorder stack witness missing")
    return dict(
        frame_limit_bytes=FRAME_LIMIT,
        producer_frames=frames,
        worker_stack_bytes=3072,
        scope="individual producer frames only; complete native call-chain and physical high-water qualification remain separate",
    )
