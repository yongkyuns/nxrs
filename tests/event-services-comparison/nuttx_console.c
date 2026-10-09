/* SPDX-License-Identifier: MIT */
#include <errno.h>
#include <malloc.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#ifdef ES_RUST
extern int es_rust_main(int argc, char **argv);
#define es_application es_rust_main
#else
extern int es_c_main(int argc, char **argv);
#define es_application es_c_main
#endif

/* The same small command transport as the other RTOS fixtures, not a shell.
 * Parsing and output occur outside the measured service window. */
int es_console_main(int argc, char **argv) {
  (void)argc;
  (void)argv;
  puts("EVENT_SERVICES_READY console=event");
  for (;;) {
    char command[16];
    unsigned length = 0;
    int overflow = 0;
    printf("event> ");
    fflush(stdout);
    for (;;) {
      char byte;
      ssize_t count = read(STDIN_FILENO, &byte, 1);
      if (count < 0 && errno == EINTR)
        continue;
      if (count != 1)
        return 1;
      if (byte == '\r' || byte == '\n')
        break;
      if (length < sizeof(command) - 1)
        command[length++] = byte;
      else
        overflow = 1;
    }
    command[length] = 0;
    if (!length)
      continue;
    if (overflow) {
      puts("ES_COMMAND_EXIT status=1");
      continue;
    }
    if (strcmp(command, "memory") == 0) {
      struct mallinfo heap = mallinfo();
      puts("total used free maxused maxfree nused nfree");
      printf("%d %d %d %d %d %d %d Umem\n", heap.arena, heap.uordblks,
             heap.fordblks, heap.usmblks, heap.mxordblk, heap.aordblks,
             heap.ordblks);
      continue;
    }
    char *arguments[] = {"event-services", command, NULL};
    int status = es_application(2, arguments);
    printf("ES_COMMAND_EXIT status=%d\n", status);
  }
}
