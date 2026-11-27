include $(APPDIR)/Make.defs

PROGNAME = heap_replay
PRIORITY = 100
STACKSIZE = 8192
MODULE = $(CONFIG_EXAMPLES_HEAP_REPLAY)
MAINSRC = replay_main.c
# The builder copies the existing memory-timing clock adapter here and retains
# its source hash. It is shared fixture code, not another clock implementation.
CSRCS = qemu_timer_clock.c

include $(APPDIR)/Application.mk
