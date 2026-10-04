/* Qualification-only pthread lifecycle shim. Target headers own every native
 * layout. No Rust Thread, TLS ID, parker, name or spawn hooks are provided. */
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>

int nxrs_cq_thread_start(void **handle, size_t stack,
                         void *(*entry)(void *), void *argument)
{
  pthread_attr_t attributes;
  pthread_t *thread = malloc(sizeof(*thread));
  int error;

  *handle = NULL;
  if (thread == NULL)
    {
      return ENOMEM;
    }

  error = pthread_attr_init(&attributes);
  if (error != 0)
    {
      free(thread);
      return error;
    }

  error = pthread_attr_setstacksize(&attributes, stack);
  if (error == 0)
    {
      error = pthread_create(thread, &attributes, entry, argument);
    }

  /* A successfully started callback now owns argument; an attr cleanup error
   * must not be reported as a spawn failure (which would double-free it). */
  (void)pthread_attr_destroy(&attributes);
  if (error != 0)
    {
      free(thread);
      return error;
    }

  *handle = thread;
  return 0;
}

int nxrs_cq_thread_join(void *handle, void **result)
{
  pthread_t *thread = handle;
  int error = pthread_join(*thread, result);
  if (error == 0)
    {
      free(thread);
    }

  return error;
}
