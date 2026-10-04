# SPDX-License-Identifier: MIT
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(nxrs_zephyr_comparison C)

set(NXRS_MODE "packet" CACHE STRING "wire, packet, or baseline")
if(NOT NXRS_MODE MATCHES "^(wire|packet|baseline)$")
  message(FATAL_ERROR "NXRS_MODE must be wire, packet, or baseline")
endif()
target_sources(app PRIVATE main.c memory.c)
target_compile_definitions(app PRIVATE NXRS_MODE="${NXRS_MODE}")
if(NXRS_MODE STREQUAL "baseline")
  target_compile_definitions(app PRIVATE NXRS_BASELINE)
else()
  set(control ${CMAKE_CURRENT_BINARY_DIR}/matched_control.c)
  execute_process(COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/generate_control.py
    ${CMAKE_CURRENT_SOURCE_DIR}/../service-footprint/channel_scale_mq.c ${control}
    COMMAND_ERROR_IS_FATAL ANY)
  target_sources(app PRIVATE ${control} native_adapter.c)
  target_include_directories(app PRIVATE . ../service-footprint)
  target_compile_definitions(app PRIVATE NXRS_CQ_DEVICE NXRS_CQ_SYNCHRONIZED
    NXRS_CQ_EMBED_CONTROL)
  if(NXRS_MODE STREQUAL "wire")
    target_compile_definitions(app PRIVATE NXRS_CQ_WIRE_ONLY NXRS_ACK_BYTES=16)
  else()
    target_compile_definitions(app PRIVATE NXRS_CQ_PACKET_SERVICE NXRS_ACK_BYTES=28)
    target_sources(app PRIVATE ../service-footprint/pipeline_processing.c
      ../service-footprint/payload_processing.c)
  endif()
  if(NXRS_APP_SPEED)
    # Match the NuttX application-only O2 control; leave both kernels at Os.
    set_source_files_properties(${control} ../service-footprint/pipeline_processing.c
      ../service-footprint/payload_processing.c PROPERTIES COMPILE_OPTIONS "-O2")
  endif()
endif()
