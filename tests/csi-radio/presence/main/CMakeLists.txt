idf_component_register(SRCS "sdk.c" INCLUDE_DIRS "." "../vendor/include"
    REQUIRES esp_wifi_sensing esp_wifi esp_netif esp_event esp_timer pthread esp_driver_usb_serial_jtag vfs esp_libc lwip)
add_prebuilt_library(reference_rust
    "${CMAKE_CURRENT_LIST_DIR}/../rust-target/xtensa-esp32s3-espidf/release/libcsi_idf60_reference.a"
    REQUIRES esp_libc vfs lwip pthread)
target_link_libraries(${COMPONENT_LIB} PUBLIC reference_rust)

target_link_libraries(${COMPONENT_LIB} PUBLIC
    "-Wl,-u,presence_lab_queue_bytes"
    "-Wl,-u,presence_model_queue_bytes"
    "-Wl,-u,presence_model_control_bytes"
    "-Wl,--wrap=esp_radar_motion_dec_compute"
    "-Wl,--wrap=esp_radar_motion_dec_wander_compute"
    "-Wl,--wrap=esp_csi_gain_ctrl_compensate_rx_gain")
