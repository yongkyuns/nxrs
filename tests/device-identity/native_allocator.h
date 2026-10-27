/* Test-only compiler-location/allocator declarations; Rust owns policy. */
#ifndef NXRS_TEST_DEVICE_IDENTITY_NATIVE_ALLOCATOR_H
#define NXRS_TEST_DEVICE_IDENTITY_NATIVE_ALLOCATOR_H
#include <stddef.h>
#include <stdint.h>
void *nxrs_test_di_calloc_at(size_t count, size_t size, const char *file, uint32_t line);
void nxrs_test_di_free(void *pointer);
#endif
