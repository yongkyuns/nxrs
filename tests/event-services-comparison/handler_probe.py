"""Parse/summarize rows from the diagnostics-only C handler probe."""
import argparse
import json
from pathlib import Path
from statistics import median
import sys


HERE = Path(__file__).resolve().parent


EXPECTED_JOBS = 117
WORK_ITERATIONS = 400_000
MAX_SNAPSHOT_SKEW = 256
MAX_ALLOWED_SNAPSHOT_SKEW = 4096
RUNTIME_PROTOTYPES = """/* Diagnostics-only adapter declarations. */
#include <stdint.h>
struct es_event;
uint32_t es_diag_original_now(void);
void es_diag_original_receive(unsigned id, const struct es_event *event,
                              int result, uint32_t started, uint32_t finished);
void es_diag_original_resources(void);
void es_probe_register(unsigned id);
void es_probe_reset(void);

"""
ROW_FIELDS = {
    "sequence", "peer", "wall_cycles", "running_cycles", "other_cycles",
    "irq_cycles", "switches", "observed_cycles",
}


def _replace_once(source, old, new, label):
    count = source.count(old)
    if count != 1:
        raise ValueError(f"expected exactly one {label} anchor; found {count}")
    return source.replace(old, new, 1)


def adapt_diagnostic_sources(runtime_source, platform_source):
    """Return instrumented copies of the shared runtime and NuttX adapter.

    Only the target definitions are renamed, so existing runtime call sites
    resolve to the diagnostic wrappers. The portable service loop is otherwise
    preserved byte-for-byte. Inputs with partial or repeated adaptation are
    rejected instead of being silently rewritten again.
    """
    if not isinstance(runtime_source, str) or not isinstance(platform_source, str):
        raise ValueError("diagnostic source adapter expects text inputs")
    if any(marker in runtime_source or marker in platform_source for marker in (
            "es_diag_original_", "es_probe_register(", "es_probe_reset(")):
        raise ValueError("diagnostic source appears already adapted")

    runtime = _replace_once(
        runtime_source, "uint32_t es_now(void) {",
        "uint32_t es_diag_original_now(void) {", "es_now definition")
    runtime = _replace_once(
        runtime,
        "void es_record_receive(unsigned id, const struct es_event *event, int result,\n"
        "                       uint32_t started, uint32_t finished) {",
        "void es_diag_original_receive(unsigned id, const struct es_event *event, int result,\n"
        "                               uint32_t started, uint32_t finished) {",
        "es_record_receive definition")
    runtime = _replace_once(
        runtime,
        "static void *service_entry(void *argument) {\n"
        "  unsigned id = (unsigned)(uintptr_t)argument;\n"
        "  if (es_platform_arrive()) {",
        "static void *service_entry(void *argument) {\n"
        "  unsigned id = (unsigned)(uintptr_t)argument;\n"
        "  es_probe_register(id);\n"
        "  if (es_platform_arrive()) {",
        "service_entry registration")
    runtime = _replace_once(
        runtime, "int es_run_main(int argc, const char *const *argv) {",
        "int es_run_main(int argc, const char *const *argv) {\n  es_probe_reset();",
        "es_run_main reset")
    platform = _replace_once(
        platform_source, "void es_platform_resources(void) {",
        "void es_diag_original_resources(void) {", "es_platform_resources definition")
    return RUNTIME_PROTOTYPES + runtime, platform


def _fields(line, marker):
    fields = {}
    for item in line.split()[1:]:
        if "=" not in item:
            raise ValueError(f"invalid {marker} field")
        key, value = item.split("=", 1)
        if key in fields or not value:
            raise ValueError(f"duplicate or empty {marker} field")
        fields[key] = value
    return fields


