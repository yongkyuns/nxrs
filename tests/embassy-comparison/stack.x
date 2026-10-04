/* App-owned override of the HAL's all-remaining-RAM stack gap.
 * Shared main/executor/interrupt stack is bounded at 8 KiB, not per task.
 * The report checks the actual ELF section and symbol sizes.
 */
SECTIONS {
  .stack ORIGIN(RWDATA) + LENGTH(RWDATA) - 8192 (NOLOAD) : ALIGN(16) {
    _stack_end = ABSOLUTE(.);
    _stack_end_cpu0 = ABSOLUTE(.);
    __stack_chk_guard = ABSOLUTE(.) + 64;
    . += 8192;
    _stack_start = ABSOLUTE(.);
    _stack_start_cpu0 = ABSOLUTE(.);
  } > RWDATA
}
