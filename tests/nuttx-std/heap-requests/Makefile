include $(APPDIR)/Make.defs

PROGNAME = heap_requests
PRIORITY = 100
STACKSIZE = 8192
MODULE = $(CONFIG_EXAMPLES_HEAP_REQUESTS)
MAINSRC = requests_main.c
# The builder copies and hashes the existing qualification clock adapter.
CSRCS = qemu_timer_clock.c

include $(APPDIR)/Application.mk
