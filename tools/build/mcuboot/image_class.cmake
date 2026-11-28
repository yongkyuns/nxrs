# Include through CMAKE_PROJECT_mcuboot_esp32s3_INCLUDE, preserving upstream's
# source/build layout. Its executable is declared after project(), so defer
# attaching the callback until that directory has finished configuring.
if(CMAKE_VERSION VERSION_LESS 3.19)
    message(FATAL_ERROR "The MCUboot image-class integration requires CMake 3.19 or newer")
endif()

function(nxrs_add_image_class)
    target_sources(mcuboot_esp32s3.elf PRIVATE
        "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/mcuboot_image_class.c")
endfunction()

cmake_language(DEFER CALL nxrs_add_image_class)
