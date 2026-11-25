#include "TrackerTLS.h"
#include <Security/CipherSuite.h>
#include <dispatch/dispatch.h>

void tracker_tls_pairing(sec_protocol_options_t options, const uint8_t *key)
{
    static const char identity[] = "nxrs-tracker-v1";
    dispatch_queue_t queue = dispatch_get_main_queue();
    sec_protocol_options_set_min_tls_protocol_version(options, tls_protocol_version_TLSv12);
    sec_protocol_options_set_max_tls_protocol_version(options, tls_protocol_version_TLSv12);
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    sec_protocol_options_add_tls_ciphersuite(options, TLS_PSK_WITH_AES_128_GCM_SHA256);
#pragma clang diagnostic pop
    sec_protocol_options_add_pre_shared_key(options,
        dispatch_data_create(key, 32, queue, DISPATCH_DATA_DESTRUCTOR_DEFAULT),
        dispatch_data_create(identity, sizeof(identity) - 1, queue, DISPATCH_DATA_DESTRUCTOR_DEFAULT));
}

bool tracker_tls_authenticated(sec_protocol_metadata_t metadata)
{
    return sec_protocol_metadata_get_negotiated_tls_protocol_version(metadata) ==
             tls_protocol_version_TLSv12 &&
           (uint16_t)sec_protocol_metadata_get_negotiated_tls_ciphersuite(metadata) ==
             TLS_PSK_WITH_AES_128_GCM_SHA256;
}
