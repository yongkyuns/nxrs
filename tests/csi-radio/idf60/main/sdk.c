/* SDK header translation only. Rust owns sequencing, deadlines and resources. */
#include "esp_idf_version.h"
#if ESP_IDF_VERSION != ESP_IDF_VERSION_VAL(6, 0, 0)
#error "Reference must use the pinned IDF 6.0 release"
#endif
#include "esp_wifi.h"
#include "esp_wifi_default.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_csi_gain_ctrl.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "../capture-abi.h"
#include <stddef.h>
#include <string.h>
#include <time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sys/poll.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/time.h>
#include <stdlib.h>
#include <errno.h>

_Static_assert(sizeof(struct nxrs_csi_metadata) == 92, "capture metadata size");
_Static_assert(offsetof(struct nxrs_csi_metadata, rx_ctrl) == 28, "capture metadata offset");
_Static_assert(sizeof(wifi_pkt_rx_ctrl_t) == 48, "reference RX control size");
_Static_assert(sizeof(time_t) == 8, "Rust IDF libc time ABI");
_Static_assert(sizeof(struct sockaddr_in) == 16, "Rust IDF libc IPv4 ABI");
_Static_assert(offsetof(struct sockaddr_in, sin_family) == 1, "Rust IDF libc family ABI");

extern void reference_main(void);
extern const uint32_t reference_rust_abi[51];
const uint32_t reference_c_abi[51] = {
    sizeof(time_t), sizeof(struct timespec), sizeof(struct timeval), sizeof(struct sockaddr_in),
    offsetof(struct sockaddr_in,sin_family), offsetof(struct sockaddr_in,sin_port),
    sizeof(struct pollfd), sizeof(pthread_mutex_t), sizeof(pthread_cond_t), sizeof(socklen_t),
    AF_INET, SOCK_STREAM, SOL_SOCKET, SO_RCVTIMEO, SO_SNDTIMEO, IPPROTO_TCP, TCP_NODELAY,
    O_NONBLOCK, FIONBIO, CLOCK_MONOTONIC, sizeof(pthread_attr_t), sizeof(struct sockaddr),
    _Alignof(pthread_mutex_t), _Alignof(pthread_cond_t), sizeof(pthread_rwlock_t), _Alignof(pthread_rwlock_t),
    sizeof(pthread_mutexattr_t), _Alignof(pthread_mutexattr_t), sizeof(pthread_condattr_t), _Alignof(pthread_condattr_t),
    _Alignof(pthread_attr_t), sizeof(pthread_t), POLLIN, POLLOUT, POLLERR, POLLHUP, POLLNVAL,
    EAGAIN, EWOULDBLOCK, EINTR, ETIMEDOUT, EINVAL, ECANCELED, PTHREAD_MUTEX_NORMAL, PTHREAD_MUTEX_ERRORCHECK,
    PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, sizeof(nfds_t),
    offsetof(struct pollfd,events), offsetof(struct pollfd,revents), _Alignof(time_t)
};
void app_main(void) {
    if (memcmp(reference_c_abi,reference_rust_abi,sizeof(reference_c_abi)) != 0) abort();
    reference_main();
}
void reference_quiet(void) { esp_log_level_set("*", ESP_LOG_NONE); }
int reference_usb_install(void) {
    usb_serial_jtag_driver_config_t cfg = {.tx_buffer_size=4096, .rx_buffer_size=4096};
    return usb_serial_jtag_driver_install(&cfg);
}
void reference_usb_stdio(void) { usb_serial_jtag_vfs_use_driver(); }
int reference_usb_read(uint8_t *data, size_t capacity) {
    return usb_serial_jtag_read_bytes(data, capacity, pdMS_TO_TICKS(100));
}
int reference_netif_init(void) { return esp_netif_init(); }
int reference_loop_create(void) { return esp_event_loop_create_default(); }
int reference_loop_delete(void) { return esp_event_loop_delete_default(); }
void *reference_netif_create(void) { return esp_netif_create_default_wifi_sta(); }
void reference_netif_destroy(void *netif) { esp_netif_destroy_default_wifi(netif); }
int reference_register_wifi(esp_event_handler_t cb, void **handle) {
    return esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, cb, NULL, handle);
}
int reference_register_ip(esp_event_handler_t cb, void **handle) {
    return esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, cb, NULL, handle);
}
int reference_unregister_wifi(void *handle) {
    return esp_event_handler_instance_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, handle);
}
int reference_unregister_ip(void *handle) {
    return esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, handle);
}
int reference_event_kind(esp_event_base_t base, int32_t id) {
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) return 1;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) return 2;
    return 0;
}
int reference_wifi_init(void) {
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    cfg.csi_enable = 1;
    return esp_wifi_init(&cfg);
}
int reference_ram_storage(void) { return esp_wifi_set_storage(WIFI_STORAGE_RAM); }
int reference_sta_mode(void) { return esp_wifi_set_mode(WIFI_MODE_STA); }
int reference_no_power_save(void) { return esp_wifi_set_ps(WIFI_PS_NONE); }
int reference_wifi_config(const uint8_t *ssid, size_t slen, const uint8_t *password, size_t plen) {
    wifi_config_t cfg = {0};
    if (!slen || slen > sizeof(cfg.sta.ssid) || plen > 63) return ESP_ERR_INVALID_ARG;
    memcpy(cfg.sta.ssid, ssid, slen);
    memcpy(cfg.sta.password, password, plen);
    return esp_wifi_set_config(WIFI_IF_STA, &cfg);
}
int reference_ap(uint8_t peer[6], uint8_t *channel) {
    wifi_ap_record_t ap;
    int status = esp_wifi_sta_get_ap_info(&ap);
    if (status == ESP_OK) { memcpy(peer, ap.bssid, 6); *channel = ap.primary; }
    return status;
}
int reference_csi_config(void) {
    const wifi_csi_config_t cfg = {
        .lltf_en=true, .htltf_en=false, .stbc_htltf2_en=false,
        .ltf_merge_en=true, .channel_filter_en=true,
        .manu_scale=false, .shift=0, .dump_ack_en=false
    };
    return esp_wifi_set_csi_config(&cfg);
}
const int8_t *reference_translate(const wifi_csi_info_t *info, struct nxrs_csi_metadata *out) {
    if (!info || !info->buf || !out) return NULL;
    struct nxrs_csi_metadata m = {0};
    m.timestamp_us=info->rx_ctrl.timestamp; m.rx_ctrl_len=sizeof(info->rx_ctrl); m.len=info->len;
    memcpy(m.mac,info->mac,6); m.rssi=info->rx_ctrl.rssi; m.noise_floor=info->rx_ctrl.noise_floor;
    m.channel=info->rx_ctrl.channel; m.secondary_channel=info->rx_ctrl.secondary_channel;
    m.sig_mode=info->rx_ctrl.sig_mode; m.bandwidth=info->rx_ctrl.cwb; m.mcs=info->rx_ctrl.mcs;
    m.stbc=info->rx_ctrl.stbc; m.first_word_invalid=info->first_word_invalid;
    memcpy(m.rx_ctrl,&info->rx_ctrl,sizeof(info->rx_ctrl));
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl,&m.agc_gain,&m.fft_gain);
    *out=m; return info->buf;
}
