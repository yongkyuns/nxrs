#include <sys/boardctl.h>

int boot_reset_cause(void)
{
#ifdef CONFIG_BOARDCTL_RESET_CAUSE
  struct boardioc_reset_cause_s reset = {0};

  if (boardctl(BOARDIOC_RESET_CAUSE, (uintptr_t)&reset) < 0)
    return -2;

  switch (reset.cause)
    {
      case BOARDIOC_RESETCAUSE_SYS_CHIPPOR:
        return 1;
      case BOARDIOC_RESETCAUSE_SYS_BOR:
        return 2;
      case BOARDIOC_RESETCAUSE_CORE_SOFT:
      case BOARDIOC_RESETCAUSE_CPU_SOFT:
        return 3;
      case BOARDIOC_RESETCAUSE_PIN:
        return 4;
      case BOARDIOC_RESETCAUSE_CORE_DPSP:
      case BOARDIOC_RESETCAUSE_LOWPOWER:
        return 5;
      case BOARDIOC_RESETCAUSE_CORE_MWDT:
      case BOARDIOC_RESETCAUSE_CPU_MWDT:
        return 6;
      case BOARDIOC_RESETCAUSE_SYS_RWDT:
      case BOARDIOC_RESETCAUSE_CORE_RWDT:
      case BOARDIOC_RESETCAUSE_CPU_RWDT:
        return 7;
      default:
        return 0;
    }
#else
  return -1;
#endif
}
