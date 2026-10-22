#pragma once
// Mechanical SDK hooks. No SDK tasks, camera operations or application policy.
#include <cassert>
#include <cstdint>
#include <climits>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <time.h>
#define ESP_OK 0
#define ESP_FAIL -1
using esp_err_t=int;
#define ESP_ERROR_CHECK(x) assert((x)==0)
#define ESP_LOGE(tag,fmt,...) fprintf(stderr,fmt "\n", ##__VA_ARGS__)
#define ESP_LOGW(tag,fmt,...) ((void)0)
#define ESP_LOGI(tag,fmt,...) ((void)0)
#define ESP_LOGD(tag,fmt,...) ((void)0)
#define MALLOC_CAP_DEFAULT 0
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_INTERNAL 2
#define MALLOC_CAP_SIMD 4
#define MALLOC_CAP_8BIT 8
#define HEAP_IRAM_ATTR
#define IRAM_ATTR
inline void* heap_caps_aligned_alloc(size_t a,size_t n,uint32_t){void*p=nullptr;return posix_memalign(&p,a,n)?nullptr:p;}
inline void* heap_caps_aligned_calloc(size_t a,size_t n,size_t s,uint32_t c){if(s && n>SIZE_MAX/s)return nullptr;void*p=heap_caps_aligned_alloc(a,n*s,c);if(p)memset(p,0,n*s);return p;}
inline void heap_caps_free(void*p){free(p);}
// The fixed graph sizes its arena; allocations use the qualified process heap.
// Product wiring budgets memory and worker stack. No SDK memory-region or
// flash-placement policy is emulated by these numerical hooks.
inline size_t heap_caps_get_largest_free_block(uint32_t){return 0;}
inline bool esp_ptr_external_ram(void*){return false;}
inline bool esp_ptr_in_drom(void*){return false;}
inline bool esp_ptr_internal(void*){return false;}
inline int64_t esp_timer_get_time(){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return 0;return int64_t(t.tv_sec)*1000000+t.tv_nsec/1000;}
inline uint32_t esp_cpu_get_cycle_count(){return uint32_t(esp_timer_get_time());}
