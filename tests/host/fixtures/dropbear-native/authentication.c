/* SPDX-License-Identifier: MIT
 * Compile the pinned, patched password handler with controlled protocol input
 * and a password-store stub. This checks authentication admission and secret
 * wiping, not NuttX ABI, the password database implementation or wire parsing.
 */

#include "includes.h"
/* The receipt now includes the real session.h. This isolated handler witness
 * supplies its small session ABI above rather than the full transport graph.
 */
#define DROPBEAR_SESSION_H_
#include "netutils/dropbear/dropbear/src/svr-authpasswd.c"

struct sshsession ses;
struct serversession svr_ses;
struct svr_runopts svr_opts;
struct runopts opts;

static const char *wire_password;
static unsigned int wire_length;
static unsigned char change_password;
static int verifier_result;
static int verifications;
static int successes;
static int failures;
static int wipes;
static int frees;

unsigned char buf_getbool(void *payload)
{
  (void)payload;
  return change_password;
}

char *buf_getstring(void *payload, unsigned int *length)
{
  char *password;

  (void)payload;
  password = calloc(wire_length + 1, 1);
  assert(password != NULL);
  memcpy(password, wire_password, wire_length);
  *length = wire_length;
  return password;
}

int dropbear_verify_password(const char *username, const char *password)
{
  assert(strcmp(username, "administrator") == 0);
  assert(password != NULL);
  verifications++;
  return verifier_result;
}

void m_burn(void *pointer, size_t length)
{
  assert(length == wire_length);
  memset(pointer, 0, length);
  wipes++;
}

void m_free_direct(void *pointer)
{
  const unsigned char *bytes = pointer;
  unsigned int index;

  assert(wipes == 1);
  for (index = 0; index < wire_length; index++)
    {
      assert(bytes[index] == 0);
    }

  free(pointer);
  frees++;
}

void dropbear_log(int level, const char *message, ...)
{
  (void)level;
  (void)message;
}

void send_msg_userauth_failure(int partial, int increment)
{
  assert(partial == 0);
  assert(increment == 1);
  failures++;
}

void send_msg_userauth_success(void)
{
  successes++;
}

static void trial(const char *name, const char *bytes, unsigned int length,
                  int valid_user, int check_result, bool change,
                  int expected_verifications, int expected_success)
{
  wire_password = bytes;
  wire_length = length;
  change_password = change;
  verifier_result = check_result;
  verifications = successes = failures = wipes = frees = 0;

  svr_auth_password(valid_user);

  assert(verifications == expected_verifications);
  assert(successes == expected_success);
  assert(failures == !expected_success);
  assert(wipes == !change);
  assert(frees == !change);
  printf("PASS authentication %s\n", name);
}

int main(void)
{
  char oversized[DROPBEAR_MAX_PASSWORD_LEN + 1];
  static const char embedded_nul[] = "correct\0suffix";
  static const char leading_nul[] = "\0suffix";

  ses.authstate.pw_name = "administrator";
  ses.authstate.authtypes = AUTH_TYPE_PASSWORD;
  svr_ses.addrstring = "127.0.0.1";
  memset(oversized, 'a', sizeof(oversized));

  trial("valid", "correct", 7, 1, DROPBEAR_SUCCESS, false, 1, 1);
  trial("wrong", "wrong", 5, 1, DROPBEAR_FAILURE, false, 1, 0);
  trial("unknown-store-user", "correct", 7, 1, -ENOENT, false, 1, 0);
  trial("store-read-error", "correct", 7, 1, -EIO, false, 1, 0);
  trial("invalid-user", "correct", 7, 0, DROPBEAR_SUCCESS, false, 0, 0);

  /* A successful stub result must not bypass these input checks. In the
   * inherited handler, auth_ok=0 incorrectly accepted the embedded-NUL cases
   * even though the verifier was never called.
   */

  trial("embedded-nul", embedded_nul, sizeof(embedded_nul) - 1,
        1, DROPBEAR_SUCCESS, false, 0, 0);
  trial("leading-nul", leading_nul, sizeof(leading_nul) - 1,
        1, DROPBEAR_SUCCESS, false, 0, 0);
  trial("empty", "", 0, 1, DROPBEAR_SUCCESS, false, 0, 0);
  trial("oversized", oversized, sizeof(oversized),
        1, DROPBEAR_SUCCESS, false, 0, 0);
  trial("password-change", "correct", 7,
        1, DROPBEAR_SUCCESS, true, 0, 0);
  return 0;
}