def parse_rows(output, *, max_snapshot_skew=MAX_SNAPSHOT_SKEW):
    """Validate the fixed 117-job, 400k-iteration diagnostics capture.

    Other console lines are ignored. HP_ROW/HP_DONE records are strict; the
    observed elapsed counter is checked against category-summed wall time to
    bound snapshot overhead without conflating it with a measured category.
    """
    if isinstance(output, bytes):
        try:
            output = output.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("probe output must be ASCII") from exc
    if not isinstance(output, str):
        raise ValueError("probe output must be text or bytes")
    if (isinstance(max_snapshot_skew, bool) or not isinstance(max_snapshot_skew, int)
            or not 0 <= max_snapshot_skew <= MAX_ALLOWED_SNAPSHOT_SKEW):
        raise ValueError(
            f"snapshot skew bound must be between 0 and {MAX_ALLOWED_SNAPSHOT_SKEW}")

    rows, seen, done = [], set(), []
    for line in output.splitlines():
        if line.startswith("HP_ROW "):
            fields = _fields(line, "HP_ROW")
            if set(fields) != ROW_FIELDS:
                raise ValueError("HP_ROW fields do not match diagnostics schema")
            try:
                row = {key: int(value, 10) for key, value in fields.items()}
            except ValueError as exc:
                raise ValueError("HP_ROW values must be decimal integers") from exc
            if any(value < 0 or value > 0xFFFFFFFF for value in row.values()):
                raise ValueError("HP_ROW values must fit unsigned 32-bit fields")
            if row["peer"] > 2 or row["sequence"] > 38:
                raise ValueError("HP_ROW identity is outside the 3-peer, 39-sequence workload")
            identity = (row["peer"], row["sequence"])
            if identity in seen:
                raise ValueError("duplicate HP_ROW (peer, sequence) identity")
            seen.add(identity)
            category_sum = row["running_cycles"] + row["other_cycles"] + row["irq_cycles"]
            if row["wall_cycles"] <= 0 or category_sum != row["wall_cycles"]:
                raise ValueError("HP_ROW wall cycles must equal category sum and be positive")
            if row["observed_cycles"] <= 0:
                raise ValueError("HP_ROW observed cycles must be positive")
            if abs(row["wall_cycles"] - row["observed_cycles"]) > max_snapshot_skew:
                raise ValueError("HP_ROW snapshot skew exceeds configured bound")
            rows.append(row)
        elif line.startswith("HP_DONE "):
            done.append(_fields(line, "HP_DONE"))

    if len(done) != 1 or set(done[0]) != {"jobs", "errors"}:
        raise ValueError("expected exactly one complete HP_DONE record")
    try:
        jobs, errors = int(done[0]["jobs"], 10), int(done[0]["errors"], 10)
    except ValueError as exc:
        raise ValueError("HP_DONE values must be decimal integers") from exc
    if jobs != EXPECTED_JOBS or len(rows) != EXPECTED_JOBS:
        raise ValueError(f"expected {EXPECTED_JOBS} rows and completed jobs")
    expected_identities = {(peer, sequence) for peer in range(3) for sequence in range(39)}
    if seen != expected_identities:
        raise ValueError("HP_ROW identities must cover every peer/sequence pair")
    if errors != 0:
        raise ValueError("HP_DONE reports diagnostic application errors")

    rows.sort(key=lambda row: (row["peer"], row["sequence"]))
    metric_names = ("wall_cycles", "running_cycles", "other_cycles", "irq_cycles",
                    "observed_cycles", "switches")
    totals = {name: sum(row[name] for row in rows) for name in metric_names}
    medians = {name: median(row[name] for row in rows) for name in metric_names}
    if totals["wall_cycles"] != (totals["running_cycles"] + totals["other_cycles"]
                                  + totals["irq_cycles"]):
        raise ValueError("aggregate cycle categories do not sum to wall time")
    return {
        "diagnostic_only": True,
        "work_iterations": WORK_ITERATIONS,
        "jobs": jobs,
        "errors": errors,
        "rows": rows,
        "summary": {
            "totals": totals,
            "per_row_medians": medians,
            # Keep the cycle decomposition tied to one actual maximum-wall row;
            # independent per-category maxima need not come from the same event.
            "max_wall_row": max(rows, key=lambda row: row["wall_cycles"]),
        },
        "interpretation": (
            "diagnostic hook-boundary scheduler/IRQ accounting; buckets include "
            "callback/context-switch costs and are not instruction-only; this is not "
            "production qualification or proof of preemption"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    adapt = commands.add_parser("adapt", help="write isolated diagnostic C source copies")
    adapt.add_argument("--out", required=True, type=Path, help="new output directory")
    parse = commands.add_parser("parse", help="validate a probe capture and emit JSON")
    parse.add_argument("capture", type=Path, help="text capture containing HP_ROW/HP_DONE")
    parse.add_argument("--max-snapshot-skew", type=int, default=MAX_SNAPSHOT_SKEW,
                       help=f"maximum observed/accounted skew in cycles (0..{MAX_ALLOWED_SNAPSHOT_SKEW})")
    args = parser.parse_args(argv)

    try:
        if args.command == "adapt":
            if args.out.exists() or args.out.is_symlink():
                raise ValueError(f"output path already exists: {args.out}")
            runtime_path = HERE / "runtime.c"
            platform_path = HERE / "platform_nuttx.c"
            runtime, platform = adapt_diagnostic_sources(
                runtime_path.read_text(), platform_path.read_text())
            args.out.mkdir(parents=True, exist_ok=False)
            (args.out / "runtime.c").write_text(runtime)
            (args.out / "platform_nuttx.c").write_text(platform)
        else:
            result = parse_rows(args.capture.read_bytes(),
                                max_snapshot_skew=args.max_snapshot_skew)
            sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError) as exc:
        print(f"handler_probe: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
