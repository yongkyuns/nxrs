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
COLORS = ("#2563eb", "#0f766e", "#d97706", "#CFD8DC")
# Material Blue, Teal, Deep-purple, Lime, Orange, Pink and Blue-grey 100.
RAM_COLORS = ("#BBDEFB", "#B2DFDB", "#D1C4E9", "#F0F4C3", "#FFE0B2", "#F8BBD0", "#CFD8DC")


def cases(filename):
    return json.loads((RESULTS / filename).read_text())["cases"]


def ram_rows():
    record = json.loads((RESULTS / "esp32s3-instrumentation-2026-10-10.json").read_text())
    rows = []
    for key, label in PLATFORMS:
        pair = []
        for cohort in ("reference", "lean"):
            case = record[cohort]["cases"][key]
            attribution = record["ram_attribution"]["cohorts"][cohort][key]
            if attribution["elf_sha256"] != case["elf_sha256"]:
                raise ValueError("RAM ledger and measured ELF identities differ")
            groups = attribution["components"]
            parts = tuple(groups[name] for name in ("execution_workspace", "event_capacity",
                          "queue_adapter_controls", "fixture_application_state",
                          "known_test_storage", "ram_code"))
            parts += (sum(groups[name] for name in ("platform_stacks_and_arena",
                      "linked_data_not_split", "live_heap_not_split", "heap_peak_margin")),)
            total = case["flash_and_ram"]["whole_ram_peak_bytes"]
            if min(parts) < 0 or sum(parts) != total:
                raise ValueError("RAM ledger does not reconcile to the measured total")
            pair.append(parts)
        rows.append((label, tuple(pair)))
    return rows


def chart_data():
    minimal = cases("esp32s3-minimal-task-trim-2026-10-09.json")
    packages = cases("esp32s3-image-packages-task-trim-2026-10-09.json")
    slow = cases("esp32s3-controls-10ms-2026-10-04.json")
    fast = cases("esp32s3-controls-1ms-2026-10-04.json")
    flash, timer = [], []
    for key, label in PLATFORMS:
        image = minimal[key]["flash_and_ram"]
        package = packages[key]
        if (package["image_sha256"] != minimal[key]["image_sha256"]
                or package["elf_sha256"] != minimal[key]["elf_sha256"]):
            raise ValueError("package and device-measurement image identities differ")
        flash.append((label, (image["loadbearing_flash_bytes"], package["package_bytes"])))
        p99 = lambda source: source[key]["profiles"]["normal"]["result_metrics"]["start_p99_us"]["median"]
        timer.append((label, (p99(slow) / 1000, p99(fast) / 1000)))
    return {"ram": ram_rows(), "flash": flash, "timer": timer}


def text(x, y, value, size=14, anchor="start", color="#172554"):
    return (f'<text x="{x:g}" y="{y:g}" font-size="{size}" '
            f'text-anchor="{anchor}" fill="{color}">{escape(str(value))}</text>')


def render(title, description, rows, legends, limit, ticks, unit, stacked=False, budget=None,
           paired=False, colors=COLORS, legend_columns=None):
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
    if legend_columns:
        top += 20
        bottom += 20
    scale = width / limit
    for tick in ticks:
        x = left + tick * scale
        parts.append(f'<path d="M{x:g} {top}V{bottom}" stroke="#e2e8f0"/>')
        parts.append(text(x, bottom + 22, f"{tick:g}", 12, "middle", "#475569"))
    parts.append(text(left + width / 2, 426 if legend_columns else 406, unit, 13, "middle"))
    if budget is not None:
        x = left + budget * scale
        parts.append(f'<path d="M{x:g} {top - 12}V{bottom}" stroke="#be123c" stroke-dasharray="5 4"/>')
        parts.append(text(x, top - 18, "250 kB reference", 12, "middle", "#be123c"))
    for index, (label, values) in enumerate(rows):
        y = 136 + index * 60 + (20 if legend_columns else 0)
        parts.append(text(left - 14, y + 5, label, 14, "end"))
        if stacked:
            for n, segments in enumerate(values if paired else (values,)):
                offset = 0
                bar_y, height = (y - 17 + n * 21, 17) if paired else (y - 12, 24)
                clip = f"bar-{index}-{n}"
                parts.append(f'<clipPath id="{clip}"><rect x="{left}" y="{bar_y}" width="{sum(segments) / 1000 * scale:g}" height="{height}" rx="3"/></clipPath>')
                parts.append(f'<g clip-path="url(#{clip})">')
                for color, value in zip(colors, segments):
                    value /= 1000
                    parts.append(f'<rect x="{left + offset * scale:g}" y="{bar_y}" width="{value * scale:g}" height="{height}" fill="{color}"/>')
                    offset += value
                parts.append("</g>")
                suffix = (" full", " lean")[n] if paired else ""
                parts.append(text(left + offset * scale + 8, bar_y + height - 3, f"{offset:.1f}{suffix}", 13))
        else:
            for n, (color, value) in enumerate(zip(COLORS, values)):
                plotted = value / 1000 if unit == "kB (1,000 bytes)" else value
                bar_y = y - 17 + n * 19
                parts.append(f'<rect x="{left}" y="{bar_y}" width="{plotted * scale:g}" height="15" fill="{color}"/>')
                parts.append(text(left + plotted * scale + 8, bar_y + 12, f"{plotted:.2f}", 12))
    x = 24
    for index, (color, label) in enumerate(zip(colors, legends)):
        legend_y = 74
        if legend_columns:
            x = 24 + index % legend_columns * (940 / legend_columns)
            legend_y += index // legend_columns * 18
        parts.append(f'<rect x="{x}" y="{legend_y}" width="12" height="12" fill="{color}"/>')
        parts.append(text(x + 18, legend_y + 11, label, 12))
        if not legend_columns:
            x += len(label) * 7 + 46
    parts.extend(("</g>", "</svg>"))
    return "\n".join(parts) + "\n"


def charts():
    data = chart_data()
    return {
        "ram-capacity.svg": render(
            "RAM: workload choices, test costs and runtime",
            "October 10 matched full/lean builds. Runtime remainder is not a fixed or unavoidable minimum.",
            data["ram"], ("Execution workspace", "Queue slots (chosen)", "Queue/adapter controls",
                          "Fixture app state", "Checker/coordinator", "RAM code / vectors",
                          "Runtime / not split"),
            280, range(0, 281, 40), "kB (1,000 bytes)", stacked=True, budget=250,
            paired=True, colors=RAM_COLORS, legend_columns=4),
        "image-size.svg": render(
            "Firmware code and gap-free distribution package size",
            "No compression. Packages omit address gaps but include boot data and ZIP metadata.",
            data["flash"], ("Code + initialized data", "Gap-free package size"),
            160, range(0, 161, 40), "kB (1,000 bytes)"),
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
