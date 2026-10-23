idf_component_register(SRCS "sdk.c" INCLUDE_DIRS "." "../vendor/include"
    REQUIRES esp_wifi esp_netif esp_event esp_timer pthread esp_driver_usb_serial_jtag vfs esp_libc lwip)
add_prebuilt_library(reference_rust
    "${CMAKE_CURRENT_LIST_DIR}/../rust-target/xtensa-esp32s3-espidf/release/libcsi_idf60_reference.a"
    REQUIRES esp_libc vfs lwip pthread)
add_prebuilt_library(reference_gain
    "${CMAKE_CURRENT_LIST_DIR}/../vendor/6.0/esp32s3/libesp_csi_gain_ctrl.a"
    REQUIRES log esp_phy)
target_link_libraries(${COMPONENT_LIB} PUBLIC reference_rust reference_gain)
