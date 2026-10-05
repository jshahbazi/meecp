/* Independent own-process CPU units and checked arithmetic; numeric output. */
#include <arpa/inet.h>
#include <libproc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#include "../../scripts/mcpme-command-resource-v1.h"

static uint64_t ns(struct timespec value) { return (uint64_t)value.tv_sec * 1000000000 + value.tv_nsec; }
int main(void) {
    alarm(2);
    struct rusage_info_v1 before = {0}, after = {0};
    struct timespec start, end, wall, current;
    mach_timebase_info_data_t timebase;
    if (mach_timebase_info(&timebase) || proc_pid_rusage(getpid(), RUSAGE_INFO_V1, (rusage_info_t *)&before) ||
        clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &start) || clock_gettime(CLOCK_MONOTONIC, &wall)) return 64;
    volatile uint64_t work = 1;
    do {
        for (int i = 0; i < 10000; ++i) work = work * 1664525 + 1013904223;
        if (clock_gettime(CLOCK_MONOTONIC, &current)) return 64;
    } while (ns(current) - ns(wall) < 30000000);
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &end) ||
        proc_pid_rusage(getpid(), RUSAGE_INFO_V1, (rusage_info_t *)&after)) return 64;
    uint64_t raw = after.ri_user_time + after.ri_system_time - before.ri_user_time - before.ri_system_time;
    uint64_t converted, overflow = 0;
    if (resource_ns(raw, timebase.numer, timebase.denom, &converted)) return 65;
    int rejected = resource_ns(UINT64_MAX, UINT32_MAX, 1, &overflow) == -1 && overflow == UINT64_MAX;
    uint64_t added = 0;
    int addition = resource_add(UINT64_MAX, 1, &added) == -1 && added == UINT64_MAX;
    printf("{\"raw\":%llu,\"converted_ns\":%llu,\"clock_cpu_ns\":%llu,\"numer\":%u,\"denom\":%u,\"overflow_rejected\":%d,\"addition_rejected\":%d}\n",
        (unsigned long long)raw, (unsigned long long)converted, (unsigned long long)(ns(end)-ns(start)),
        timebase.numer, timebase.denom, rejected, addition);
    return 0;
}
