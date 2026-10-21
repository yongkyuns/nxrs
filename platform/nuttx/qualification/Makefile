include $(APPDIR)/Make.defs
PROGNAME = nxrs_sim
PRIORITY = 100
STACKSIZE = 65536
MODULE = $(CONFIG_EXAMPLES_NXRS_SIM)
MAINSRC = runner.c
CSRCS = storage.c camera.c close.c qualification.c close_fault.c storage_fault.c

# The old core-only ARCH_SIM fixture keeps its test-local UDP scalar bridge.
# The std integration image selects transport's NuttX provider and must not
# compile or link that compatibility implementation.
ifeq ($(NXRS_STD_ELF),)
CSRCS += time.c transport.c
endif

# Test-only interposition: the real bridge still calls actual NuttX close.
# close_fault.c returns an injected error after closing and reusing an fd.
# Production builds of close.c keep the ordinary rc_nx_close symbol.
close.c_CFLAGS += -Drc_nx_close=rc_nx_close_real

# Inject below append/flush so the production retry/rollback code still runs.
# These macros affect ONLY storage.c; the fixture calls the ordinary target VFS.
storage.c_CFLAGS += -Drc_nx_file_create=rc_nx_file_create_real \
  -Dwrite=rc_test_storage_write -Dftruncate=rc_test_storage_truncate \
  -Dlseek=rc_test_storage_seek -Dfsync=rc_test_storage_sync

# ARCH_SIM retains the old core-only staticlib solely as a compatibility oracle.
# The ESP32-S3 std integration path consumes an ordinary Cargo binary and
# renames rustc's generated C-ABI main; its call into std::rt is preserved.
ifneq ($(NXRS_STD_ELF),)
runner.c_CFLAGS += -DNXRS_STD_ENTRY=1
EXTOBJS = nxrs_std_app.o
nxrs_std_app.o: $(NXRS_STD_ELF)
	$(OBJCOPY) --redefine-sym main=rc_rust_std_main $< $@
else
EXTOBJS = nxrs_core.o
nxrs_core.o: $(NXRS_SIM_LIB)
	$(LD) -r --whole-archive $(NXRS_SIM_LIB) --no-whole-archive -o $@
endif

include $(APPDIR)/Application.mk
