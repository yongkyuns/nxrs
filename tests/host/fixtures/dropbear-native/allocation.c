#include "includes.h"
#include <setjmp.h>
static jmp_buf failed;
static int inject;
static unsigned frees;
static void *failing_realloc(void *pointer, size_t size)
{ return inject ? NULL : realloc(pointer,size); }
static void tracked_free(void *pointer) { frees++; free(pointer); }
#define realloc failing_realloc
#define free tracked_free
#include "netutils/dropbear/dropbear/src/dbmalloc.c"
#undef realloc
#undef free
void dropbear_exit(const char *format, ...)
{ (void)format; longjmp(failed,1); }
int main(void)
{
  m_malloc_set_epoch(0);
  void *persistent = m_malloc(11);
  m_malloc_set_epoch(1);
  void *session = m_malloc(16);
  memset(session, 0x45, 16);
  inject = 1;
  if (setjmp(failed) == 0)
    {
      m_realloc(session,32);
      assert(!"failure was not propagated");
    }
  m_malloc_free_epoch(1,1);
  assert(frees == 1 && staple != NULL && staple->next == NULL);
  m_free_direct(persistent);
  assert(frees == 2 && staple == NULL);
  return 0;
}
