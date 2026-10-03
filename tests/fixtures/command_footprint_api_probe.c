/* Public Mach API, owned self only. No target lookup, privilege or entitlement. */
#include <mach/mach.h>
#include <libproc.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct result { int lower, inherited, raise, raised_from, usage_ok; };
int main(void) {
    alarm(3);
    int old = -2;
    kern_return_t initial = task_set_phys_footprint_limit(mach_task_self(), 128, &old);
    struct result result = {-1, -2, -1, -2, 0};
    int joined = 0, status = 0;
    long peak = 0;
    if (initial == KERN_SUCCESS) {
        int channel[2];
        if (pipe(channel)) return 20;
        pid_t child = fork();
        if (child == -1) return 21;
        if (!child) {
            alarm(2); close(channel[0]);
            result.lower = task_set_phys_footprint_limit(mach_task_self(), 64, &result.inherited);
            result.raise = task_set_phys_footprint_limit(mach_task_self(), 128, &result.raised_from);
            volatile unsigned char *bytes = malloc(1024 * 1024);
            if (!bytes) _exit(22);
            for (size_t i = 0; i < 1024 * 1024; i += 4096) bytes[i] = 1;
            struct rusage_info_v1 usage = {0};
            result.usage_ok = proc_pid_rusage(getpid(), RUSAGE_INFO_V1, (rusage_info_t *)&usage) == 0;
            free((void *)bytes);
            ssize_t wrote = write(channel[1], &result, sizeof(result));
            close(channel[1]); _exit(wrote == sizeof(result) ? 0 : 23);
        }
        close(channel[1]);
        ssize_t got = read(channel[0], &result, sizeof(result)); close(channel[0]);
        struct rusage usage = {0};
        joined = wait4(child, &status, 0, &usage) == child;
        peak = usage.ru_maxrss;
        if (!joined || got != sizeof(result) || !WIFEXITED(status) || WEXITSTATUS(status)) return 24;
    }
    printf("{\"initial_result\":%d,\"no_access_code\":%d,\"old_limit_mb\":%d,"
           "\"child_lower_result\":%d,\"child_inherited_mb\":%d,\"child_raise_result\":%d,"
           "\"child_raised_from_mb\":%d,\"child_usage_ok\":%d,\"child_joined\":%d,"
           "\"child_terminal_peak_bytes\":%ld,\"hard_aggregate_enforced\":false,\"lifetime_coverage_complete\":false}\n",
           initial, KERN_NO_ACCESS, old, result.lower, result.inherited, result.raise,
           result.raised_from, result.usage_ok, joined, peak);
    return 0;
}
