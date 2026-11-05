include $(APPDIR)/Make.defs

PROGNAME = $(NXRS_APP_COMMAND)
PRIORITY = $(NXRS_APP_PRIORITY)
STACKSIZE = $(NXRS_APP_STACKSIZE)
MODULE = $(CONFIG_EXAMPLES_NXRS_STD_APP)
EXTOBJS = nxrs_app.o

nxrs_app.o: $(NXRS_STD_ELF)
	@test -n "$(NXRS_APP_COMMAND)"
	$(OBJCOPY) --redefine-sym main=$(NXRS_APP_COMMAND)_main $< $@

ifneq ($(NXRS_TARGET_C_SOURCE),)
CSRCS += $(NXRS_TARGET_C_SOURCE)
CFLAGS += $(NXRS_TARGET_C_FLAGS)
# Test helpers can inspect the configured native ABI without copied layouts.
CFLAGS += ${INCDIR_PREFIX}"$(TOPDIR)"
endif

ifeq ($(CONFIG_RP23XX_CYW43439_READ_OBSERVER),y)
# Only the optional board-level capture needs private PIO/DMA headers.
CFLAGS += ${INCDIR_PREFIX}"$(TOPDIR)/arch/arm/src/chip"
CFLAGS += ${INCDIR_PREFIX}"$(TOPDIR)/arch/arm/src/common"
endif

include $(APPDIR)/Application.mk

# Export the same native compiler/ABI flags used for this application. The
# consuming HAL build scripts own private helper compilation and archives.
.PHONY: nxrs-native-context
nxrs-native-context:
	$(file >$(NXRS_NATIVE_CONTEXT_DIR)/cc.txt,$(CC))
	$(file >$(NXRS_NATIVE_CONTEXT_DIR)/ar.txt,$(AR))
	$(file >$(NXRS_NATIVE_CONTEXT_DIR)/cflags.txt,$(CFLAGS) ${INCDIR_PREFIX}"$(TOPDIR)")
	@:
