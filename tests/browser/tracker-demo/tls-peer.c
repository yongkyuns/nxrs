/* Mbed TLS client for the tracker pairing protocol over an ATT byte stream. */
#include <emscripten.h>
#include <mbedtls/platform_util.h>
#include <mbedtls/ssl.h>
#include <psa/crypto.h>
#include <psa/crypto_extra.h>
#include <stdint.h>

static mbedtls_ssl_config config;
static mbedtls_ssl_context session;
static unsigned char key[32];
static unsigned char plaintext[768];

EM_JS(int, browser_random, (unsigned char *out, size_t size), {
  if (!globalThis.isSecureContext || !globalThis.crypto?.getRandomValues) return 0;
  globalThis.crypto.getRandomValues(HEAPU8.subarray(out, out + size));
  return 1;
});

EM_JS(int, browser_send, (const unsigned char *out, size_t size), {
  return Module.writeEncrypted(HEAPU8.subarray(out, out + size));
});

EM_JS(int, browser_receive, (unsigned char *destination, size_t size), {
  return Module.readEncrypted(HEAPU8.subarray(destination, destination + size));
});

psa_status_t mbedtls_psa_external_get_random(
  mbedtls_psa_external_random_context_t *context, uint8_t *out,
  size_t size, size_t *written)
{
  (void)context;
  *written = 0;
  if (!browser_random(out, size))
    {
      return PSA_ERROR_INSUFFICIENT_ENTROPY;
    }

  *written = size;
  return PSA_SUCCESS;
}

static int send_data(void *context, const unsigned char *out, size_t size)
{
  (void)context;
  int sent = browser_send(out, size);
  return sent == 0 ? MBEDTLS_ERR_SSL_WANT_WRITE : sent;
}

static int read_data(void *context, unsigned char *in, size_t size)
{
  (void)context;
  int read = browser_receive(in, size);
  return read == 0 ? MBEDTLS_ERR_SSL_WANT_READ : read;
}

EMSCRIPTEN_KEEPALIVE unsigned char *peer_key(void)
{
  return key;
}

EMSCRIPTEN_KEEPALIVE int peer_start(void)
{
  static const int suites[] = { MBEDTLS_TLS_PSK_WITH_AES_128_GCM_SHA256, 0 };
  static const unsigned char identity[] = "nxrs-tracker-v1";
  mbedtls_ssl_config_init(&config);
  mbedtls_ssl_init(&session);

  int ret = psa_crypto_init();
  if (ret == 0)
    {
      ret = mbedtls_ssl_config_defaults(&config, MBEDTLS_SSL_IS_CLIENT,
                                      MBEDTLS_SSL_TRANSPORT_STREAM,
                                      MBEDTLS_SSL_PRESET_DEFAULT);
    }

  if (ret == 0)
    {
      mbedtls_ssl_conf_min_tls_version(&config, MBEDTLS_SSL_VERSION_TLS1_2);
      mbedtls_ssl_conf_max_tls_version(&config, MBEDTLS_SSL_VERSION_TLS1_2);
      mbedtls_ssl_conf_ciphersuites(&config, suites);
      ret = mbedtls_ssl_conf_psk(&config, key, sizeof(key), identity,
                                sizeof(identity) - 1);
    }

  mbedtls_platform_zeroize(key, sizeof(key));
  if (ret == 0)
    {
      ret = mbedtls_ssl_setup(&session, &config);
      mbedtls_ssl_set_bio(&session, NULL, send_data, read_data, NULL);
    }

  return ret;
}

EMSCRIPTEN_KEEPALIVE int peer_handshake(void)
{
  int ret = mbedtls_ssl_handshake(&session);
  if (ret == 0)
    {
      return mbedtls_ssl_get_ciphersuite_id_from_ssl(&session) ==
             MBEDTLS_TLS_PSK_WITH_AES_128_GCM_SHA256 ? 1 : MBEDTLS_ERR_SSL_INTERNAL_ERROR;
    }

  return ret == MBEDTLS_ERR_SSL_WANT_READ || ret == MBEDTLS_ERR_SSL_WANT_WRITE ? 0 : ret;
}

EMSCRIPTEN_KEEPALIVE unsigned char *peer_plaintext(void)
{
  return plaintext;
}

EMSCRIPTEN_KEEPALIVE int peer_read(void)
{
  int ret = mbedtls_ssl_read(&session, plaintext, sizeof(plaintext));
  return ret == MBEDTLS_ERR_SSL_WANT_READ || ret == MBEDTLS_ERR_SSL_WANT_WRITE ? 0 : ret;
}

EMSCRIPTEN_KEEPALIVE void peer_close(void)
{
  mbedtls_ssl_free(&session);
  mbedtls_ssl_config_free(&config);
  mbedtls_platform_zeroize(plaintext, sizeof(plaintext));
}
