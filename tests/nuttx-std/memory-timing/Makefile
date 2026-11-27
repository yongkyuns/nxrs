include $(APPDIR)/Make.defs

PROGNAME = memory_timing
PRIORITY = 100
STACKSIZE = 8192
MODULE = $(CONFIG_EXAMPLES_MEMORY_TIMING)
MAINSRC = timing_main.c
CSRCS = acquisition_trace.c

ifeq ($(CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK),y)
CSRCS += qemu_timer_clock.c
endif

include $(APPDIR)/Application.mk
