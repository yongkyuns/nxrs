/* Diagnostic reuse of the unchanged C contract. Only the work symbol is
 * retained in the probe; other functions are discarded by section GC. */
#define es_work_value probe_c
#include "core.c"
