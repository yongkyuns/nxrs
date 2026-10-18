#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define FAR
#define OK 0
#define CONFIG_NET_TCP_NPOLLWAITERS 2
#define CONFIG_NET_SOCKOPTS 1
#define TCP_CLOSED 0
#define TCP_ALLOCATED 1
#define TCP_SYN_SENT 3
#define TCP_ESTABLISHED 4
#define TCP_ACKDATA (1u << 0)
#define TCP_NEWDATA (1u << 1)
#define TCP_POLL (1u << 4)
#define TCP_BACKLOG (1u << 10)
#define TCP_ABORT (1u << 11)
#define TCP_CONNECTED (1u << 12)
#define TCP_TIMEDOUT (1u << 13)
#define TCP_RXCLOSE (1u << 16)
#define NETDEV_DOWN (1u << 17)
#define TCP_DISCONN_EVENTS (TCP_ABORT | TCP_TIMEDOUT | NETDEV_DOWN)
#define POLLIN 0x01
#define POLLOUT 0x04
#define POLLERR 0x08
#define POLLHUP 0x10
#define POLLRDNORM 0x01
#define POLLWRNORM 0x04
#define SHUT_RD 1
#define IFF_UP 1
#define _SF_NONBLOCK 0x08
#define _SF_LISTENING 0x10
#define _SF_CONNECTED 0x40
#define _SF_CLOSED 0x80
#define _SS_ISNONBLOCK(s) (((s) & _SF_NONBLOCK) != 0)
#define _SS_ISLISTENING(s) (((s) & _SF_LISTENING) != 0)
#define _SS_ISCONNECTED(s) (((s) & _SF_CONNECTED) != 0)
#define _SS_ISCLOSED(s) (((s) & _SF_CLOSED) != 0)
#define _SO_CONN_SETERRNO(c, e) ((c)->sconn.s_error = (e))
#define DEBUGASSERT(x) assert(x)
#define DEBUGPANIC() assert(!"callback slots exhausted")
#define ninfo(...) ((void)0)
#define nerr(...) ((void)0)
#define nwarn(...) ((void)0)
#define PRIx32 "x"
#define IFF_IS_UP(f) (((f) & IFF_UP) != 0)

typedef uint32_t pollevent_t;
struct net_driver_s { unsigned d_flags; };
struct socket_conn_s { uint8_t s_flags; int s_error; };
struct pollfd { short events; short revents; void *priv; };
struct devif_callback_s {
  uint32_t (*event)(struct net_driver_s *, void *, uint32_t);
  void *priv;
  uint32_t flags;
};
struct tcp_conn_s;
struct tcp_poll_s {
  struct tcp_conn_s *conn;
  struct pollfd *fds;
  struct devif_callback_s *cb;
};
struct tcp_conn_s {
  struct socket_conn_s sconn;
  uint8_t tcpstateflags;
  struct tcp_poll_s pollinfo[CONFIG_NET_TCP_NPOLLWAITERS];
  struct net_driver_s *dev;
  void *readahead;
  unsigned shutdown;
};
struct socket { struct tcp_conn_s *s_conn; };

static struct devif_callback_s callbacks[CONFIG_NET_TCP_NPOLLWAITERS];
static unsigned callback_count;
static unsigned lost_count;
static int send_capacity;

static int cansend(struct tcp_conn_s *conn)
{
  (void)conn;
  return send_capacity;
}

#define psock_tcp_cansend(c) cansend(c)

static void poll_notify(struct pollfd **fds, int nfds, pollevent_t events)
{
  (void)nfds;
  if (events & (POLLERR | POLLHUP)) (*fds)->revents |= events;
  else (*fds)->revents |= events & (*fds)->events;
}

static void tcp_lost_connection(struct tcp_conn_s *conn,
                               struct devif_callback_s *cb, uint32_t flags)
{
  (void)conn; (void)cb; (void)flags;
  lost_count++;
}

static void conn_dev_lock(struct socket_conn_s *conn, struct net_driver_s *dev)
{ (void)conn; (void)dev; }
static void conn_dev_unlock(struct socket_conn_s *conn, struct net_driver_s *dev)
{ (void)conn; (void)dev; }

static struct devif_callback_s *tcp_callback_alloc(struct tcp_conn_s *conn)
{
  (void)conn;
  if (callback_count == CONFIG_NET_TCP_NPOLLWAITERS) return NULL;
  return &callbacks[callback_count++];
}

static bool netdev_verify(struct net_driver_s *dev) { return dev != NULL; }
static bool tcp_backlogpending(struct tcp_conn_s *conn) { (void)conn; return false; }

#include "tcp-poll-functions.h"

#define CHECK(x) do { if (!(x)) { \
  fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); return 1; \
} } while (0)

static void reset(struct tcp_conn_s *conn, struct socket *sock,
                  struct pollfd *pfd, uint8_t state, uint8_t flags)
{
  memset(conn, 0, sizeof(*conn));
  memset(sock, 0, sizeof(*sock));
  memset(pfd, 0, sizeof(*pfd));
  memset(callbacks, 0, sizeof(callbacks));
  callback_count = lost_count = 0;
  send_capacity = 0;
  conn->tcpstateflags = state;
  conn->sconn.s_flags = flags;
  sock->s_conn = conn;
  pfd->events = POLLOUT;
}

