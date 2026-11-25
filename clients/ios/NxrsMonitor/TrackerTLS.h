#ifndef NXRS_TRACKER_TLS_H
#define NXRS_TRACKER_TLS_H
#include <Security/SecProtocolOptions.h>
#include <Security/SecProtocolMetadata.h>
#include <stdbool.h>
#include <stdint.h>

void tracker_tls_pairing(sec_protocol_options_t options, const uint8_t *key);
bool tracker_tls_authenticated(sec_protocol_metadata_t metadata);

#endif
