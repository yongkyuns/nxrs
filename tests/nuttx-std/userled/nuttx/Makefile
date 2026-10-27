include $(APPDIR)/Make.defs
PROGNAME = rust_std
PRIORITY = 100
STACKSIZE = 65536
MODULE = $(CONFIG_EXAMPLES_NXRS_STD)
CSRCS = fixture.c userled.c
EXTOBJS = rust_std.o

# Ordinary Cargo binary and normal std startup; no alternate Rust entry point.
rust_std.o: $(NXRS_STD_ELF)
	$(OBJCOPY) --redefine-sym main=rust_std_main $< $@

include $(APPDIR)/Application.mk
