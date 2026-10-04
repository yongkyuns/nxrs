#!/usr/bin/env python3
"""Adapt only OS entry points; keep the checked-in C workload as the source."""
import argparse
from pathlib import Path


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"control source changed: expected one {old!r}")
    return source.replace(old, new)


def adapt(source):
    for header in ("fcntl.h", "mqueue.h", "poll.h", "pthread.h", "unistd.h"):
        source = replace_once(source, f"#include <{header}>\n", "")
    source = replace_once(source, "#include <errno.h>",
                          '#include "native_adapter.h"\n#include <errno.h>')
    # Native queues use object addresses/indices, not filesystem names. Keep
    # descriptor ownership and setup order, without retaining unused names.
    source = replace_once(source, "  char name[32];\n", "")
    source = replace_once(source,
        '  snprintf(queue->name, sizeof(queue->name), "/cqs%04x%02x",\n'
        '           (unsigned)(cycles() ^ (uint32_t)getpid()) & 0xffffu, id);\n',
        "  (void)id;\n")
    source = replace_once(source, "mq_open(queue->name,", "mq_open(NULL,")
    source = replace_once(source, "      mq_unlink(queue->name);\n", "")
    # Zephyr applications normally reserve their fixed topology statically.
    # Do not reserve an oversized heap arena just to emulate calloc/free.
    source = replace_once(source, "static int run_scenario(struct config config, unsigned seed)",
                          "static struct scenario scenario_storage;\n\n"
                          "static int run_scenario(struct config config, unsigned seed)")
    source = replace_once(source, "struct scenario *scenario = calloc(1, sizeof(*scenario));",
                          "struct scenario *scenario = &scenario_storage;\n"
                          "  memset(scenario, 0, sizeof(*scenario));")
    if source.count("free(scenario);") != 2:
        raise ValueError("control scenario teardown changed")
    source = source.replace("free(scenario);", "/* Static scenario storage is reused after joins. */")
    source = source.replace("CQ_C_", "CQ_ZEPHYR_")
    return replace_once(source, "language=c mode=large", "language=zephyr mode=large")


def nuttx_without_wake(source):
    """Remove only the auxiliary probe that is absent from the native app."""
    block = ('#ifndef NXRS_CQ_EMBED_CONTROL\n'
             '  if (run_wake() != 0)\n'
             '    {\n'
             '      fprintf(stderr, "CQ_C_WAKE_FAIL\\n");\n'
             '      return 1;\n'
             '    }\n'
             '#endif\n')
    return replace_once(source, block, '/* Separate wake probe omitted in this comparison. */\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--nuttx-without-wake", action="store_true")
    args = parser.parse_args()
    transform = nuttx_without_wake if args.nuttx_without_wake else adapt
    args.output.write_text(transform(args.source.read_text()))


if __name__ == "__main__":
    main()
