#!/usr/bin/env python3
"""Render the comparison's three charts from committed numeric evidence."""

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape


HERE = Path(__file__).resolve().parent
RESULTS = HERE.parents[2] / "tests/event-services-comparison/results"
PLATFORMS = (
    ("nuttx-c-three", "NuttX C"),
    ("nuttx-rust-three", "NuttX Rust / native"),
    ("zephyr-c-three", "Zephyr C"),
    ("embassy-three", "Embassy Rust"),
)
COLORS = ("#2563eb", "#0f766e", "#d97706")


def cases(filename):
    return json.loads((RESULTS / filename).read_text())["cases"]


def chart_data():
    minimal = cases("esp32s3-minimal-2026-10-09.json")
    slow = cases("esp32s3-controls-10ms-2026-10-04.json")
    fast = cases("esp32s3-controls-1ms-2026-10-04.json")
    ram, flash, timer = [], [], []
    for key, label in PLATFORMS:
        image = minimal[key]["flash_and_ram"]
        total = image["whole_ram_peak_bytes"]
        stack = 8192 if key.startswith("embassy") else 20 * 4096
        # Nominal test fields, not an attribution of every alignment/tag byte.
        diagnostics = 29920 + 276
        ram.append((label, (stack, diagnostics, total - stack - diagnostics)))
        flash.append((label, (image["loadbearing_flash_bytes"], image["image_bytes"])))
        p99 = lambda source: source[key]["profiles"]["normal"]["result_metrics"]["start_p99_us"]["median"]
        timer.append((label, (p99(slow) / 1000, p99(fast) / 1000)))
    return {"ram": ram, "flash": flash, "timer": timer}


def text(x, y, value, size=14, anchor="start", color="#172554"):
    return (f'<text x="{x:g}" y="{y:g}" font-size="{size}" '
            f'text-anchor="{anchor}" fill="{color}">{escape(str(value))}</text>')


def render(title, description, rows, legends, limit, ticks, unit, stacked=False, budget=None):
    """Horizontal, zero-origin bars; all panels use explicit units and scales."""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="440" viewBox="0 0 1000 440" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title>',
        f'<desc id="desc">{escape(description)}</desc>',
        '<rect width="1000" height="440" fill="white"/>',
        '<g font-family="Arial, sans-serif">',
        text(24, 32, title, 20),
        text(24, 56, description, 12, color="#475569"),
    ]
    left, width, top, bottom = 230, 665, 112, 354
    scale = width / limit
    for tick in ticks:
        x = left + tick * scale
        parts.append(f'<path d="M{x:g} {top}V{bottom}" stroke="#e2e8f0"/>')
        parts.append(text(x, bottom + 22, f"{tick:g}", 12, "middle", "#475569"))
    parts.append(text(left + width / 2, 406, unit, 13, "middle"))
    if budget is not None:
        x = left + budget * scale
        parts.append(f'<path d="M{x:g} {top - 12}V{bottom}" stroke="#be123c" stroke-dasharray="5 4"/>')
        parts.append(text(x, top - 18, "250 kB reference", 12, "middle", "#be123c"))
    for index, (label, values) in enumerate(rows):
        y = 136 + index * 60
        parts.append(text(left - 14, y + 5, label, 14, "end"))
        if stacked:
            offset = 0
            for color, value in zip(COLORS, values):
                value /= 1000
                parts.append(f'<rect x="{left + offset * scale:g}" y="{y - 12}" width="{value * scale:g}" height="24" fill="{color}"/>')
                offset += value
            parts.append(text(left + offset * scale + 8, y + 5, f"{offset:.1f}", 13))
        else:
            for n, (color, value) in enumerate(zip(COLORS, values)):
                plotted = value / 1000 if unit == "kB (1,000 bytes)" else value
                bar_y = y - 17 + n * 19
                parts.append(f'<rect x="{left}" y="{bar_y}" width="{plotted * scale:g}" height="15" fill="{color}"/>')
                parts.append(text(left + plotted * scale + 8, bar_y + 12, f"{plotted:.2f}", 12))
    x = 24
    for color, label in zip(COLORS, legends):
        parts.append(f'<rect x="{x}" y="74" width="12" height="12" fill="{color}"/>')
        parts.append(text(x + 18, 85, label, 12))
        x += len(label) * 7 + 46
    parts.extend(("</g>", "</svg>"))
    return "\n".join(parts) + "\n"


def charts():
    data = chart_data()
    return {
        "ram-capacity.svg": render(
            "RAM when all 480 queue slots are exercised",
            "October 9 minimal-NuttX cohort; 20 services, 60 queues. Includes allocator high-water.",
            data["ram"], ("Service stacks", "Test fields (nominal)", "Everything else"),
            280, range(0, 281, 40), "kB (1,000 bytes)", stacked=True, budget=250),
        "image-size.svg": render(
            "Firmware code + initialized data and flash binary size",
            "October 9: minimal NuttX, Zephyr C, Embassy natural waits. Boot layouts still differ.",
            data["flash"], ("Code + initialized data", "Flash binary size"),
            240, range(0, 241, 40), "kB (1,000 bytes)"),
        "timer-response.svg": render(
            "Publication wake resolution changes end-to-end response",
            "Matched normal-traffic controls: median per-run release-to-handler p99 upper bound.",
            data["timer"], ("10 ms wake resolution", "1 ms wake resolution"),
            25, range(0, 26, 5), "Milliseconds"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if a committed chart is stale")
    args = parser.parse_args()
    stale = []
    for name, svg in charts().items():
        path = HERE / name
        if args.check:
            if not path.exists() or path.read_text() != svg:
                stale.append(name)
        else:
            path.write_text(svg)
    if stale:
        parser.error("stale charts: " + ", ".join(stale))


if __name__ == "__main__":
    main()
