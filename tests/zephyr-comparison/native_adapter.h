/* SPDX-License-Identifier: MIT
 * Compile-time vocabulary adapter, NOT Zephyr's POSIX implementation.
 * The matched C workload calls native k_msgq/k_poll/k_thread directly through
 * these small wrappers. It is intentionally limited to this finite fixture.
 */
#ifndef NXRS_ZEPHYR_NATIVE_ADAPTER_H
#define NXRS_ZEPHYR_NATIVE_ADAPTER_H
#include <zephyr/kernel.h>
#include <stdint.h>
#include <time.h>

#define O_CREAT 1
#define O_EXCL 2
#define O_RDWR 4
#define POLLIN 1
#define POLLERR 2
#define POLLNVAL 4
#define CLOCK_MONOTONIC 1
typedef int mqd_t;
typedef unsigned nxrs_thread_id;
typedef struct { size_t stack_size; } nxrs_thread_attr;
#define pthread_t nxrs_thread_id
#define pthread_attr_t nxrs_thread_attr
struct mq_attr { long mq_maxmsg, mq_msgsize; };
struct pollfd { int fd; short events, revents; };
int nxrs_mq_open(const char *, int, unsigned, const struct mq_attr *);
int nxrs_mq_close(int);
int nxrs_mq_send(int, const char *, size_t, unsigned);
long nxrs_mq_receive(int, char *, size_t, unsigned *);
int nxrs_poll(struct pollfd *, unsigned, int);
int nxrs_thread_create(nxrs_thread_id *, const nxrs_thread_attr *,
                       void *(*)(void *), void *);
int nxrs_thread_join(nxrs_thread_id, void **);
int nxrs_clock_gettime(int, struct timespec *);
int nxrs_nanosleep(const struct timespec *, struct timespec *);
void nxrs_adapter_reset(void);
void nxrs_adapter_report(void);

#define mq_open nxrs_mq_open
#define mq_close nxrs_mq_close
#define mq_send nxrs_mq_send
#define mq_receive nxrs_mq_receive
static inline int mq_unlink(const char *name) { (void)name; return 0; }
#define poll nxrs_poll
#define pthread_create nxrs_thread_create
#define pthread_join nxrs_thread_join
#define clock_gettime nxrs_clock_gettime
#define nanosleep nxrs_nanosleep
#define getpid() (1)
#define perror nxrs_perror
static inline int pthread_attr_init(nxrs_thread_attr *a) { a->stack_size = 4096; return 0; }
static inline int pthread_attr_setstacksize(nxrs_thread_attr *a, size_t s) { a->stack_size = s; return 0; }
static inline int pthread_attr_destroy(nxrs_thread_attr *a) { (void)a; return 0; }
#endif
