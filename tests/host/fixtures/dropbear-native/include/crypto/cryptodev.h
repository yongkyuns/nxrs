#pragma once
#include <stdint.h>
#define CRIOGET 1
#define CIOCGSESSION 2
#define CIOCCRYPT 3
#define CIOCFSESSION 4
#define CRYPTO_SHA2_256_HMAC 5
#define COP_ENCRYPT 1
#define COP_FLAG_UPDATE 2
struct session_op { int mac; char *mackey; int mackeylen; uint32_t ses; };
struct crypt_op { uint32_t ses; int op,flags; unsigned len; char *src,*mac; };