static int run(const char *label,
               int (*setup)(struct socket *, struct pollfd *),
               uint32_t (*event)(struct net_driver_s *, void *, uint32_t),
               bool fixed)
{
  struct tcp_conn_s conn;
  struct socket sock;
  struct pollfd pfd;
  struct devif_callback_s *cb;

  reset(&conn, &sock, &pfd, TCP_SYN_SENT, _SF_NONBLOCK);
  CHECK(setup(&sock, &pfd) == 0);
  CHECK(!(pfd.revents & (POLLERR | POLLHUP)));
  cb = conn.pollinfo[0].cb;
  CHECK(cb != NULL && (cb->flags & TCP_CONNECTED));
  cb->flags = TCP_POLL | TCP_ACKDATA;
  event(NULL, cb->priv, TCP_POLL | TCP_ACKDATA);
  if (fixed) CHECK(!(pfd.revents & POLLOUT));
  else CHECK(pfd.revents & POLLOUT);

  reset(&conn, &sock, &pfd, TCP_SYN_SENT, 0);
  CHECK(setup(&sock, &pfd) == 0);
  cb = conn.pollinfo[0].cb;
  CHECK(cb != NULL);
  if (fixed)
    CHECK(!(pfd.revents & (POLLERR | POLLHUP)) && (cb->flags & TCP_CONNECTED));
  else
    CHECK((pfd.revents & (POLLERR | POLLHUP)) == (POLLERR | POLLHUP) &&
          conn.sconn.s_error == ENOTCONN && !(cb->flags & TCP_CONNECTED));

  reset(&conn, &sock, &pfd, TCP_ESTABLISHED, _SF_CONNECTED);
  CHECK(setup(&sock, &pfd) == 0 && (pfd.revents & POLLOUT));
  pfd.revents = 0;
  cb = conn.pollinfo[0].cb;
  cb->flags = TCP_POLL;
  event(NULL, cb->priv, TCP_POLL);
  CHECK(pfd.revents & POLLOUT);

  /* A successful handshake wakes poll, including in write-buffer builds. */
  reset(&conn, &sock, &pfd, TCP_SYN_SENT, 0);
  CHECK(setup(&sock, &pfd) == 0);
  cb = conn.pollinfo[0].cb;
  pfd.revents = 0;
  event(NULL, cb->priv, TCP_CONNECTED);
  CHECK(pfd.revents & POLLOUT);

  reset(&conn, &sock, &pfd, TCP_ALLOCATED, _SF_NONBLOCK);
  CHECK(setup(&sock, &pfd) == 0);
  CHECK(conn.pollinfo[0].cb->flags & TCP_CONNECTED);
  reset(&conn, &sock, &pfd, TCP_ALLOCATED, 0);
  CHECK(setup(&sock, &pfd) == 0);
  CHECK((pfd.revents & (POLLERR | POLLHUP)) == (POLLERR | POLLHUP));
  CHECK(conn.sconn.s_error == ENOTCONN);
  CHECK(!(conn.pollinfo[0].cb->flags & TCP_CONNECTED));

  reset(&conn, &sock, &pfd, TCP_ESTABLISHED, 0);
  CHECK(setup(&sock, &pfd) == 0);
  CHECK((pfd.revents & (POLLERR | POLLHUP)) == (POLLERR | POLLHUP));
  CHECK(conn.sconn.s_error == ENOTCONN);

  reset(&conn, &sock, &pfd, TCP_CLOSED, _SF_CLOSED);
  CHECK(setup(&sock, &pfd) == 0);
  CHECK((pfd.revents & (POLLERR | POLLHUP)) == (POLLERR | POLLHUP));
  CHECK(conn.sconn.s_error == ECONNREFUSED);

  const uint32_t losses[] = { TCP_ABORT, TCP_TIMEDOUT, NETDEV_DOWN };
  const int errors[] = { ECONNREFUSED, ETIMEDOUT, ENETUNREACH };
  for (unsigned i = 0; i < 3; i++)
    {
      reset(&conn, &sock, &pfd, TCP_ESTABLISHED, _SF_CONNECTED);
      CHECK(setup(&sock, &pfd) == 0);
      cb = conn.pollinfo[0].cb;
      pfd.revents = 0;
      event(NULL, cb->priv, losses[i]);
      CHECK((pfd.revents & (POLLERR | POLLHUP)) == (POLLERR | POLLHUP));
      CHECK(conn.sconn.s_error == errors[i] && lost_count == 1);
    }

  reset(&conn, &sock, &pfd, TCP_ESTABLISHED, _SF_CONNECTED);
  conn.dev = &(struct net_driver_s){ .d_flags = 0 };
  CHECK(setup(&sock, &pfd) == 0);
  CHECK((pfd.revents & (POLLERR | POLLHUP)) == (POLLERR | POLLHUP));
  CHECK(conn.sconn.s_error == ENETDOWN);
  printf("%s TCP poll behavior passed\n", label);
  return 0;
}

int main(void)
{
  int rc = 0;
  rc |= run("baseline", baseline_setup, baseline_event, false);
  rc |= run("patched", patched_setup, patched_event, true);
  return rc;
}
